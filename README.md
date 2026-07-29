# AI Incident Classifier

LLM-powered incident classification with failure-mode taxonomy and exact-match grouping. Built for Hajj operations (Nusuk Masar Haj).

<img width="1619" height="903" alt="Screenshot 1448-02-15 at 7 22 01 AM" src="https://github.com/user-attachments/assets/3b288fec-616f-4919-bf4d-00e5852768f4" />

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
| POST | `/classify` | Classify + find duplicates |
| GET | `/classify` | Same via query params |
| POST | `/classify/batch` | Batch classify (max 50) |
| POST | `/import/{file.json}` | Bulk import from JSON file |
| GET | `/incidents` | List incidents (?status=active) |
| GET | `/incidents/{id}` | Get one incident |
| POST | `/incidents/{id}/resolve` | Mark resolved |
| GET | `/api/reports/{period}` | Clustered report for dashboard |
| GET | `/health` | Service status |
| POST | `/reset` | Delete all incidents |

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
│   │   ├── grouping.py          # Two-phase clustering (FM + embedding)
│   │   ├── failure_modes.py     # 22 FM codes mined from real tickets
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

1. **Dedupe gate** — content hash (digit-blanked) checks DB before LLM call
2. **LLM classifies** — returns system, service, severity, FM code from taxonomy
3. **Embedding** — uses taxonomy description string (not LLM output) for FM-matched tickets
4. **Background rebuild** — every 5 min:
   - **Phase 1**: Exact-match by FM code (50%+ coverage)
   - **Phase 2**: Embedding similarity for unclassified tickets

## Failure-Mode Taxonomy

22 codes mined from 199 real tickets, each with includes/excludes keywords:

| Code | Description | Tickets |
|------|-------------|---------|
| FM-018 | Rawdah permit issuance fails | 19 |
| FM-007 | Company evaluation icon missing | 10 |
| FM-014 | Users unable to reply/close reports | 8 |
| FM-001 | Database server CPU exceeds threshold | 8 |
| FM-020 | Arrival confirmation fails | 8 |
| FM-011 | Tax billing data access blocked | 8 |
| FM-022 | Appeal submission fails | 7 |
| FM-015 | Inter-city request approval fails | 6 |
| FM-010 | Pilgrim data entry blocked | 5 |
| FM-005 | Payment transactions fail | 4 |
| FM-004 | CRM operational failure | 4 |
| FM-008 | Complaint status not updated | 4 |
| FM-017 | Registration rejects license | 3 |
| FM-012 | Manual permit request | 3 |
| FM-002 | Housing confirmations cancelled | 3 |

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
