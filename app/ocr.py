"""
ocr.py — Image OCR using Gemini multimodal capabilities.

Why a dedicated module?
  Keeps image transcription logic and google-genai calls isolated from document
  parsers and database operations. If the OCR provider or vision model changes
  later, only this file needs to be updated.
"""

import re
import time
import logging
import random
import collections

from google import genai
from google.genai import types as genai_types

from app.config import (
    GEMINI_API_KEY,
    OCR_MODEL,
    OCR_REQUESTS_PER_MINUTE,
)

logger = logging.getLogger(__name__)

# Single shared Gemini client
_client = genai.Client(api_key=GEMINI_API_KEY)

# Transcription prompt instructed to transcribe exactly without commentary
OCR_INSTRUCTION = (
    "Transcribe all text in this image exactly as written, keeping the reading "
    "order and line breaks. Keep the original language (English, Hindi or a mix). "
    "Do not translate, summarise or add anything. If a table appears, write each "
    "row on its own line with cells separated by ' | '. If there is no readable "
    "text, reply with an empty string."
)

# Regex to parse 'retry after N seconds' from 429 rate limit responses
_RETRY_AFTER_RE = re.compile(r'retry.{0,20}?(\d+)\s*s', re.IGNORECASE)


def _parse_retry_after(error_str: str) -> float | None:
    """Extract retry-after seconds from error string if present."""
    match = _RETRY_AFTER_RE.search(error_str)
    return float(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Rate-limit tracker (Sliding 60-second window)
# ---------------------------------------------------------------------------
_request_timestamps: collections.deque = collections.deque()


def _requests_in_window() -> int:
    """Drop timestamps older than 60s and return current count in window."""
    cutoff = time.monotonic() - 60.0
    while _request_timestamps and _request_timestamps[0] < cutoff:
        _request_timestamps.popleft()
    return len(_request_timestamps)


def _throttle() -> None:
    """
    Block until sending another request will not exceed OCR_REQUESTS_PER_MINUTE.

    Why sliding-window throttling?
      Fixed 1-minute reset buckets allow rate limit spikes (e.g. sending requests
      at :59 and :01 consumes double the quota in 2 seconds). A sliding
      60-second window guarantees that no more than OCR_REQUESTS_PER_MINUTE
      calls occur across any continuous 60-second span.
    """
    while True:
        count = _requests_in_window()
        if count < OCR_REQUESTS_PER_MINUTE:
            break

        # Oldest call in window must age past 60s to free a request slot
        oldest_time = _request_timestamps[0]
        sleep_for = (oldest_time + 60.0) - time.monotonic() + 0.1
        if sleep_for > 0:
            logger.info(
                "OCR rate limit: %d/%d requests in window — sleeping %.1f s.",
                count,
                OCR_REQUESTS_PER_MINUTE,
                sleep_for,
            )
            time.sleep(sleep_for)

    _request_timestamps.append(time.monotonic())


# ---------------------------------------------------------------------------
# Public OCR API
# ---------------------------------------------------------------------------

def ocr_image(image_bytes: bytes, mime_type: str) -> str:
    """
    Transcribe text from image bytes using Gemini multimodal vision.

    Parameters
    ----------
    image_bytes : bytes
        Raw binary image data.
    mime_type : str
        MIME type of the image (e.g. 'image/png', 'image/jpeg').

    Returns
    -------
    str
        Extracted text stripped of surrounding whitespace.
    """
    max_retries = 6

    for attempt in range(max_retries + 1):
        try:
            _throttle()

            image_part = genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
            response = _client.models.generate_content(
                model=OCR_MODEL,
                contents=[image_part, OCR_INSTRUCTION],
            )

            text = response.text or ""
            return text.strip()

        except Exception as exc:
            error_str = str(exc)
            is_429 = "429" in error_str or "resource_exhausted" in error_str.lower()
            is_503_or_network = (
                "503" in error_str
                or "unavailable" in error_str.lower()
                or isinstance(exc, (ConnectionError, TimeoutError, OSError))
            )

            # Why retries with exponential backoff & jitter?
            # 429 indicates rate-limit exhaustion, requiring at least 60s for the quota
            # window to reset (or the explicit retry-after header value).
            # 503 and network errors indicate transient outages or connection blips;
            # exponential backoff with jitter prevents all retrying callers from hitting
            # the API simultaneously.
            if attempt == max_retries or not (is_429 or is_503_or_network):
                if attempt == max_retries and (is_429 or is_503_or_network):
                    raise RuntimeError(
                        f"OCR failed after {max_retries} retries: {exc}"
                    ) from exc
                raise

            if is_429:
                hint = _parse_retry_after(error_str)
                wait = max(hint, 60.0) if hint else 60.0
                logger.warning(
                    "OCR rate limited (attempt %d/%d) — waiting %.1f s before retry.",
                    attempt + 1,
                    max_retries,
                    wait,
                )
                time.sleep(wait)

            elif is_503_or_network:
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    "OCR 503/network error (attempt %d/%d): %s — retrying in %.1f s.",
                    attempt + 1,
                    max_retries,
                    exc,
                    wait,
                )
                time.sleep(wait)

    raise RuntimeError("ocr_image: unreachable.")
