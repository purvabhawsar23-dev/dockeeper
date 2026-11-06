"""
llm.py — A thin wrapper around the Gemini chat/generation API.

Why a wrapper module?
  Same reason as embeddings.py: all google-genai imports live here.
  If we swap the LLM provider later, only this file changes.  The rest of
  the app calls generate_answer() and never touches the SDK directly.
"""

import re
import time
import logging
import random

from google import genai
from google.genai import types as genai_types

from app.config import GEMINI_API_KEY, CHAT_MODEL

logger = logging.getLogger(__name__)

# Single shared client — same reasoning as embeddings.py.
_client = genai.Client(api_key=GEMINI_API_KEY)

# Pattern to extract "retry after N seconds" from 429 error messages.
_RETRY_AFTER_RE = re.compile(r'retry.{0,20}?(\d+)\s*s', re.IGNORECASE)


def _parse_retry_after(error_str: str) -> float | None:
    match = _RETRY_AFTER_RE.search(error_str)
    return float(match.group(1)) if match else None


def generate_answer(system_prompt: str, user_prompt: str) -> str:
    """
    Send a system prompt + user prompt to the chat model and return the text.

    Retry policy (mirrors embeddings.py for consistency):
      - 429 rate limit:  wait at least 60 s, or longer if the error says so.
      - 503 / network:   exponential backoff with jitter (2^attempt + noise).
      - Other errors:    re-raise immediately (not transient).
      - Max retries: 6.  Raises RuntimeError if all retries fail.
    """
    max_retries = 6

    for attempt in range(max_retries + 1):
        try:
            response = _client.models.generate_content(
                model=CHAT_MODEL,
                contents=user_prompt,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    # temperature=0 would make answers deterministic but can
                    # make the model overly terse; leave at the model default.
                ),
            )
            return response.text

        except Exception as exc:
            error_str = str(exc)
            is_429 = "429" in error_str
            is_503 = "503" in error_str or "unavailable" in error_str.lower()

            if attempt == max_retries:
                raise RuntimeError(
                    f"LLM call failed after {max_retries} retries: {exc}"
                ) from exc

            if is_429:
                hint = _parse_retry_after(error_str)
                wait = max(hint, 60.0) if hint else 60.0
                logger.warning(
                    "LLM rate limited (attempt %d/%d) — waiting %.0f s.",
                    attempt + 1, max_retries, wait,
                )
                time.sleep(wait)

            elif is_503:
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    "LLM 503/network error (attempt %d/%d): %s — retrying in %.1f s.",
                    attempt + 1, max_retries, exc, wait,
                )
                time.sleep(wait)

            else:
                raise  # not a transient error — don't retry

    raise RuntimeError("generate_answer: unreachable.")  # satisfies type checkers
