# DocKeeper

A personal document question-answering app powered by RAG (Retrieval-Augmented Generation).
Upload your PDFs, ask questions in plain language, and get answers grounded in your own documents with numbered citations.

---

## What's in here

| File / Folder | Purpose |
|---|---|
| `docker-compose.yml` | Runs a Postgres + pgvector database in Docker |
| `sql/schema.sql` | Creates the `documents` and `chunks` tables |
| `app/config.py` | Reads all settings from `.env` |
| `app/db.py` | Connection pool and schema runner |
| `app/main.py` | FastAPI app, lifespan wiring, and `/health` endpoint |
| `app/parsers.py` | Text extraction for PDF, DOCX, XLSX, TXT, MD |
| `app/chunker.py` | Splits extracted pages into overlapping ~600-token chunks |
| `app/embeddings.py` | Gemini embedding wrapper with batching, throttling, and retry |
| `app/processing.py` | Full pipeline: parse → chunk → embed → store in Postgres |
| `app/search.py` | Vector search, keyword search, and hybrid RRF fusion |
| `app/llm.py` | Gemini chat wrapper with rate-limit retry |
| `app/routes_documents.py` | Upload, list, status, preview, delete endpoints |
| `app/routes_chat.py` | `/chat` answer endpoint and `/search/hybrid` debug endpoint |
| `requirements.txt` | Python dependencies |
| `uploads/` | Where uploaded files are stored (not committed to git) |

---

## How to run it

**1. Copy the example env file and fill in your values**
```powershell
copy .env.example .env
```
Open `.env` and set `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, and `GEMINI_API_KEY`.

**2. Install Python dependencies**
```powershell
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**3. Start the database**
```powershell
docker compose up -d
```

**4. Start the API server**
```powershell
uvicorn app.main:app --reload
```

**5. Check everything is working**

Open **`http://localhost:8000/`** — the full web interface. Upload documents, then ask questions.

```
http://localhost:8000/          ← web UI
http://localhost:8000/health    ← database check
http://localhost:8000/docs      ← interactive API docs
```

---

## API Endpoints

### Health
| Method | Path | Description |
|---|---|---|
| GET | `/health` | Database connectivity and extension check |

### Documents
| Method | Path | Description |
|---|---|---|
| POST | `/upload` | Upload a file (PDF, DOCX, XLSX, TXT, MD — max 200 MB). Processing starts automatically. |
| GET | `/documents` | List all documents, newest first |
| GET | `/documents/{id}/status` | Processing status, error message, and chunk count |
| POST | `/documents/{id}/process` | Re-run processing manually |
| GET | `/documents/{id}/preview` | First 500 chars of each extracted page *(debug)* |
| DELETE | `/documents/{id}` | Delete a document and its file from disk |

### Chat
| Method | Path | Description |
|---|---|---|
| POST | `/chat` | Ask a question; returns an answer with numbered citations |

### Search & Debug
| Method | Path | Description |
|---|---|---|
| GET | `/search?q=...&limit=5` | Vector-only search *(debug)* |
| GET | `/search/hybrid?q=...&limit=8` | Hybrid search showing RRF scores *(debug)* |

#### Example: ask a question
```powershell
curl -X POST http://localhost:8000/chat `
  -H "Content-Type: application/json" `
  -d "{\"question\": \"What is the refund policy?\"}"
```

#### Example: hybrid search debug
```powershell
curl "http://localhost:8000/search/hybrid?q=machine+learning&limit=5"
```

#### Example: upload a file
```powershell
curl -X POST http://localhost:8000/upload -F "file=@mydoc.pdf"
```

---

## Full pipeline

```
Upload saved to disk
       ↓
Status → 'processing'
       ↓
Parse file into pages  (parsers.py)
       ↓
Split into ~600-token overlapping chunks  (chunker.py)
       ↓
Embed chunks via Gemini API in batches  (embeddings.py)
       ↓
Insert chunks + vectors into Postgres  (processing.py)
       ↓
Status → 'ready'

── At query time ──────────────────────────────────────
Question
       ↓
Embed query  (embeddings.py)
       ↓
Vector search (HNSW) + Keyword search (GIN/tsv)  (search.py)
       ↓
Reciprocal Rank Fusion → top FINAL_K chunks
       ↓
Relevance gate (MIN_SIMILARITY)
       ↓
Build numbered context, capped at MAX_CONTEXT_CHARS
       ↓
Gemini LLM generates answer with [n] citations  (llm.py)
       ↓
{"answer": "...", "sources": [...]}
```

---

## Stopping the database

```powershell
docker compose down
```

Add `-v` to also delete all stored data: `docker compose down -v`.
