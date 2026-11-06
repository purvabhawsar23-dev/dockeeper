-- =============================================================================
-- DocKeeper Schema
-- Safe to run repeatedly: every statement uses IF NOT EXISTS.
-- =============================================================================

-- pgvector adds the VECTOR data type and similarity-search operators.
-- Without this extension the chunks.embedding column won't work.
CREATE EXTENSION IF NOT EXISTS vector;


-- -----------------------------------------------------------------------------
-- Table: documents
-- Tracks every file the user uploads.  Status moves through a small lifecycle:
--   uploaded → processing → ready (or failed on error)
-- Keeping a row per document lets us show upload history and retry failed ones.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    id            BIGSERIAL       PRIMARY KEY,
    user_id       INTEGER         NOT NULL DEFAULT 1,       -- future multi-user support
    filename      TEXT            NOT NULL,                 -- original file name shown in the UI
    file_path     TEXT            NOT NULL,                 -- path on disk where the file is stored
    file_type     TEXT,                                     -- e.g. 'pdf', 'txt' — used to pick the right parser
    category      TEXT,                                     -- user-assigned label, e.g. 'receipts', 'notes'
    status        TEXT            NOT NULL DEFAULT 'uploaded'
                                  CHECK (status IN ('uploaded','processing','ready','failed')),
    error_message TEXT,                                     -- populated when status = 'failed'
    page_count    INTEGER,                                  -- filled in after parsing
    summary       TEXT,                                     -- AI-generated summary, filled after processing
    created_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);


-- -----------------------------------------------------------------------------
-- Table: chunks
-- A document is split into overlapping text chunks before embedding.
-- Storing chunks separately lets us retrieve only the relevant pieces of a
-- document instead of sending the entire file to the LLM every time.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunks (
    id            BIGSERIAL       PRIMARY KEY,
    document_id   BIGINT          NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id       INTEGER         NOT NULL DEFAULT 1,
    chunk_index   INTEGER         NOT NULL,                 -- position of this chunk within the document
    page_number   INTEGER,                                  -- source page, for showing context to the user
    section       TEXT,                                     -- heading or section name if detectable
    content       TEXT            NOT NULL,                 -- raw text of this chunk
    -- vector(768) matches the output dimension of many embedding models (e.g. text-embedding-004)
    embedding     VECTOR(768),
    -- tsvector is a preprocessed form of content used by Postgres full-text search.
    -- GENERATED ALWAYS means Postgres keeps it in sync automatically — no manual updates needed.
    -- 'simple' config tokenises without stemming, so it handles mixed-language text (English + Hindi)
    -- without incorrectly normalising Hindi words through English morphology rules.
    tsv           TSVECTOR        GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    created_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);


-- -----------------------------------------------------------------------------
-- Indexes
-- -----------------------------------------------------------------------------

-- GIN index on tsv: enables fast keyword / full-text search across all chunks.
-- Without this, a LIKE or @@ query would do a slow sequential scan.
CREATE INDEX IF NOT EXISTS idx_chunks_tsv
    ON chunks USING GIN (tsv);

-- HNSW (Hierarchical Navigable Small World) index on embeddings.
-- Allows approximate-nearest-neighbour search in sub-linear time.
-- vector_cosine_ops means similarity is measured by cosine distance,
-- which is standard for normalised text embeddings.
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON chunks USING HNSW (embedding vector_cosine_ops);

-- B-tree index so "give me all chunks for document X" is an index lookup.
CREATE INDEX IF NOT EXISTS idx_chunks_document_id
    ON chunks (document_id);

-- B-tree index so we can filter a user's chunks efficiently
-- (important when the app grows to support multiple users).
CREATE INDEX IF NOT EXISTS idx_chunks_user_id
    ON chunks (user_id);
