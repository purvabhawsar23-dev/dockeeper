"""
config.py — Application settings loaded from the .env file.

Why python-dotenv?
  It reads key=value pairs from .env into os.environ so the rest of the app
  can use os.getenv() without knowing whether values came from the shell or
  the file.  This keeps secrets out of source code.
"""

import os
from dotenv import load_dotenv

# load_dotenv() reads .env (if it exists) into os.environ.
# It does NOT overwrite variables that are already set in the shell,
# so real environment variables always take priority over the file.
load_dotenv()


# ---------------------------------------------------------------------------
# Individual settings — straightforward variables, easy to import anywhere.
# ---------------------------------------------------------------------------

POSTGRES_USER: str     = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD: str = os.getenv("POSTGRES_PASSWORD", "")
POSTGRES_DB: str       = os.getenv("POSTGRES_DB", "dockeeper")
POSTGRES_HOST: str     = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT: str     = os.getenv("POSTGRES_PORT", "5432")

GEMINI_API_KEY: str    = os.getenv("GEMINI_API_KEY", "")

# ---------------------------------------------------------------------------
# Embedding settings
# ---------------------------------------------------------------------------

# Which Gemini embedding model to use. gemini-embedding-001 is the current
# recommended model for retrieval tasks.
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")

# Output vector length. Must match the vector(768) column in the schema.
# If you change the model you must also update the schema and re-embed everything.
EMBEDDING_DIM: int = int(os.getenv("EMBEDDING_DIM", "768"))

# How many texts to send in a single API call. Larger batches are more
# efficient but use more tokens per request.
EMBED_BATCH_SIZE: int = int(os.getenv("EMBED_BATCH_SIZE", "50"))

# Rate limit: tokens per minute allowed by the Gemini free tier.
# The embedder sleeps when this would be exceeded to avoid 429 errors.
EMBED_TOKENS_PER_MINUTE: int = int(os.getenv("EMBED_TOKENS_PER_MINUTE", "25000"))

# ---------------------------------------------------------------------------
# Upload settings
# ---------------------------------------------------------------------------

# Maximum allowed upload size in megabytes. Kept in config so it's easy to
# change without hunting through route code.
MAX_UPLOAD_MB: int = int(os.getenv("MAX_UPLOAD_MB", "200"))

# Where uploaded files are stored on disk. pathlib.Path is used throughout
# the app for safe, cross-platform path operations.
import pathlib
UPLOAD_DIR: pathlib.Path = pathlib.Path(os.getenv("UPLOAD_DIR", "uploads"))

# ---------------------------------------------------------------------------
# Chat / retrieval settings
# ---------------------------------------------------------------------------

# Which Gemini model to use for answer generation.
CHAT_MODEL: str = os.getenv("CHAT_MODEL", "gemini-2.0-flash")

# Total candidates fetched from each search arm (vector + keyword) before
# they are merged and re-ranked. Larger = better recall, slower query.
RETRIEVE_K: int = int(os.getenv("RETRIEVE_K", "30"))

# How many results to keep after RRF re-ranking and pass to the LLM.
FINAL_K: int = int(os.getenv("FINAL_K", "8"))

# RRF smoothing constant. Each chunk scores sum(1 / (RRF_K + rank)).
# 60 is the standard default from the original RRF paper.
RRF_K: int = int(os.getenv("RRF_K", "60"))

# Relevance gate: if no result meets this cosine similarity threshold AND
# no result appeared in the keyword search, we skip the LLM entirely and
# return a "not found" message.
MIN_SIMILARITY: float = float(os.getenv("MIN_SIMILARITY", "0.58"))

# Hard cap on total characters sent to the LLM as context. Prevents
# exceeding the model's context window for documents with many chunks.
MAX_CONTEXT_CHARS: int = int(os.getenv("MAX_CONTEXT_CHARS", "24000"))


def get_database_url() -> str:
    """
    Build a psycopg (v3) connection string from the individual settings.

    Format:  postgresql://user:password@host:port/dbname
    Having a single function that constructs the URL means we only update
    one place if the format ever needs to change (e.g. to add SSL params).
    """
    return (
        f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
        f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
    )
