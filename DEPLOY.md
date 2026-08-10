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
```

## 1. Get the code

```bash
cd ~
git clone git@github.com:osamayalghamdi/AI_Classifier.git
cd AI_Classifier
git checkout feat/suboffering-clustering   # or the merged release branch
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

The compose file passes these straight through to the api container.
Verify from the server BEFORE starting (ELM must resolve from the server):

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://llms.elm.sa/v1/models
# expect 200 (401/403 also OK = reachable; 000 = DNS/network problem)
```

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
curl -s -X POST http://localhost:8000/import/test_incidents.json
# ~100 real tickets classified through the live pipeline (~10 min)
```

## 6. Operational notes

- Logs: `docker compose logs -f api`
- Restart: `docker compose restart api`
- The db stores embeddings in pgvector; `pgdata` volume persists across restarts
- First boot downloads bge-m3 ONCE (baked into the image by the builder stage;
  no runtime download needed)
- Image is ~8.75GB (torch+CUDA). CPU-only torch would shrink it — future
  optimization, not required.

## 7. Rollback / update

```bash
git pull && docker compose build api && docker compose up -d
# db persists; tables are CREATE IF NOT EXISTS
```
