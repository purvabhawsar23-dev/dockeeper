"""
chunker.py — Split extracted document pages into overlapping text chunks.

Why chunk at all?
  An LLM's context window has a token limit, so we can't feed it an entire
  document.  By splitting into ~600-token chunks with 100-token overlap we
  can retrieve only the relevant pieces at query time.  The overlap means
  a sentence that falls across a chunk boundary is still fully captured in
  at least one chunk.

Why NOT split across pages?
  Each page in the source (PDF page, Excel sheet, …) is a natural boundary.
  Keeping chunks within a single page means every chunk's page_number is
  accurate, which lets us show the user exactly where an answer came from.
"""

import re


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Target size in tokens. We estimate 1 token ≈ 4 characters (a rough but
# standard rule-of-thumb for English/Hindi mixed text).
TARGET_TOKENS = 600
TARGET_CHARS  = TARGET_TOKENS * 4   # ≈ 2400 characters

# How much the end of one chunk overlaps with the start of the next.
# Overlap preserves context for sentences that straddle a boundary.
OVERLAP_TOKENS = 100
OVERLAP_CHARS  = OVERLAP_TOKENS * 4  # ≈ 400 characters

# Sentence-ending punctuation. Includes the Hindi danda (।) so sentences in
# Hindi are also treated as natural split points.
SENTENCE_END = re.compile(r'(?<=[।?!.])\s+')


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _split_into_sentences(text: str) -> list[str]:
    """
    Split text on sentence boundaries, keeping the punctuation attached.

    re.split with a lookbehind keeps the delimiter (. ! ? ।) at the end of
    the preceding sentence rather than discarding it.
    """
    parts = SENTENCE_END.split(text)
    # Filter out blank strings that can appear after a split
    return [p for p in parts if p.strip()]


def _split_into_paragraphs(text: str) -> list[str]:
    """
    Split on one or more blank lines — the standard paragraph separator in
    plain text, Markdown, and most document-to-text converters.
    """
    parts = re.split(r'\n\s*\n', text)
    return [p.strip() for p in parts if p.strip()]


def _hard_cut(text: str, max_chars: int) -> list[str]:
    """
    Last-resort split: cut at max_chars boundaries regardless of structure.

    Used only when a single paragraph or sentence exceeds TARGET_CHARS
    (e.g. a table cell with thousands of characters and no punctuation).
    """
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def _make_segments(text: str) -> list[str]:
    """
    Break page text into segments small enough to be assembled into chunks.

    Strategy (from coarsest to finest):
      1. Paragraph breaks  — most natural boundary, preserves meaning
      2. Sentence ends     — finer boundary, still readable
      3. Hard cut          — last resort for pathological text
    """
    segments: list[str] = []

    for para in _split_into_paragraphs(text):
        if len(para) <= TARGET_CHARS:
            # Paragraph fits in one chunk — keep it whole
            segments.append(para)
        else:
            # Paragraph is too long; split into sentences
            for sentence in _split_into_sentences(para):
                if len(sentence) <= TARGET_CHARS:
                    segments.append(sentence)
                else:
                    # Sentence itself is too long — hard cut
                    segments.extend(_hard_cut(sentence, TARGET_CHARS))

    return segments


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def chunk_pages(pages: list[dict], filename: str) -> list[dict]:
    """
    Convert a list of parsed pages into a flat list of overlapping chunks.

    Parameters
    ----------
    pages    : output of any extract_* function in parsers.py
    filename : original document name, prefixed into each chunk so the
               filename itself becomes searchable in full-text queries.

    Returns
    -------
    List of dicts with keys:
        chunk_index  – sequential integer across the whole document
        page_number  – source page (accurate because we never cross pages)
        section      – sheet/section name from xlsx, else empty string
        content      – the text to embed and store
    """
    chunks: list[dict] = []
    chunk_index = 0

    # A short header prepended to every chunk. This means a query like
    # "lecture notes" can match the filename even if the words don't appear
    # in the body text, and it gives the LLM document context at retrieval time.
    header = f"Document: {filename}\n\n"

    for page in pages:
        page_number = page["page"]
        section     = page.get("section", "")
        text        = page.get("text", "").strip()

        if not text:
            continue  # skip empty pages (scanned PDFs, blank sheets, etc.)

        segments = _make_segments(text)
        if not segments:
            continue

        # Sliding-window assembly: accumulate segments until we'd exceed the
        # target size, then emit a chunk and rewind by OVERLAP_CHARS worth
        # of text so the next chunk starts with some repeated context.
        buffer = ""

        for seg in segments:
            # Would adding this segment exceed the target?
            if buffer and len(buffer) + len(seg) + 1 > TARGET_CHARS:
                # Emit the current buffer as a chunk
                content = header + buffer.strip()
                chunks.append({
                    "chunk_index": chunk_index,
                    "page_number": page_number,
                    "section":     section,
                    "content":     content,
                })
                chunk_index += 1

                # Keep the tail of the buffer as overlap for the next chunk.
                # This ensures a sentence straddling the boundary is in both chunks.
                buffer = buffer[-OVERLAP_CHARS:] + "\n" + seg
            else:
                # Segment fits — append it (space-separated)
                buffer = (buffer + "\n" + seg).strip() if buffer else seg

        # Emit whatever remains in the buffer as the last chunk for this page
        if buffer.strip():
            content = header + buffer.strip()
            chunks.append({
                "chunk_index": chunk_index,
                "page_number": page_number,
                "section":     section,
                "content":     content,
            })
            chunk_index += 1

    return chunks
