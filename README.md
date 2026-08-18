# AI Incident Classifier

LLM-powered incident classification with failure-mode taxonomy and exact-match grouping. Built for Hajj operations (Nusuk Masar Haj).

## Stack

| Layer | Tech |
|-------|------|
| API | FastAPI + Uvicorn |
| LLM | LiteLLM — Qwen3.6-35B-A3B via OpenRouter |
| Embeddings | BAAI/bge-m3 (1024d) |
| Storage | PostgreSQL + pgvector (HNSW ANN index) |
| Frontend | Vanilla JS, no build step |
| Grouping | Two-phase: FM-code exact match → embedding fallback |

## API

| Method | Path | Description |
|--------|------|-------------|
| POST | `/classify` | Classify + find similar incidents |
| GET | `/classify` | Same via query params |
| POST | `/classify/batch` | Batch classify (max 50) |
| POST | `/import/{file.json}` | Bulk import from JSON file |
| GET | `/incidents` | List incidents (?status=active) |
| GET | `/incidents/{id}` | Get one incident |
| POST | `/incidents/{id}/resolve` | Mark resolved |
| GET | `/api/reports/{period}` | Clustered report for dashboard |
| GET | `/health` | Service status |
| POST | `/reset` | Delete all incidents |

**Integration API (ready):** `/api/v1/incidents` (async ingest, 202 + reference),
`/api/v1/incidents/{ref}` (fetch result), `/api/v1/incidents/dry-run` (writes
nothing), `/api/v1/backfill` (batch ≤200), `/ready` (db/embedding/llm checks).
Bearer auth (`INTEGRATION_API_TOKEN`). Full contract: `docs/INTEGRATION_GUIDE.md`.

## SMAX integration — code ready, not live yet

SMAX = the ticketing system. Two ways to connect:

1. **API (ready now):** SMAX (or any system) pushes incidents →
   `POST /api/v1/incidents` → poll `GET /api/v1/incidents/{ref}` for the
   result. Nothing to configure on the SMAX side beyond the bearer token.

2. **Polling (code exists, NOT yet tested against real SMAX):** the app
   polls SMAX for changed tickets itself (adapter: `ai_classification/seams/smax/`).
   Enable when SMAX credentials exist:

   ```
   TICKETING_API_URL=<smax-url>     # default http://localhost:8002
   TICKETING_API_TOKEN=<token>      # required — until set, sync logs
                                    # "SMAX source is not configured" (expected)
   TICKETING_SOURCE=real            # default
   TICKETING_DRY_RUN=true           # first: verify nothing is written back
   ```

   Until `TICKETING_API_TOKEN` is configured, the API path (1) is the
   integration route.

## Project Layout

```
AI_Classifier/
├── ai_classification/
│   ├── __init__.py
│   ├── config.py                # Env-based settings (LLM model, PG, keys)
│   ├── sync.py                  # Background ticketing sync worker
│   ├── api/
│   │   ├── routes.py            # FastAPI endpoints — no business logic
│   │   └── schemas.py           # Pydantic request/response schemas
│   ├── core/
│   │   ├── classifier.py        # LLM classification via LiteLLM
│   │   ├── store.py             # PostgreSQL + pgvector persistence
│   │   ├── grouping.py          # Two-phase clustering (offering + embedding)
│   │   ├── failure_modes.py     # Legacy FM codes (internal only — not the product taxonomy)
│   │   └── import_service.py    # Bulk import logic
│   └── domain/
│       ├── models.py            # ClassificationResult, SimilarMatch
│       └── taxonomy.py          # Hajj-only enums (3 systems, 189 services)
├── frontend/dashboard/          # Three-lens dashboard (standalone)
│   ├── index.html               # UI shell
│   └── app.js                   # Logic + rendering (no build step)
├── tests/
│   ├── test_classifier.py       # 15 tests (mocked LLM)
│   ├── test_incident_store.py   # 22 tests (requires PG)
│   └── test_service.py          # 5 tests
├── ocr/                         # OCR microservice (separate)
├── simulator/                   # Ticketing simulator (separate)
├── test_incidents.json          # 200 real Nusuk tickets
└── pyproject.toml
```

## Quick Start

```bash
# Backend
cd ~/projects/AI_Classifier
source .env
uv run uvicorn ai_classification.api.routes:app --host 0.0.0.0 --port 8000

# Dashboard (separate terminal)
cd frontend/dashboard
python3 -m http.server 8085 --bind 0.0.0.0
open http://192.168.1.50:8085
```

## Classification Flow

1. **ID-based dedupe** — exact match on source_ticket_id prevents double-counting; text similarity is informational only
2. **LLM classifies** — returns offering/sub-offering, service, severity
3. **Embedding** — matches against the offering description (not LLM output)
4. **Background rebuild** — every 5 min:
   - **Phase 1**: Exact-match by offering
   - **Phase 2**: Embedding similarity for unclassified tickets

## Taxonomy

The system classifies incidents against the **offering catalog** — offerings
and sub-offerings, versioned JSON, grown from real tickets (data-driven).
The LLM returns the offering/sub-offering; embeddings match against offering
descriptions. Legacy FM codes are internal identifiers only, not the product
taxonomy.

## Tests

```bash
uv run pytest tests/test_classifier.py -v   # 15 tests, mocked LLM
uv run pytest tests/ -v                      # 42 tests, some need PG
```

## Test Data

```bash
curl -s -X POST http://127.0.0.1:8000/reset
curl -s -X POST http://127.0.0.1:8000/import/test_incidents.json
curl -s http://127.0.0.1:8000/reports/daily | python3 -m json.tool
```
