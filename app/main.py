"""
main.py — FastAPI application entry point.

This file wires together the app, the database pool, and the HTTP routes.
It is intentionally short: business logic lives in other modules.
"""

import logging
from contextlib import asynccontextmanager
import pathlib

from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import app.db as db
from app.routes_documents import router as documents_router
from app.routes_chat import router as chat_router
from app.processing import recover_interrupted_documents

logger = logging.getLogger(__name__)

# Path to the static folder — resolved relative to this file so it works
# regardless of the working directory uvicorn was launched from.
_STATIC_DIR = pathlib.Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Lifespan: startup and shutdown logic
# ---------------------------------------------------------------------------
# The lifespan approach (instead of the older @app.on_event decorators)
# is the current FastAPI best practice.  Everything before `yield` runs at
# startup; everything after `yield` runs at shutdown.

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    # Initialise the connection pool first, then run the schema so the
    # tables exist before any request handler tries to use them.
    db.init_pool()
    db.init_db()
    print("✓ Database pool ready and schema applied.")

    # Recover any documents that were interrupted mid-processing by a server restart.
    # Why safe here? At startup no background job can be running, and automatic
    # restarts would spend API quota without the user asking.
    try:
        recovered = recover_interrupted_documents()
        if recovered > 0:
            logger.info("Recovered %d interrupted document(s)", recovered)
    except Exception as exc:
        logger.error("Failed to recover interrupted documents: %s", exc)

    yield  # the app is now running and serving requests

    # --- Shutdown ---
    # Close all connections in the pool cleanly so Postgres doesn't see
    # abrupt disconnects.
    if db.pool is not None:
        db.pool.close()
    print("✓ Database pool closed.")


# ---------------------------------------------------------------------------
# App instance
# ---------------------------------------------------------------------------

app = FastAPI(
    title="DocKeeper",
    description="Personal document Q&A backend using RAG.",
    version="0.2.0",
    lifespan=lifespan,
)

# Register the documents router.
# All routes defined in routes_documents.py become active here.
# Using include_router keeps main.py clean as the app grows — each feature
# area gets its own route file and is plugged in with one line.
app.include_router(documents_router)
app.include_router(chat_router)   # /chat and /search/hybrid

# Serve the UI files at /static/*.
# Must be mounted AFTER routers so API routes take priority over static files.
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Root route — serves the single-page UI
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def serve_ui():
    """Return index.html for GET /. include_in_schema=False hides it from /docs."""
    return FileResponse(_STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health_check():
    """
    Check that the API is running and that Postgres is reachable.

    Returns HTTP 200 with a JSON summary on success,
    or HTTP 503 with an error message if the database is down.

    Why do real queries instead of just returning {"status": "ok"}?
    A health endpoint that doesn't actually touch the database gives a false
    sense of security.  Monitoring tools use /health to decide whether to
    route traffic here, so it must reflect the true state of the system.
    """
    try:
        with db.get_connection() as conn:
            with conn.cursor() as cur:

                # 1. Basic connectivity check
                cur.execute("SELECT 1")

                # 2. Confirm the pgvector extension is installed.
                #    We check pg_extension (the catalogue of installed extensions)
                #    rather than just attempting a vector operation so that the
                #    error message is clear and human-readable.
                cur.execute(
                    "SELECT COUNT(*) FROM pg_extension WHERE extname = 'vector'"
                )
                vector_count = cur.fetchone()[0]
                pgvector_status = "installed" if vector_count > 0 else "missing"

                # 3. List tables in the public schema that belong to this app.
                #    information_schema.tables is the standard, portable way to
                #    query table metadata in any SQL database.
                cur.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_type = 'BASE TABLE'
                    ORDER BY table_name
                    """
                )
                tables = [row[0] for row in cur.fetchall()]

        return {
            "status": "ok",
            "database": "connected",
            "pgvector": pgvector_status,
            "tables": tables,
        }

    except Exception as exc:
        # Return 503 Service Unavailable so load balancers and health monitors
        # know the service cannot currently handle requests.
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "database": "unreachable",
                "detail": str(exc),
            },
        )
