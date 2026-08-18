# AI Incident Classifier

LLM-powered incident classification on the Nusuk Masar Haj service-offering model (offering + sub-offering), with embedding clustering for live duplicate detection and root-cause grouping. Built for Hajj operations (Nusuk Masar Haj).

<img width="1619" height="903" alt="Screenshot 1448-02-15 at 7 22 01 AM" src="https://github.com/user-attachments/assets/3b288fec-616f-4919-bf4d-00e5852768f4" />

## Stack

| Layer | Tech |
|-------|------|
| API | FastAPI + Uvicorn |
| LLM | LiteLLM — Qwen3.6-35B-A3B via OpenRouter |
| Embeddings | BAAI/bge-m3 (1024d) |
| Storage | PostgreSQL + pgvector (HNSW ANN index) |
| Frontend | Vanilla JS, no build step |
| Grouping | Two-phase: offering exact match → embedding fallback |

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

## Deploy on a Linux VM with Docker (recommended)

The project runs entirely inside Docker — the VM only needs Docker, ~15 GB
disk, and internet (for the one-time image build). No Python, Node, or
database installs on the VM.

```bash
# 1. Install Docker (one-time)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker

# 2. Get the code
git clone git@github.com:osamayalghamdi/AI_Classifier.git
cd AI_Classifier
git checkout feat/deploy-integration-ready

# 3. Configure (see "How to change things" below)
cp .env.example .env
nano .env          # fill in the ELM API key + a database password

# 4. THE GATE — check the AI model works before anything else
./scripts/canary.sh

# 5. Build + start (~10 min the first time)
docker compose build api
docker compose up -d postgres api nginx

# 6. Load demo data (optional)
./scripts/reseed.sh http://localhost:8000 test_incidents.json
```

Open the dashboard at `http://<VM-IP>:8082`.

Everything is documented in detail in `DEPLOY.md`.

## How to change things (all in one file: `.env`)

| What you want to change | Line in `.env` |
|---|---|
| The AI model | `LLM_MODEL` (e.g. `openai/qwen3.6` or `ollama/qwen2.5:7b`) |
| The AI service URL | `LLM_API_BASE` (e.g. `https://llms.elm.sa/v1`) |
| The AI API key | `LLM_API_KEY` |
| Database password (REQUIRED) | `POSTGRES_PASSWORD` |
| Integration API token | `INTEGRATION_API_TOKEN` (empty = locked) |
| Duplicate-ticket threshold | `SIMILARITY_THRESHOLD` (0.35 for deploy) |

After editing `.env`, restart the API:

```bash
docker compose restart api
```

**Status check** — see if everything is healthy:

```bash
curl http://localhost:8000/status
# {"status":"ok","services":{"db":...,"embedding":...,"llm":...}}
```

The dashboard shows a red `⚠ llm:DOWN` indicator in the top bar when the
AI endpoint is unreachable, and the startup logs print
`SERVICE LLM: UNREACHABLE` loudly — so a wrong key or unreachable model
is visible immediately.

**Full detail:** `DEPLOY.md` (deploy runbook) and `DEPLOY_STATUS.md`
(what was verified, gate results, known open items).

## Ports & URLs

The deployed stack exposes **3 ports** (all changeable in `.env`):

| Port | Service | What it is | URL |
|---|---|---|---|
| **8082** | nginx | The dashboard (UI) | `http://<VM-IP>:8082` |
| **8000** | FastAPI | The API (what integrations call) | `http://<VM-IP>:8000` |
| **8001** | OCR | OCR microservice (attachments) | `http://<VM-IP>:8001` |

Postgres (5432) is NOT exposed to the host — only the api reaches it,
inside the Docker network (deliberately safer). Change ports via
`API_PORT` in `.env` (e.g. `API_PORT=9000` if 8000 is taken).

### Key API URLs (:8000)

| URL | Purpose |
|---|---|
| `/health` | Liveness — is the API up? |
| `/ready` | Readiness — db / embedding / llm (one-shot) |
| `/status` | Per-service status: db / embedding / llm + resolved model & URL |
| `/test/llm` | **Ask the configured model anything** — live LLM smoke test |
| `/test/all` | **RUN THE WHOLE BATTERY** — db/embedding/llm/classify/similar/clusters in one call |
| `/docs` | Swagger UI — interactive API explorer |
| `/classify` | POST — classify one ticket |
| `/incidents` | GET — list incidents |
| `/api/reports/daily` | GET — clustered report (dashboard data) |
| `/api/v1/incidents` | POST — integration ingest (async, needs token) |
| `/api/v1/incidents/dry-run` | POST — integration dry-run (persists nothing) |
| `/api/v1/backfill` | POST — batch ingest |

### What the ticketing-system team needs

Just two things:
1. **Base URL**: `http://<VM-IP>:8000` (or whatever `API_PORT` is set to)
2. **Auth token**: `INTEGRATION_API_TOKEN` from `.env` — every `/api/v1/*`
   call needs `Authorization: Bearer <token>`

Full contract (payload shapes, error codes, retry semantics, curl example
per endpoint) is in `docs/INTEGRATION_GUIDE.md`.

### Ready-made request files (send to every endpoint fast)

- **`docs/AI_Classifier.postman_collection.json`** — import into Postman
  (File → Import). Set the `base` variable to `http://<VM-IP>:8000` and
  `token` to your `INTEGRATION_API_TOKEN`. All 7 groups, ~25 requests,
  organized: health/status, LLM test, classification, incidents, reports,
  data management, integration API.
- **`scripts/endpoints.sh`** — no Postman needed: prints every endpoint as
  a ready-to-paste curl command.

```bash
# quick smoke of the whole surface from the VM:
curl -s http://localhost:8000/status          # all services ok?
curl -s "http://localhost:8000/test/llm?question=Hello"   # LLM answers?
```

### Quick checks after deploy

```bash
curl http://<VM-IP>:8000/status        # all services ok?
curl http://<VM-IP>:8082/              # dashboard loads?
curl http://<VM-IP>:8000/docs          # API explorer
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
# Needs a Postgres (compose: docker compose up -d postgres) and the LLM env.
# The suite REFUSES to run against the production database (ai_incidents) —
# it forces ai_incidents_test automatically.
PG_PORT=5432 uv run pytest tests/ -q
# ~131 passed + xfail/xpass (documented flaky canary pairs)
```

## Test Data

```bash
./scripts/reseed.sh http://localhost:8000 test_incidents.json
# ~11 min: wipes, imports 100 tickets, classifies + embeds through the live
# pipeline → 91 stored (9 duplicate titles deduped). Re-runnable anytime.
```
