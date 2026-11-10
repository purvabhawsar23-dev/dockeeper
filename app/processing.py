"""
processing.py — Full document processing pipeline: parse → chunk → embed → store.

This module is called in the background after an upload.  It deliberately
never raises an unhandled exception — any error is caught, logged, and written
to the documents.error_message column so the user sees a clear status instead
of a silent failure.

Why a separate module?
  The pipeline touches parsers, chunker, embeddings, and the database.  Putting
  it all here keeps each other module focused on one concern, and makes it easy
  to call process_document() from both the background task and the manual
  /process endpoint.
"""

import logging
import pathlib

import app.db as db
from app.parsers import extract_text, extract_pdf
from app.chunker import chunk_pages
from app.embeddings import embed_documents

# pgvector's psycopg adapter lets us pass a plain Python list as a vector
# column value.  Without registering it, psycopg would not know how to
# serialise the list into the PostgreSQL vector wire format.
from pgvector.psycopg import register_vector

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

def _set_status(doc_id: int, status: str, error_message: str | None = None) -> None:
    """Write a new status (and optional error message) to the documents table."""
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE documents
                SET status = %s, error_message = %s
                WHERE id = %s
                """,
                (status, error_message, doc_id),
            )
        conn.commit()


def _set_page_count(doc_id: int, page_count: int) -> None:
    """Record how many pages were extracted, for display in the document list."""
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET page_count = %s WHERE id = %s",
                (page_count, doc_id),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_document(doc_id: int) -> None:
    """
    Run the full processing pipeline for one document:

      1. Mark status 'processing'.
      2. Fetch document metadata from the DB.
      3. Parse the file into pages with the appropriate parser.
      4. Detect scanned PDFs that need OCR.
      5. Split pages into overlapping chunks.
      6. Embed chunks in batches and INSERT after each batch so that progress
         is saved even if a later batch fails.
      7. Mark status 'ready' on success, or 'failed' with an error message.

    Safe to re-run: existing chunks for this document are deleted first so
    re-processing doesn't create duplicates.

    Never raises — all exceptions are caught and stored in error_message.
    """
    try:
        _set_status(doc_id, "processing")

        # --- Fetch document metadata ---
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT filename, file_path, file_type FROM documents WHERE id = %s",
                    (doc_id,),
                )
                row = cur.fetchone()

        if row is None:
            # Should never happen if called from the upload route, but guard anyway.
            logger.error("process_document: doc_id %d not found in database.", doc_id)
            return

        filename, file_path, file_type = row
        path = pathlib.Path(file_path)

        if not path.exists():
            raise FileNotFoundError(f"File not found on disk: {file_path}")

        # --- Parse ---
        logger.info("Processing doc %d (%s) — parsing ...", doc_id, filename)
        if file_type == "pdf":
            # Enable OCR during background processing so scanned pages can be transcribed.
            # Multi-page OCR is safe here as it runs asynchronously in the background task.
            pages = extract_pdf(path, allow_ocr=True)
        else:
            pages = extract_text(path, file_type)

        # Update page_count so the document list shows accurate info even
        # before embedding is complete.
        _set_page_count(doc_id, len(pages))

        # --- Chunk ---
        logger.info("Doc %d — chunking ...", doc_id)
        chunks = chunk_pages(pages, filename)
        logger.info("Doc %d — produced %d chunks.", doc_id, len(chunks))

        if not chunks:
            _set_status(doc_id, "failed", "No text could be extracted from this file.")
            return

        # --- Delete old chunks (makes re-processing safe / idempotent) ---
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM chunks WHERE document_id = %s", (doc_id,))
            conn.commit()

        # --- Embed and insert in batches ---
        # We insert after every batch rather than after all batches so that
        # if the process is interrupted mid-way, we don't lose all the work.
        from app.config import EMBED_BATCH_SIZE  # imported here to avoid circular import

        for batch_start in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch = chunks[batch_start: batch_start + EMBED_BATCH_SIZE]
            texts = [c["content"] for c in batch]

            logger.info(
                "Doc %d — embedding batch %d–%d ...",
                doc_id, batch_start, batch_start + len(batch) - 1,
            )
            vectors = embed_documents(texts)

            # Insert this batch into the DB
            with db.get_connection() as conn:
                # Register the pgvector adapter for this connection so psycopg
                # knows how to serialise Python lists as vector column values.
                register_vector(conn)

                with conn.cursor() as cur:
                    for chunk, vector in zip(batch, vectors):
                        cur.execute(
                            """
                            INSERT INTO chunks
                              (document_id, user_id, chunk_index, page_number,
                               section, content, embedding)
                            VALUES (%s, 1, %s, %s, %s, %s, %s)
                            """,
                            (
                                doc_id,
                                chunk["chunk_index"],
                                chunk["page_number"],
                                chunk.get("section", ""),
                                chunk["content"],
                                vector,
                            ),
                        )
                conn.commit()

            logger.info("Doc %d — batch saved (%d chunks so far).",
                        doc_id, batch_start + len(batch))

        # --- Done ---
        _set_status(doc_id, "ready")
        logger.info("Doc %d processing complete — status: ready.", doc_id)

    except Exception as exc:
        # Catch everything so the server never crashes due to a bad document.
        # Store the error as a short readable message so the user knows why it failed.
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.exception("Doc %d processing failed: %s", doc_id, error_msg)
        try:
            _set_status(doc_id, "failed", error_msg[:500])  # truncate for the DB column
        except Exception:
            pass  # if even the status update fails, just log and move on
