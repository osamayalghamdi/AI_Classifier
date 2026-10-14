# AI Incident Classifier

LLM-powered incident classification on the Nusuk Masar Haj service-offering model (offering + sub-offering), with embedding clustering for live duplicate detection and root-cause grouping. Built for Hajj operations (Nusuk Masar Haj).

<img width="1619" height="903" alt="Screenshot 1448-02-15 at 7 22 01 AM" src="https://github.com/user-attachments/assets/3b288fec-616f-4919-bf4d-00e5852768f4" />

## Deployment — one config file, inside the chart

No model is installed or baked into the image: every model lives **outside**
this project and the app holds only URLs + keys. Kubernetes is the target, and
**everything is configured in one place:
`deploy/helm/ai-classifier/values.yaml`**.

```bash
kubectl create namespace ai-classifier

# Secrets (never in values) — the only things set outside the values file:
kubectl -n ai-classifier create secret generic ai-classifier-secrets \
  --from-literal=LLM_API_KEY='<llm key>' \
  --from-literal=INTEGRATION_API_TOKEN='<api token>'
kubectl -n ai-classifier create secret generic ai-classifier-db \
  --from-literal=PG_PASSWORD='<db password>'

# Point values.yaml at your database + model endpoints, then:
helm upgrade --install ai-classifier deploy/helm/ai-classifier -n ai-classifier --wait
```

Rules of thumb:

- `EMBEDDING_DIM` (default `1024`) must match what the embeddings endpoint
  returns — it is written into the `vector(N)` column on first startup, so set
  it before the first run.
- The classifier needs an **enabled model registry entry**, and `LLM_MODEL` is
  enough: an `ollama/…` id (the local model) or an `openai/…` id (the company
  endpoint) enables the matching entry automatically. Anything else needs
  `MODEL_<NAME>_ENABLED=1` (`MODEL_<NAME>_ID` / `_API_BASE` / `_API_KEY`
  override that model's endpoint).
- A missing model **does not stop the app**: it hosts with whatever is
  configured and names every gap. The startup log carries the list (variable,
  example value, what degrades meanwhile) and `GET /ready` serves the same as
  `models.issues`. Only classification / OCR / similarity are affected — the
  dashboard, ingest, review and the E1-E9 API stay up.
- Attachment OCR is a **separate service** — point `backend.env.OCR_SERVICE_URL`
  at it (empty = attachments recorded by name only).
- **Attachment understanding** (universal layers: where / objects / signals /
  text / summary) is an external vision reader, env-only — set
  `ATTACHMENTS_ENABLED=1` + `ATTACHMENT_VISION_API_URL` / `_MODEL` / `_API_KEY`.
  Off by default (text-only, never crashes). Measurement tools:
  `backend/scripts/attachments_backfill.py` and `backend/evaluation/attachments_eval.py`
  (both accept `--mock` to run offline); contract in
  `backend/ai_classification/services/attachments/CONTRACT.md`.
- The **database is required platform infrastructure** — the platform/DB team
  runs it, it is *not* in the chart, and it is not a third-party service. The
  **only third-party external services are the models** (LLM, embeddings, OCR,
  vision). See `deploy/helm/ai-classifier/README.md` and `DEPLOYMENT_GUIDE.md`.

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
│   ├── Dockerfile                  # nginx + dashboard files (the image used by k8s)
├── deploy/                         # infrastructure
│   ├── helm/ai-classifier/         # Kubernetes chart — values.yaml is THE config; README.md is the handover
│   └── DEPLOYMENT_GUIDE.md         # K8s deployment notes + gotchas
└── README.md  ARCHITECTURE.md
```

## Running locally (no .env, one config file)

The whole thing runs on this machine: kind for the cluster, Ollama for the
models, the PostgreSQL in namespace `platform-db` for the database. **The only
config file is `deploy/helm/ai-classifier/values.yaml`** — there is no `.env`
anywhere in the project.

```bash
# 1. models (once): the classifier + the embedding model the values point at
ollama pull classifier-qwen && ollama pull bge-m3

# 2. images (after any code change)
docker build -f backend/Dockerfile   -t ai_classifier-api:latest        backend/
docker build -f frontend/Dockerfile  -t ai-classifier-frontend:latest   frontend/
kind load docker-image ai_classifier-api:latest ai-classifier-frontend:latest --name ai-classifier

# 3. the app
helm upgrade --install ai-classifier deploy/helm/ai-classifier -n ai-classifier

# 4. reach it
kubectl -n ai-classifier port-forward svc/ai-classifier-ai-classifier-frontend 8085:8082
#   dashboard http://localhost:8085   ·   API http://localhost:8000 via the same proxy
```

A host-side run instead of a pod (same config source, nothing to keep in sync):

```bash
cd backend
source scripts/env-from-chart.sh          # exports values.yaml's backend.env + database.*
.venv/bin/python -m uvicorn ai_classification.app:app --host 0.0.0.0 --port 8000
```

**Is it healthy? What is still missing?** One read-only command answers both, and
names the variable or command for anything it finds wrong:

```bash
backend/scripts/doctor.sh                     # the cluster deployment
backend/scripts/doctor.sh --api http://localhost:8000   # a host-side run
```

Either way the models are unmissable: an unconfigured model does **not** stop
the app — the startup log and `GET /ready` (`models.issues`) name the missing
value, the variable to set, and what degrades meanwhile.

# Tests (needs a Postgres; see .github/workflows/test.yml for the CI env)
cd ~/projects/AI_Classifier/backend && uv run pytest -q

# Dashboard (separate terminal)
cd ~/projects/AI_Classifier/frontend/dashboard
python3 -m http.server 8085 --bind 0.0.0.0
```

## Build the images

Both images build straight from the repo (the API image is model-free, ~1.1 GB):

```bash
# API — the context MUST be backend/ (it holds pyproject.toml,
# ai_classification/, integrations/, entrypoint.sh). Building from the repo
# root fails with "/ai_classification: not found".
docker build -f backend/Dockerfile -t REGISTRY/ai-classifier:<tag> backend/

# Dashboard
docker build -f frontend/Dockerfile -t REGISTRY/ai-classifier-frontend:<tag> frontend/
```

Push them to your registry, set `image.repository` / `image.tag` (+ the frontend
equivalents) in `values.yaml`, then deploy with `helm upgrade --install` as shown
at the top. Full release steps: `deploy/helm/ai-classifier/README.md` §2.

## How to change things (all in one file: `values.yaml`)

| What you want to change | Where (chart `values.yaml`) |
|---|---|
| The AI model | `backend.env.LLM_MODEL` (`ollama/classifier-qwen` local, `openai/qwen3.6` for the company endpoint) |
| The AI service URL | `backend.env.LLM_API_BASE` (`http://<host>:11434` local Ollama, `https://llms.elm.sa/v1` at the customer) |
| The AI API key | Secret `backend.existingSecret` → `LLM_API_KEY` / `UNIVERSAL_API_KEY` (not needed for a local Ollama) |
| Embeddings endpoint | `backend.env.EMBEDDING_API_URL` + `EMBEDDING_MODEL_ID` / `EMBEDDING_DIM` |
| Database connection | `database.host` / `.port` / `.name` / `.user` + `database.existingSecret` (password) |
| Integration API token | Secret `backend.existingSecret` → `INTEGRATION_API_TOKEN` |
| Duplicate-ticket threshold | `backend.env.SIMILARITY_THRESHOLD` |
| Attachment understanding | `backend.env.ATTACHMENTS_ENABLED` + `ATTACHMENT_VISION_*` (+ the key in the Secret) |

After editing values, upgrade the release:

```bash
helm upgrade ai-classifier deploy/helm/ai-classifier -n ai-classifier
```

**Status check** — see if everything is healthy (from inside the cluster):

```bash
kubectl -n ai-classifier port-forward svc/ai-classifier-backend 8000
curl http://localhost:8000/status
# {"status":"ok","services":{"db":...,"embedding":...,"llm":...}}
```

The dashboard shows a red `⚠ llm:DOWN` indicator in the top bar when the
AI endpoint is unreachable, and the startup logs print
`SERVICE LLM: UNREACHABLE` loudly — so a wrong key or unreachable model
is visible immediately.

**Full detail:** `deploy/DEPLOYMENT_GUIDE.md` and
`deploy/helm/ai-classifier/README.md`.

## Ports & URLs

In-cluster the chart exposes two Services (both `ClusterIP`): the frontend on
`frontend.port` (8082) and the API on `backend.port` (8000). Nothing is
published until you enable `ingress` or set a Service type.

```bash
# Reach them from your machine (binds 127.0.0.1 only — never --address 0.0.0.0):
kubectl -n ai-classifier port-forward svc/ai-classifier-frontend 8082:8082
kubectl -n ai-classifier port-forward svc/ai-classifier-backend  8000:8000
```

Postgres is required infrastructure the platform team runs — the API reaches it
over the network; the chart holds only the connection values (`database.*`).

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
1. **Base URL**: the API Service — `http://ai-classifier-backend.<ns>.svc:8000` in-cluster, or your ingress host
2. **Auth token**: `INTEGRATION_API_TOKEN` from the Secret — every `/api/v1/*`
   call needs an `Authorization: Bearer <token>` header.

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
# Needs a Postgres and the env vars exported (there is no .env in the repo).
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
