"""
embeddings.py — A thin wrapper around the Gemini embedding API.

Why a wrapper module?
  It keeps all google-genai imports in one place.  If we ever swap the
  embedding provider (e.g. to a local model), we only change this file —
  the rest of the app calls embed_documents() and embed_query() and never
  needs to know which model is underneath.

Why L2-normalise?
  The Gemini API pre-normalises only its 3072-dim output, not the 768-dim
  output we request via output_dimensionality.  Cosine similarity is
  equivalent to dot-product on unit vectors, so normalising here lets
  pgvector's <=> operator (cosine distance) return correct results.
"""

import re
import time
import logging
import random
import collections

import numpy as np
from google import genai
from google.genai import types as genai_types

from app.config import (
    GEMINI_API_KEY,
    EMBEDDING_MODEL,
    EMBEDDING_DIM,
    EMBED_BATCH_SIZE,
    EMBED_TOKENS_PER_MINUTE,
)

logger = logging.getLogger(__name__)

# Initialise the Gemini client once at import time.
# Creating a client per request would be wasteful (re-reads config, opens
# connections) and would make rate-limit tracking across requests impossible.
_client = genai.Client(api_key=GEMINI_API_KEY)


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def _estimate_tokens(texts: list[str]) -> int:
    """
    Estimate how many tokens a list of texts will consume.

    We use len(text) / 3 rather than / 4 — more conservative, meaning we
    sleep sooner and avoid 429s rather than hitting them and having to retry.
    No exact tokeniser is used because that would add a heavy dependency for
    a rough accounting number.
    """
    return sum(len(t) for t in texts) // 3


# ---------------------------------------------------------------------------
# Sliding-window rate limiter
# ---------------------------------------------------------------------------
# Instead of a single fixed 60-second window that resets all at once, we keep
# a deque of (timestamp, tokens) records for every batch sent in the last 60
# seconds.  This is a true sliding window: as old records age out, their tokens
# are freed up, so we never over-sleep.

_sent_log: collections.deque = collections.deque()   # (monotonic_time, tokens)


def _tokens_in_window() -> int:
    """Sum up tokens sent within the last 60 seconds, dropping stale entries."""
    cutoff = time.monotonic() - 60.0
    while _sent_log and _sent_log[0][0] < cutoff:
        _sent_log.popleft()
    return sum(tokens for _, tokens in _sent_log)


def _throttle(texts: list[str]) -> None:
    """
    Block until sending `texts` would not exceed EMBED_TOKENS_PER_MINUTE.

    Uses a sliding 60-second window so the sleep duration is exactly as long
    as needed — never more.

    If a single batch alone exceeds the per-minute limit (e.g. very long
    texts), this function still returns immediately after the window clears,
    because there is no point sleeping longer — the batch must be sent as-is.
    The caller (_send_sub_batches) handles splitting oversized batches before
    they reach here.
    """
    estimated = _estimate_tokens(texts)

    while True:
        used = _tokens_in_window()
        if used + estimated <= EMBED_TOKENS_PER_MINUTE:
            break  # enough room in the window — proceed

        # Find the oldest entry; sleeping until it ages out of the window
        # frees exactly those tokens.
        if _sent_log:
            oldest_time = _sent_log[0][0]
            sleep_for = (oldest_time + 60.0) - time.monotonic() + 0.1  # +0.1 margin
            if sleep_for > 0:
                logger.info(
                    "Rate limit: window has %d/%d tokens used — sleeping %.1f s.",
                    used, EMBED_TOKENS_PER_MINUTE, sleep_for,
                )
                time.sleep(sleep_for)
        else:
            # Window is empty but batch alone exceeds limit — can't split here;
            # just proceed and let the API handle it (or caller split first).
            break

    # Record this batch in the window
    _sent_log.append((time.monotonic(), estimated))


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

# Pattern to extract a "retry after N seconds" hint from 429 error messages.
# The Gemini API sometimes includes this in the error body.
_RETRY_AFTER_RE = re.compile(r'retry.{0,20}?(\d+)\s*s', re.IGNORECASE)


def _parse_retry_after(error_str: str) -> float | None:
    """
    Try to read a 'retry after N seconds' value from a 429 error message.
    Returns the number of seconds if found, else None.
    """
    match = _RETRY_AFTER_RE.search(error_str)
    if match:
        return float(match.group(1))
    return None


def _call_with_retry(texts: list[str], task_type: str) -> list[list[float]]:
    """
    Call the Gemini embedding API with smart retry logic:

    - 429 (rate limited): wait at least 60 s, or use the retry-after hint
      from the error message if it is longer.  Up to 8 retries.
    - 503 / network errors: exponential backoff with jitter (2^attempt + noise).
      Up to 8 retries.
    - Any other exception: re-raise immediately (not retryable).

    _throttle() is called before every attempt so we never send more than
    EMBED_TOKENS_PER_MINUTE even across retries.
    """
    max_retries = 8

    for attempt in range(max_retries + 1):
        try:
            _throttle(texts)

            response = _client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=texts,
                config=genai_types.EmbedContentConfig(
                    task_type=task_type,
                    output_dimensionality=EMBEDDING_DIM,
                ),
            )

            return [e.values for e in response.embeddings]

        except Exception as exc:
            error_str = str(exc)
            is_429 = "429" in error_str
            is_503 = "503" in error_str or "unavailable" in error_str.lower()

            if attempt == max_retries:
                raise  # out of retries

            if is_429:
                # Respect the API's own retry-after hint if present;
                # otherwise wait a full 60 s (one rate-limit window).
                hint = _parse_retry_after(error_str)
                wait = max(hint, 60.0) if hint else 60.0
                logger.warning(
                    "Rate limited (attempt %d/%d) — waiting %.0f s.",
                    attempt + 1, max_retries, wait,
                )
                time.sleep(wait)

            elif is_503:
                # Transient server error — exponential backoff with jitter so
                # concurrent callers don't all retry at the same instant.
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    "503 / network error (attempt %d/%d): %s — retrying in %.1f s.",
                    attempt + 1, max_retries, exc, wait,
                )
                time.sleep(wait)

            else:
                raise  # unknown error — don't retry

    raise RuntimeError("Embedding failed after maximum retries.")  # unreachable


# ---------------------------------------------------------------------------
# Batch splitter for oversized batches
# ---------------------------------------------------------------------------

def _send_sub_batches(texts: list[str], task_type: str) -> list[list[float]]:
    """
    Send `texts` to the API, automatically splitting into smaller pieces if
    the batch alone would exceed EMBED_TOKENS_PER_MINUTE.

    Why split here rather than just relying on _throttle?
      _throttle can make us wait, but if a single batch is so large that it
      exceeds the limit on its own, we would wait forever.  Splitting first
      means each sub-batch is guaranteed to be sendable within one window.
    """
    # If the batch fits within the limit, send it directly.
    if _estimate_tokens(texts) <= EMBED_TOKENS_PER_MINUTE:
        return _call_with_retry(texts, task_type)

    # Batch is too large — split in half and recurse until pieces fit.
    # This handles pathological cases like a single text that is itself huge.
    mid = len(texts) // 2
    if mid == 0:
        # Single text that is larger than the limit — send anyway and let
        # the server decide; we cannot split a single text here.
        return _call_with_retry(texts, task_type)

    left  = _send_sub_batches(texts[:mid], task_type)
    right = _send_sub_batches(texts[mid:], task_type)
    return left + right


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _l2_normalize(vector: list[float]) -> list[float]:
    """
    Scale a vector to unit length (L2 norm = 1).

    After normalisation, cosine_similarity(a, b) = dot(a, b), which is what
    pgvector's <=> (cosine distance) operator computes efficiently.
    If the vector is all zeros (shouldn't happen, but guard anyway), return it
    unchanged to avoid a division-by-zero.
    """
    arr = np.array(vector, dtype=np.float32)
    norm = np.linalg.norm(arr)
    if norm == 0:
        return vector
    return (arr / norm).tolist()


def _verify_and_normalize(vectors: list[list[float]]) -> list[list[float]]:
    """Check every vector has the expected dimension, then normalise."""
    result = []
    for i, vec in enumerate(vectors):
        if len(vec) != EMBEDDING_DIM:
            raise ValueError(
                f"Vector {i} has length {len(vec)}, expected {EMBEDDING_DIM}. "
                f"Check EMBEDDING_MODEL and EMBEDDING_DIM in config."
            )
        result.append(_l2_normalize(vec))
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def embed_documents(texts: list[str]) -> list[list[float]]:
    """
    Embed a list of document texts for storage.

    Uses task_type RETRIEVAL_DOCUMENT, which tells the model these are
    passages to be indexed (not queries).  Using the right task type
    measurably improves retrieval quality.

    Sends texts in batches of EMBED_BATCH_SIZE.  Each batch is checked
    against the sliding token window and split further if it would exceed
    EMBED_TOKENS_PER_MINUTE on its own.

    Logs "embedded X of Y chunks" after every batch so you can watch
    progress in the uvicorn console.

    Returns one 768-length unit-vector per input text, in the same order.
    """
    if not texts:
        return []

    total = len(texts)
    all_vectors: list[list[float]] = []

    for batch_start in range(0, total, EMBED_BATCH_SIZE):
        batch = texts[batch_start: batch_start + EMBED_BATCH_SIZE]

        raw = _send_sub_batches(batch, task_type="RETRIEVAL_DOCUMENT")
        all_vectors.extend(_verify_and_normalize(raw))

        logger.info(
            "Embedded %d of %d chunks.",
            min(batch_start + len(batch), total),
            total,
        )

    return all_vectors


def embed_query(text: str) -> list[float]:
    """
    Embed a single user query for similarity search.

    Uses task_type RETRIEVAL_QUERY, which is the asymmetric counterpart to
    RETRIEVAL_DOCUMENT.  The model optimises the query vector to match well
    against document vectors generated with RETRIEVAL_DOCUMENT.
    Returns one 768-length unit-vector.
    """
    raw = _call_with_retry([text], task_type="RETRIEVAL_QUERY")
    return _verify_and_normalize(raw)[0]
