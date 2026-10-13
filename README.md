# AI Incident Classifier

LLM-powered incident classification on the Nusuk Masar Haj service-offering model (offering + sub-offering), with embedding clustering for live duplicate detection and root-cause grouping. Built for Hajj operations (Nusuk Masar Haj).

<img width="1619" height="903" alt="Screenshot 1448-02-15 at 7 22 01 AM" src="https://github.com/user-attachments/assets/3b288fec-616f-4919-bf4d-00e5852768f4" />

## First run — the env file is the whole configuration

No model is installed or baked into the image: every model lives **outside**
this project, and the app holds only URLs + keys. Copy the template, fill it
in, start it:

```bash
cp .env.example .env
# in .env set:
#   LLM_API_BASE / LLM_MODEL / LLM_API_KEY            → classification
#   EMBEDDING_API_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL_ID / EMBEDDING_DIM
#                                                     → similarity (OpenAI-compatible /embeddings)
#   OCR_SERVICE_URL                                   → attachment text (optional; empty = names only)
#   ATTACHMENT_VISION_API_URL / ATTACHMENT_VISION_MODEL / ATTACHMENT_VISION_API_KEY
#                                                     → attachment "layers" (ATTACHMENTS_ENABLED=1; empty = text-only)
#   POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB    → database
docker compose up -d        # api + postgres + nginx (dashboard served by nginx)
```

Rules of thumb:

- `EMBEDDING_DIM` (default `1024`) must match what the embeddings endpoint
  returns — it is written into the `vector(N)` column on first startup, so set
  it before the first run.
- The classifier needs an **enabled model registry entry**: setting `LLM_MODEL`
  to an `openrouter/…` or `openai/…` id enables the matching entry
  automatically; anything else needs `MODEL_<NAME>_ENABLED=1`
  (`MODEL_<NAME>_ID` / `_API_BASE` / `_API_KEY` override that model's endpoint).
- Attachment OCR is a **separate service**. To host the bundled one:
  `docker compose --profile ocr up -d` + `OCR_SERVICE_URL=http://ocr:8001`.
- **Attachment understanding** (universal layers: where / objects / signals /
  text / summary) is an external vision reader, env-only — set
  `ATTACHMENTS_ENABLED=1` + `ATTACHMENT_VISION_API_URL` / `_MODEL` / `_API_KEY`.
  Off by default (text-only, never crashes). Measurement tools:
  `backend/scripts/attachments_backfill.py` and `backend/evaluation/attachments_eval.py`
  (both accept `--mock` to run offline); contract in
  `backend/ai_classification/services/attachments/CONTRACT.md`.
- Kubernetes: the chart is values-only and deploys the same two workloads
  (api + frontend). The **database is required platform infrastructure** — the
  platform/DB team runs it, it is *not* in the chart, and it is not a
  third-party service. The **only third-party external services are the models**
  (LLM, embeddings, OCR, vision). See `deploy/helm/ai-classifier/README.md`
  (first install) and `DEPLOYMENT_GUIDE.md`.

## Stack

| Layer | Tech |
|-------|------|
| API | FastAPI + Uvicorn |
| LLM | LiteLLM — Qwen3.6-35B-A3B via OpenRouter |
| Embeddings | External OpenAI-compatible `/embeddings` endpoint (bge-m3, 1024d) — **no model in the image** |
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

**Admin console (`/admin.html`, bearer auth):** overall status, taxonomy
overrides (add services/offerings to the FROZEN base taxonomy — effective
immediately, persisted in the `taxonomy_overrides` table), env credentials
(write to the env file — restart required), add incident, full DB reset,
cluster group add/adjust, and in-container smoke/pytest runners. All
`/admin/*` endpoints require the same bearer token as `/api/v1/*`.

**Bulk-ingest rule (applies everywhere):** bulk loads (>20 tickets) go through
the **async integration API** (`POST /api/v1/backfill` or `/api/v1/incidents` →
202 + poll) — it has a retry worker with backoff and never holds an HTTP
connection for the whole run. `/classify` and `/classify/batch` are for **single
interactive tickets / small manual batches** (and `/classify/batch` is serial by
design — no concurrency; `CLASSIFY_BATCH_SLEEP_S` can pace it if needed).

## SMAX integration — standalone connector

SMAX = the ticketing system. The connection lives in its **own process**:
`integrations/smax/` — a self-contained connector that talks to the
classifier **only through its public HTTP API** (zero `ai_classification`
imports; runnable on a machine that only has network access).

```
SMAX ──list_changed──▶ integrations/smax (poller)
                          │ POST /api/v1/incidents (Bearer CLASSIFIER_API_TOKEN)
                          ▼
                      classifier API ──▶ classification + persistence
                          │ GET /api/v1/incidents/{ref}
                          ▼
                      integrations/smax (writeback) ──write_suggestion──▶ SMAX
```

Run it:

```bash
cd backend/integrations/smax
cp .env.example .env                       # SMAX config lives HERE, not in the app .env
export SMAX_BASE=https://smax.example.com  # upstream ticketing system (SMAX_API_URL also works)
export SMAX_TENANT=<tenant>                # TENANTID (login query + cookie)
export SMAX_USER=<integration-user>
export SMAX_PASS=<password>                # login exchange — there is NO static token
export CLASSIFIER_API_URL=http://localhost:8000
export CLASSIFIER_API_TOKEN=<INTEGRATION_API_TOKEN from .env>
export SMAX_DRY_RUN=true                   # default; write-back logs, posts nothing

.venv/bin/python -m integrations.smax.main --check                     # validate config, masked summary
.venv/bin/python -m integrations.smax.main --probe <id> --probe-redact  # one incident, structure only
.venv/bin/python -m integrations.smax.main --once                      # one poll + write-back pass
.venv/bin/python -m integrations.smax.main                             # poller + writeback (Ctrl-C to stop)
```

Write-back is **not implemented yet**: the SMAX handover documents no write
endpoint, so `write_suggestion` refuses and the loop only logs the payload it
would post. Confirm the write API before enabling it.

Full contract (payload shapes, env vars, error handling, sequence diagram):
`backend/integrations/smax/README.md`. The E1-E9 API it consumes is documented in
`backend/docs/INTEGRATION_GUIDE.md`.

## Project Layout

```
AI_Classifier/
├── backend/                        # the service (Python)
│   ├── ai_classification/
│   │   ├── app.py                  # FastAPI wiring: lifespan, CORS, router mounting, worker startup
│   │   ├── api/                    # Endpoint-only modules (no business logic)
│   │   ├── domain/                 # ClassificationResult, taxonomy enum/hierarchy (frozen)
│   │   ├── services/               # classify, cluster, jobs, review, ingest, match
│   │   ├── seams/                  # ticket-source port, pipeline, local fake source
│   │   └── shared/                 # config + split store (db, store_incidents, store_clusters, store_logs)
│   ├── tests/                      # Mirrors services 1:1
│   ├── scripts/                    # canary, reseed, endpoints, migrations
│   ├── docs/                       # App docs + worklogs/
│   ├── ocr/                        # OCR sidecar — company vision model (no local model)
│   ├── integrations/smax/          # Standalone SMAX connector (talks to the API over HTTP)
│   ├── legacy/suboffering_engine/  # Quarantined sub-offering engine (superseded, for resurrection)
│   ├── simulator/  evaluation/
│   └── pyproject.toml  uv.lock  Dockerfile  entrypoint.sh  smoke_test.sh
├── frontend/                       # the dashboard (no build step)
│   ├── dashboard/                  # index.html, app.js, admin.html, write-incident.html …
│   ├── Dockerfile                  # nginx + dashboard files (the image used by k8s/compose)
│   └── nginx.conf                  # dev/compose reverse proxy (the k8s copy lives in the chart)
├── deploy/                         # infrastructure
│   ├── helm/ai-classifier/         # Kubernetes chart — README.md is the DevOps handover
│   ├── DEPLOYMENT_GUIDE.md         # K8s deployment notes + gotchas
│   └── DEPLOY.md                   # Docker-compose server deploy
├── docker-compose.yml              # local/VM stack (reads .env from the repo root)
└── README.md  ARCHITECTURE.md
```

## Quick Start

```bash
# Backend API
cd ~/projects/AI_Classifier/backend
source ../.env
uv run uvicorn ai_classification.api.routes:app --host 0.0.0.0 --port 8000

# Tests (needs a Postgres; see .github/workflows/test.yml for the CI env)
cd ~/projects/AI_Classifier/backend && uv run pytest -q

# Dashboard (separate terminal)
cd ~/projects/AI_Classifier/frontend/dashboard
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

# 3. Configure (see "How to change things" below)
cp .env.example .env
nano .env          # fill in the ELM API key + a database password

# 4. THE GATE — check the AI model works before anything else
./backend/scripts/canary.sh

# 5. Build + start (~10 min the first time)
docker compose build api
docker compose up -d postgres api nginx

# 6. Load demo data (optional)
./backend/scripts/reseed.sh http://localhost:8000 test_incidents.json
```

Open the dashboard at `http://<VM-IP>:8082`.

Everything is documented in detail in `deploy/DEPLOY.md`.

## How to change things (all in one file: `.env`)

| What you want to change | Line in `.env` |
|---|---|
| The AI model | `LLM_MODEL` (e.g. `openai/qwen3.6` or `ollama/qwen2.5:7b`) |
| The AI service URL | `LLM_API_BASE` (e.g. `https://llms.elm.sa/v1`) |
| The AI API key | `LLM_API_KEY` |
| Database password (REQUIRED) | `POSTGRES_PASSWORD` |
| Integration API token | `INTEGRATION_API_TOKEN` (empty = locked) |
| Duplicate-ticket threshold | `SIMILARITY_THRESHOLD` (0.35 for deploy) |
| Attachment understanding | `ATTACHMENTS_ENABLED` + `ATTACHMENT_VISION_API_URL` / `_MODEL` / `_API_KEY` |

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

**Full detail:** `DEPLOY.md` (deploy runbook) and `docs/worklogs/DEPLOY_STATUS.md`
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

## Volume-adaptive clustering

Incident volume is not constant — some periods have a handful of tickets,
others a flood (Hajj season). The clustering sensitivity therefore adapts
to how many active incidents exist right now:

- **Few incidents (≤ 20):** sensitivity is LOOSE — the embedding threshold
  drops to 0.40 and groups of just 2 tickets count as real clusters, so
  operators see "2 real problems" instead of 15 lonely tickets.
- **Many incidents (≥ 150):** sensitivity is TIGHT — the threshold rises to
  0.60 and a cluster needs at least 4 tickets, so unrelated tickets don't
  merge into giant meaningless clusters.
- **In between:** the threshold interpolates smoothly (0.40 → 0.60), and
  the minimum cluster size steps 2 → 4.

The internal coherence floor scales with the threshold too (0.50 in the
loose regime, 0.70 at mid volume — the previous behavior exactly — and
0.80 in a flood). The whole mechanism is a pure, deterministic function
of the active-incident count — same data always gives the same groups,
and the LLM validator still runs at temperature 0 with seed 42.

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
