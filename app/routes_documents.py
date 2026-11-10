"""
routes_documents.py — HTTP routes for document upload and management.

Uses FastAPI's APIRouter so these routes are defined here and registered
in main.py with a single include_router() call.  This keeps main.py short
and lets us add more route files in later steps without touching the core
app setup.
"""

import uuid
import pathlib

from fastapi import APIRouter, BackgroundTasks, UploadFile, File, HTTPException, Query
from fastapi.responses import JSONResponse

import app.db as db
from app.config import MAX_UPLOAD_MB, MAX_IMAGE_MB, UPLOAD_DIR
from app.parsers import extract_text, SUPPORTED_TYPES, IMAGE_MIME_TYPES
from app.processing import process_document

router = APIRouter()

# Maximum allowed file sizes in bytes.
# Images have a smaller 10 MB limit because vision OCR models process images
# uncompressed and we want to preserve API quota and prevent timeout issues.
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_IMAGE_BYTES  = MAX_IMAGE_MB * 1024 * 1024


# ---------------------------------------------------------------------------
# POST /upload
# ---------------------------------------------------------------------------

@router.post("/upload", status_code=201)
async def upload_document(file: UploadFile = File(...),
                          background_tasks: BackgroundTasks = None):
    """
    Accept a single file upload, stream it to disk, and record it in the DB.

    Steps:
      1. Validate the file type against the allowed list.
      2. Generate a safe, unique filename (UUID + original extension).
      3. Stream the upload to disk in 1 MB chunks, counting bytes as we go.
         Abort with 413 and delete the partial file if the limit is exceeded.
      4. Insert a row into the documents table with status 'uploaded'.
      5. Return the new document's id and metadata.
    """
    # --- 1. File type validation ---
    # Use the original filename's extension as the file type.
    # We do NOT trust the Content-Type header because browsers and tools
    # often set it incorrectly (e.g. application/octet-stream for everything).
    original_name = file.filename or "unknown"
    suffix = pathlib.Path(original_name).suffix.lstrip(".").lower()

    if suffix not in SUPPORTED_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"File type '.{suffix}' is not supported. "
                f"Allowed types: {', '.join(SUPPORTED_TYPES)}"
            ),
        )

    # Images use MAX_IMAGE_MB to keep Gemini OCR payloads and quota reasonable;
    # other documents use MAX_UPLOAD_MB.
    is_image = suffix in IMAGE_MIME_TYPES
    max_bytes = MAX_IMAGE_BYTES if is_image else MAX_UPLOAD_BYTES
    max_mb = MAX_IMAGE_MB if is_image else MAX_UPLOAD_MB

    # --- 2. Generate a safe stored filename ---
    # A UUID prefix guarantees uniqueness even if two users upload a file
    # with the same name.  Using only the extension (not the original name)
    # avoids path traversal attacks (e.g. a filename like "../../etc/passwd").
    stored_name = f"{uuid.uuid4().hex}.{suffix}"
    file_path = UPLOAD_DIR / stored_name

    # --- 3. Stream to disk in 1 MB chunks ---
    # Why stream instead of reading the whole file into memory?
    # A 200 MB file read all at once uses 200 MB of RAM for that one request.
    # With concurrent uploads this would exhaust server memory fast.
    # Streaming writes each small chunk straight to disk, keeping memory use
    # flat no matter how large the file is.
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    CHUNK_SIZE = 1024 * 1024  # 1 MB per read — small enough to keep RAM low
    bytes_written = 0

    try:
        with file_path.open("wb") as dest:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break  # end of upload

                bytes_written += len(chunk)

                # Check the running total *before* writing so we never store
                # more than the limit on disk, even partially.
                if bytes_written > max_bytes:
                    # Close the file handle before deleting (required on Windows).
                    dest.close()
                    file_path.unlink(missing_ok=True)
                    type_label = "Image" if is_image else "File"
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"{type_label} exceeds the {max_mb} MB limit. "
                            f"Upload aborted and partial file deleted."
                        ),
                    )

                dest.write(chunk)

    except HTTPException:
        raise  # already a clean error — let FastAPI handle it
    except OSError as exc:
        file_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500,
            detail=f"Could not save file to disk: {exc}",
        )

    # --- 5. Insert DB row ---
    try:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO documents (filename, file_path, file_type, status)
                    VALUES (%s, %s, %s, 'uploaded')
                    RETURNING id, filename, file_type, status, created_at
                    """,
                    (original_name, str(file_path), suffix),
                )
                row = cur.fetchone()
            conn.commit()
    except Exception as exc:
        # If the DB insert fails, remove the file we just wrote so disk and
        # database don't get out of sync.
        file_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500,
            detail=f"Database error while recording upload: {exc}",
        )

    # --- Return the new document record and kick off background processing ---
    doc_id, filename, file_type, status, created_at = row

    # BackgroundTasks runs process_document() after the HTTP response is sent.
    # The upload endpoint returns immediately (status 201) so the user isn't
    # blocked waiting for potentially slow embedding API calls.
    background_tasks.add_task(process_document, doc_id)

    return {
        "id": doc_id,
        "filename": filename,
        "file_type": file_type,
        "status": status,
        "created_at": created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /documents
# ---------------------------------------------------------------------------

@router.get("/documents")
def list_documents():
    """
    Return all documents, newest first.

    Returning a stable, predictable list lets the frontend (or test scripts)
    show upload history and pick document IDs for preview or deletion.
    """
    try:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, filename, file_type, status, page_count, created_at
                    FROM documents
                    ORDER BY created_at DESC
                    """
                )
                rows = cur.fetchall()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    return [
        {
            "id": row[0],
            "filename": row[1],
            "file_type": row[2],
            "status": row[3],
            "page_count": row[4],
            "created_at": row[5].isoformat(),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# GET /documents/{id}/preview  [DEBUG ENDPOINT]
# This endpoint exists only to verify that text extraction is working during
# development.  It will be removed or restricted once chunking is implemented.
# ---------------------------------------------------------------------------

@router.get("/documents/{doc_id}/preview")
def preview_document(doc_id: int):
    """
    Run the parser on the saved file and return the first 500 characters of
    each extracted page so you can visually verify that extraction is correct.
    """
    # --- Fetch the document row ---
    try:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, filename, file_path, file_type FROM documents WHERE id = %s",
                    (doc_id,),
                )
                row = cur.fetchone()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    if row is None:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found.")

    doc_id_db, filename, file_path, file_type = row
    path = pathlib.Path(file_path)

    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"File not found on disk: {file_path}",
        )

    # For image files, do NOT call OCR again as it consumes API quota.
    # Instead, return the stored chunk text from the chunks table.
    if file_type in IMAGE_MIME_TYPES:
        try:
            with db.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT page_number, content
                        FROM chunks
                        WHERE document_id = %s
                        ORDER BY chunk_index ASC
                        """,
                        (doc_id,),
                    )
                    chunk_rows = cur.fetchall()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Database error: {exc}")

        if not chunk_rows:
            preview_pages = [{
                "page": 1,
                "preview": "(Processing in progress or failed — no chunks stored yet)",
            }]
            pages_count = 0
        else:
            preview_pages = [
                {
                    "page": r[0],
                    "preview": r[1][:500],
                }
                for r in chunk_rows
            ]
            pages_count = len(preview_pages)

        return {
            "id": doc_id_db,
            "filename": filename,
            "file_type": file_type,
            "pages_extracted": pages_count,
            "pages": preview_pages,
        }

    # --- Parse the file ---
    try:
        pages = extract_text(path, file_type)
    except ValueError as exc:
        # Unsupported type — shouldn't happen if upload validation worked,
        # but handle it cleanly just in case.
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Could not parse file '{filename}': {exc}",
        )

    # Return only the first 500 characters per page — enough to confirm the
    # right text is coming out without flooding the response with huge payloads.
    preview_pages = []
    for p in pages:
        entry = {
            "page": p["page"],
            "preview": p["text"][:500],
        }
        if "section" in p:
            entry["section"] = p["section"]
        if p.get("needs_ocr"):
            entry["needs_ocr"] = True
        preview_pages.append(entry)

    return {
        "id": doc_id_db,
        "filename": filename,
        "file_type": file_type,
        "pages_extracted": len(pages),
        "pages": preview_pages,
    }


# ---------------------------------------------------------------------------
# DELETE /documents/{id}
# ---------------------------------------------------------------------------

@router.delete("/documents/{doc_id}", status_code=200)
def delete_document(doc_id: int):
    """
    Delete a document row (chunks cascade automatically via the FK constraint)
    and remove the file from disk.

    We delete the DB row first.  If the file deletion then fails, the data is
    still consistent — the row is gone and we just have an orphaned file,
    which is recoverable.  The reverse order (file first) risks deleting the
    file but then failing the DB delete, leaving a broken DB record pointing
    to a missing file.
    """
    # --- Fetch the file path before deleting the row ---
    try:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT file_path FROM documents WHERE id = %s",
                    (doc_id,),
                )
                row = cur.fetchone()

                if row is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Document {doc_id} not found.",
                    )

                file_path = row[0]

                # DELETE cascades to chunks via the FK ON DELETE CASCADE
                # defined in the schema, so we only need to delete the parent row.
                cur.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
            conn.commit()
    except HTTPException:
        raise  # re-raise 404 without wrapping it
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    # --- Remove the file from disk ---
    # missing_ok=True means no error if the file was already gone.
    pathlib.Path(file_path).unlink(missing_ok=True)

    return {"deleted": True, "id": doc_id}


# ---------------------------------------------------------------------------
# POST /documents/{id}/process  — manually re-trigger processing
# ---------------------------------------------------------------------------

@router.post("/documents/{doc_id}/process", status_code=202)
def reprocess_document(doc_id: int, background_tasks: BackgroundTasks):
    """
    Re-run the full processing pipeline for a document that previously
    failed or needs to be re-embedded (e.g. after a model change).

    Returns 202 Accepted immediately; processing runs in the background.
    Existing chunks are deleted inside process_document() before new ones
    are inserted, so calling this multiple times is safe.
    """
    # Verify the document exists before queuing the background task
    try:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM documents WHERE id = %s", (doc_id,))
                row = cur.fetchone()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    if row is None:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found.")

    background_tasks.add_task(process_document, doc_id)
    return {"queued": True, "id": doc_id, "message": "Processing started in background."}


# ---------------------------------------------------------------------------
# GET /documents/{id}/status
# ---------------------------------------------------------------------------

@router.get("/documents/{doc_id}/status")
def document_status(doc_id: int):
    """
    Return the current processing status of a document, its error message
    (if any), and the number of chunks stored so far.

    Polling this endpoint lets a client show a live progress indicator while
    the background processing is running.
    """
    try:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                # Fetch document status in one query
                cur.execute(
                    """
                    SELECT d.id, d.filename, d.status, d.error_message, d.page_count,
                           COUNT(c.id) AS chunk_count
                    FROM documents d
                    LEFT JOIN chunks c ON c.document_id = d.id
                    WHERE d.id = %s
                    GROUP BY d.id
                    """,
                    (doc_id,),
                )
                row = cur.fetchone()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    if row is None:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found.")

    doc_id_db, filename, status, error_message, page_count, chunk_count = row
    return {
        "id": doc_id_db,
        "filename": filename,
        "status": status,
        "error_message": error_message,
        "page_count": page_count,
        "chunks_stored": chunk_count,
    }


# ---------------------------------------------------------------------------
# GET /search?q=...&limit=5  [TEMPORARY DEBUG ENDPOINT]
# This endpoint is for development only — it verifies that embedding and
# vector search are working end-to-end.  It will be replaced by a proper
# chat/query endpoint in a later step.
# ---------------------------------------------------------------------------

@router.get("/search")
def vector_search(
    q: str = Query(..., description="Search query text"),
    limit: int = Query(5, ge=1, le=50, description="Number of results to return"),
):
    """
    Embed the query and return the nearest chunks by cosine distance.

    The <=> operator in pgvector computes cosine distance (lower = more similar).
    We convert it to a similarity score with 1 - distance for readability.
    """
    # Import here to avoid loading the Gemini client at module import time
    # when the API key might not yet be set.
    from app.embeddings import embed_query
    from pgvector.psycopg import register_vector

    try:
        query_vector = embed_query(q)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Embedding API error: {exc}",
        )

    try:
        with db.get_connection() as conn:
            register_vector(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        c.id,
                        d.filename,
                        c.page_number,
                        c.section,
                        c.content,
                        1 - (c.embedding <=> %s::vector) AS similarity
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE c.embedding IS NOT NULL
                    ORDER BY c.embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (query_vector, query_vector, limit),
                )
                rows = cur.fetchall()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    return {
        "query": q,
        "results": [
            {
                "chunk_id":    row[0],
                "filename":    row[1],
                "page_number": row[2],
                "section":     row[3],
                # Preview: first 300 chars — enough to judge relevance at a glance
                "preview":     row[4][:300],
                "similarity":  round(float(row[5]), 4),
            }
            for row in rows
        ],
    }
