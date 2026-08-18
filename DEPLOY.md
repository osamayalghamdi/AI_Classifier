# AI_Classifier — Server Deploy Guide (Docker Compose)

Target: a fresh Linux server (Ubuntu/Debian) with Docker + Compose v2.
The LLM is a REMOTE API (Saudi ELM: https://llms.elm.sa/v1 — resolves on
the production server, not the dev box). No Ollama needed on the server.

## 0. Prerequisites on the server

```bash
# Docker + compose plugin
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
docker compose version   # expect v2.x
# uv (for the canary + test steps) + python3 (reseed script)
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo apt-get install -y python3
```

## 1. Get the code

```bash
cd ~
git clone git@github.com:osamayalghamdi/AI_Classifier.git
cd AI_Classifier
git checkout feat/deploy-integration-ready   # the integration-ready release branch
```

## 2. Configure the environment (THE critical step)

Create `.env` from the example with the ELM provider block ACTIVE:

```bash
cp .env.example .env
nano .env
```

Required for a working LLM (ELM):

```bash
LLM_MODEL=openai/qwen3.6
LLM_API_BASE=https://llms.elm.sa/v1
LLM_API_KEY=<your ELM key>
# Universal key variant if ELM uses it (per current .env):
# BASE_URL=https://llms.elm.sa/v1
# UNIVERSAL_API_KEY=<key>
```

REQUIRED for the database (compose FAILS LOUDLY if unset):

```bash
POSTGRES_USER=aiuser
POSTGRES_PASSWORD=<pick a strong password>
POSTGRES_DB=ai_incidents
```

Optional integration token (W3 API auth — fail-closed: empty = all
integration requests rejected with 401):

```bash
INTEGRATION_API_TOKEN=<token for the ticketing system>
```

The compose file passes these straight through to the api container.
Verify from the server BEFORE starting (ELM must resolve from the server):

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://llms.elm.sa/v1/models
# expect 200 (401/403 also OK = reachable; 000 = DNS/network problem)
```

## 2.5 CRITICAL GATE — canary before any live traffic (D4)

The 34-pair canary validates the company-hosted model BEFORE anything
real flows through it. One command — reads LLM_* from .env explicitly
(no CWD traps, refuses the ollama default):

```bash
./scripts/canary.sh
```

Expected: 22/22 wrong pairs → NO and 5/5 correct → YES (8 passed,
6 xfailed = the 7 documented flaky pairs, 1 xpassed). If the strict
wrong→NO direction breaks, STOP — report to the manager. Do NOT tune
prompts or thresholds to make it green. A deviation here is information
about the model.

## 3. Build and start

```bash
docker compose build api          # ~10 min first time (torch + bge-m3 baked in)
docker compose up -d postgres api nginx
```

- `postgres` — pgvector image, healthchecked (api waits for it)
- `api` — FastAPI on :8000, healthchecked; entrypoint SKIPS ollama wait
  when LLM_MODEL isn't ollama/* (OpenRouter/ELM path)
- `nginx` — dashboard on :8082, proxies /api /classify /incidents
  /reports /health to api:8000
- `ollama` / `cloudflared` / `ocr` — NOT started (profiles / optional).
  Start if needed: `docker compose --profile local-llm up -d ollama`

## 4. Verify it's alive

```bash
docker compose ps                  # all 3 healthy
curl -s http://localhost:8000/health
# {"status":"ok","model":"openai/qwen3.6","store_ready":true}
curl -s -X POST http://localhost:8000/classify -H "Content-Type: application/json" \
  -d '{"title":"Rawdah permit booking fails","description":"User cannot book Rawdah permit, error on done button"}'
# returns classification — proves LLM + DB + embeddings all wired
```

Dashboard: http://SERVER_IP:8082/

## 5. Load demo data (optional)

```bash
./scripts/reseed.sh http://localhost:8000 test_incidents.json
# ~100 real tickets classified + embedded through the live pipeline (~11 min)
# Expect: 91 incidents stored, 0 failed (9 duplicate titles deduped)
# Re-runnable any time — wipes and rebuilds from source (reproducible).
```

## 5.5 Run the test suite (sanity check)

```bash
# MUST point at a test database — the suite refuses to run against the
# production DB (ai_incidents) by design (conftest safety guard).
PG_PORT=5432 uv run pytest tests/ -q
# Expected: ~131 passed + xfail/xpass from the documented flaky canary class
```

## 6. Operational notes

- Logs: `docker compose logs -f api`
- Restart: `docker compose restart api`
- The db stores embeddings in pgvector; `pgdata` volume persists across restarts
- First boot downloads bge-m3 ONCE (baked into the image by the builder stage;
  no runtime download needed)
- Image is ~8.75GB (torch+CUDA). CPU-only torch would shrink it — future
  optimization, not required.
- Integration API (W3): POST /api/v1/incidents (202 async), GET
  /api/v1/incidents/{ref}, POST /api/v1/incidents/dry-run, POST
  /api/v1/backfill, GET /api/v1/jobs — all Bearer-token auth. Full
  contract: docs/INTEGRATION_GUIDE.md
- Sync worker polls TICKETING_API_URL when TICKETING_API_TOKEN is set;
  fails loudly (NotConfiguredError) until then.

## 7. Rollback / update

```bash
git pull && docker compose build api && docker compose up -d
# db persists; tables are CREATE IF NOT EXISTS
```
