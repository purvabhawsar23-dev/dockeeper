"""
search.py — Vector search, keyword search, and hybrid RRF fusion.

Why hybrid search instead of just vector search?
  Vector search finds semantically similar chunks but misses exact keyword
  matches (e.g. a rare proper noun or a document ID).  Keyword search finds
  exact terms but misses paraphrases.  Combining both with Reciprocal Rank
  Fusion (RRF) captures the strengths of each without needing to tune weights.

Why RRF?
  RRF is score-free: it only uses rank positions, so it works even though
  vector similarity and BM25/ts_rank scores are on completely different scales.
  Formula: score = sum(1 / (RRF_K + rank)) over all lists the chunk appears in.
  A chunk that ranks #1 in both lists beats one that ranks #1 in only one.
"""

import re

import app.db as db
from app.config import RETRIEVE_K, RRF_K, FINAL_K
from pgvector.psycopg import register_vector


# ---------------------------------------------------------------------------
# Stop-word sets (module-level so they are compiled once, not per call)
# ---------------------------------------------------------------------------
_EN_STOP = {
    "what", "is", "the", "a", "an", "of", "about", "me", "tell",
    "how", "why", "does", "do", "in", "on", "to", "for", "and",
    "or", "are", "was", "i", "my", "you",
}
_HI_STOP = {
    "है", "हैं", "क्या", "का", "की", "के", "में", "से", "को",
    "और", "यह", "वह", "कि",
}
_STOP_WORDS = _EN_STOP | _HI_STOP


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------
# Every search function returns a list of dicts with at least these keys so
# the caller (routes_chat.py) can treat all results uniformly.
#
#   chunk_id        int
#   document_id     int
#   filename        str
#   page_number     int
#   section         str
#   content         str
#   similarity      float | None   (cosine similarity; None for keyword-only hits)


# ---------------------------------------------------------------------------
# Vector search
# ---------------------------------------------------------------------------

def vector_search(query_vec: list[float], k: int = RETRIEVE_K) -> list[dict]:
    """
    Return the top-k chunks nearest to query_vec by cosine distance.

    Uses the HNSW index on chunks.embedding via pgvector's <=> operator,
    which computes cosine distance (0 = identical, 2 = opposite).
    We convert to similarity with 1 - distance so higher = better.

    Only searches chunks whose parent document has status = 'ready' so we
    never surface partial or failed documents to the user.
    """
    with db.get_connection() as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    c.id            AS chunk_id,
                    c.document_id,
                    d.filename,
                    c.page_number,
                    c.section,
                    c.content,
                    1 - (c.embedding <=> %s::vector)  AS similarity
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.user_id = 1
                  AND d.status  = 'ready'
                  AND c.embedding IS NOT NULL
                ORDER BY c.embedding <=> %s::vector
                LIMIT %s
                """,
                (query_vec, query_vec, k),
            )
            rows = cur.fetchall()

    return [
        {
            "chunk_id":    row[0],
            "document_id": row[1],
            "filename":    row[2],
            "page_number": row[3],
            "section":     row[4],
            "content":     row[5],
            "similarity":  float(row[6]),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Keyword search
# ---------------------------------------------------------------------------

def extract_content_tokens(query: str) -> list[str]:
    """
    Tokenise a natural-language query into sanitised content words.

    Steps:
      1. Extract runs of word characters and Devanagari characters (strips ?, ।, etc.)
      2. Lowercase for English stop-word comparison.
      3. Remove stop words and single-character tokens.
      4. Verify each token is safe for to_tsquery (alphanumeric / Devanagari only).

    Returned tokens are ready to be joined with '|' or '&' and passed to
    to_tsquery('simple', ...).
    """
    raw   = re.findall(r'[\w\u0900-\u097F]+', query)
    lower = [t.lower() for t in raw]
    content = [t for t in lower if t not in _STOP_WORDS and len(t) > 1]
    # Final safety check: reject anything that could break tsquery syntax
    return [t for t in content if re.fullmatch(r'[\w\u0900-\u097F]+', t)]


def keyword_covers_all(chunk_ids: list[int], tokens: list[str]) -> set[int]:
    """
    Return the subset of chunk_ids whose tsv contains ALL of the given tokens.

    Why AND here when keyword_search uses OR?
      keyword_search uses OR to maximise recall — a chunk needs only one
      content word to surface.  Here we raise the bar: a chunk must contain
      EVERY content token so that a question like "refund policy" only passes
      the gate if a chunk has BOTH "refund" AND "policy", not just one of them.
      This prevents a single common word (e.g. "policy") appearing in an
      unrelated chunk from unlocking the LLM.

    Tokens are joined with '&' (AND) in to_tsquery, parameterized safely.
    Returns an empty set if tokens is empty (caller should check before calling).
    """
    if not tokens or not chunk_ids:
        return set()

    tsquery_str = " & ".join(tokens)   # AND — every token must be present

    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM chunks
                WHERE id = ANY(%s)
                  AND tsv @@ to_tsquery('simple', %s)
                """,
                (chunk_ids, tsquery_str),
            )
            rows = cur.fetchall()

    return {row[0] for row in rows}


def keyword_search(query: str, k: int = RETRIEVE_K) -> list[dict]:
    """
    Full-text search using the GIN-indexed tsv column on chunks.

    Why OR instead of AND?
      Questions contain stop words and question particles that won't appear in
      documents, so AND almost always returns nothing.  Joining content words
      with OR lets a chunk match if it contains ANY relevant term; ts_rank
      still sorts by how many tokens matched, so multi-term matches rank higher.

    Tokenisation and stop-word removal are handled by extract_content_tokens()
    so the same logic is reused by keyword_covers_all() without duplication.
    """
    safe_tokens = extract_content_tokens(query)

    if not safe_tokens:
        return []

    # Join with | (OR) — chunk matches if it contains ANY content word.
    tsquery_str = " | ".join(safe_tokens)

    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    c.id            AS chunk_id,
                    c.document_id,
                    d.filename,
                    c.page_number,
                    c.section,
                    c.content,
                    ts_rank(c.tsv, to_tsquery('simple', %s)) AS rank_score
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.user_id = 1
                  AND d.status  = 'ready'
                  AND c.tsv @@ to_tsquery('simple', %s)
                ORDER BY rank_score DESC
                LIMIT %s
                """,
                (tsquery_str, tsquery_str, k),
            )
            rows = cur.fetchall()

    return [
        {
            "chunk_id":    row[0],
            "document_id": row[1],
            "filename":    row[2],
            "page_number": row[3],
            "section":     row[4],
            "content":     row[5],
            "similarity":  None,   # keyword search has no cosine similarity
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Hybrid search via Reciprocal Rank Fusion
# ---------------------------------------------------------------------------

def hybrid_search(query: str, k: int = FINAL_K) -> list[dict]:
    """
    Run vector and keyword search in parallel, merge with RRF, return top k.

    Steps:
      1. Embed the query (RETRIEVAL_QUERY task type).
      2. Fetch up to RETRIEVE_K results from each search arm.
      3. Build a dict keyed by chunk_id.  For each list a chunk appears in,
         add 1 / (RRF_K + rank) to its running score.
      4. Sort by RRF score descending and return the top k.

    Each returned dict includes:
      rrf_score        float  — merged relevance score (higher = better)
      vector_rank      int|None
      keyword_rank     int|None
      vector_similarity float|None
      + all standard fields (chunk_id, filename, page_number, content, …)
    """
    # Import here to avoid loading the Gemini client at module import time
    # when the API key might not yet be set (e.g. during testing / health check).
    from app.embeddings import embed_query

    query_vec = embed_query(query)

    vec_results  = vector_search(query_vec, k=RETRIEVE_K)
    kw_results   = keyword_search(query, k=RETRIEVE_K)

    # --- Build the merged scoring table ---
    # merged maps chunk_id -> full result dict augmented with ranking info.
    merged: dict[int, dict] = {}

    for rank, result in enumerate(vec_results, start=1):
        cid = result["chunk_id"]
        merged[cid] = {
            **result,
            "rrf_score":        1.0 / (RRF_K + rank),
            "vector_rank":      rank,
            "keyword_rank":     None,
            "vector_similarity": result["similarity"],
        }

    for rank, result in enumerate(kw_results, start=1):
        cid = result["chunk_id"]
        rrf_contribution = 1.0 / (RRF_K + rank)

        if cid in merged:
            # Chunk appeared in both lists — add its keyword RRF contribution
            # to the existing vector contribution.
            merged[cid]["rrf_score"]    += rrf_contribution
            merged[cid]["keyword_rank"] = rank
        else:
            # Keyword-only hit — no vector similarity available.
            merged[cid] = {
                **result,
                "rrf_score":         rrf_contribution,
                "vector_rank":       None,
                "keyword_rank":      rank,
                "vector_similarity": None,
            }

    # Sort by RRF score (highest first) and return top k
    ranked = sorted(merged.values(), key=lambda r: r["rrf_score"], reverse=True)
    return ranked[:k]
