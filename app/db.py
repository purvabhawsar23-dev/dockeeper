"""
db.py — Database connection pool and schema initialisation.

Why a connection pool instead of opening a new connection per request?
  Opening a Postgres connection takes ~50–100 ms (TCP handshake, auth, etc.).
  A pool keeps a small number of connections open and reuses them across
  requests.  For a web app that receives many concurrent requests, this
  makes responses much faster and avoids exhausting Postgres's connection limit.
"""

import pathlib
import psycopg
from psycopg_pool import ConnectionPool

from app.config import get_database_url

# ---------------------------------------------------------------------------
# Module-level pool variable.
# It starts as None and is populated during FastAPI's startup lifespan.
# Using a module-level variable is the simplest way to share one pool across
# all request handlers without passing it around explicitly.
# ---------------------------------------------------------------------------
pool: ConnectionPool | None = None


def init_pool() -> None:
    """
    Create the connection pool.

    min_size=1  — keep at least one connection warm so the first request
                  doesn't wait for a new connection to be established.
    max_size=5  — cap at 5 connections; enough for development and light use
                  without overwhelming a small Postgres instance.
    """
    global pool
    pool = ConnectionPool(
        conninfo=get_database_url(),
        min_size=1,
        max_size=5,
        # open=True means the pool establishes its minimum connections right
        # now rather than lazily — we want to fail fast on startup if the DB
        # is unreachable.
        open=True,
    )


def init_db() -> None:
    """
    Read sql/schema.sql and execute it against the database.

    Running the schema on every startup is safe because every statement uses
    IF NOT EXISTS.  This means:
      - First run: creates the extension, tables, and indexes.
      - Later runs: finds everything already there and skips creation silently.
    No manual migration step needed in development.
    """
    # Build the path relative to this file so it works regardless of the
    # working directory the server was launched from.
    schema_path = pathlib.Path(__file__).parent.parent / "sql" / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


def get_connection():
    """
    Borrow a connection from the pool.

    Returns a context manager — use it with `with get_connection() as conn:`.
    The pool automatically returns the connection when the `with` block exits,
    even if an exception is raised.
    """
    if pool is None:
        raise RuntimeError("Database pool has not been initialised. Call init_pool() first.")
    return pool.connection()
