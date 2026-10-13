# AI Classifier — Kubernetes / Helm Deployment Guide

Companion to the `ai-classifier/` Helm chart in this folder. **DevOps/values reference: `deploy/helm/ai-classifier/README.md`.** Read the **gotchas** section before the first deploy — several are specific to how this app is built, not generic K8s advice.

---

## 1. What gets deployed

| Component | Kind | Notes |
|---|---|---|
| `api` | Deployment (replicas **must stay 1** for now) | FastAPI + in-process workers (sync poller, cluster sweep, status monitor, integration retry worker). **No model is baked in**: the LLM, the embeddings (`EMBEDDING_API_URL`) and the OCR (`OCR_SERVICE_URL`) are all external endpoints. Incident attachments are POSTed to the OCR endpoint and the transcription is stored on the record (`extracted_text` + file names in `documents`). |
| `frontend` | Deployment + ConfigMap | nginx serving the dashboard, proxying API paths to the backend Service. **Needs a new image** — see §3. |
| `ocr` | **NOT in this chart** | The OCR service is a separate deployment (own image, own endpoint). The API only needs `backend.env.OCR_SERVICE_URL`. |
| `ingress` | Ingress (**off by default**) | Single host → frontend (which proxies API), same shape as the compose stack. Turning it on publishes the app — opt in per environment (§1.2). Routing itself is a value — see §1.1. |

There is **no database object in this chart**: the database is **required platform infrastructure**, owned by the DB/platform team (README §3) — not a third-party service (only the models are). The chart only takes connection values and fails the render if `database.host` is empty.

Secrets are **never** in values files — they come from pre-created K8s Secrets (`backend.existingSecret`, `database.existingSecret`), ideally via whatever the DevOps team standard is (ExternalSecrets / SealedSecrets / Vault).

### 1.1 Everything is a value (no hardcoded resources)

Every field of every object this chart renders — Service, ConfigMap, Deployment, Ingress — comes from `values.yaml` / the env overlay. Nothing environment-specific lives inside a template, so an environment change is a **values** change, never a chart change.

| What | Where |
|---|---|
| Service type / port | `backend.service.*`, `frontend.service.*` |
| nginx ConfigMap (the whole file body) | `frontend.configMap.conf` — rendered with `tpl`, so chart helpers (`{{ include "ai-classifier.fullname" . }}`) work inside it |
| Deployment shape | `backend.strategy`, `backend.replicas`, `backend.resources`, `backend.probes`, `backend.env` |
| Ingress | `ingress.enabled`, `ingress.host`, `ingress.className`, `ingress.annotations`, `ingress.tls` |

The surface is deliberately small. Anything not listed here was removed on purpose — if an environment needs another knob, it's a few-line addition. Routing is fixed (everything → the frontend, or the API when `frontend.enabled=false`).

Third-party infrastructure is **not** shipped in this chart:

- **Database** — required platform infrastructure (`database.host/port/name/user/existingSecret`), provided by the DB/platform team — not a third-party service. The chart only wires the connection; provisioning, backups, the pgvector extension and the schema belong to the DB/platform team.
- `backend.env.EMBEDDING_*` — embeddings come from an OpenAI-compatible `/embeddings` endpoint (`EMBEDDING_API_URL`, `EMBEDDING_MODEL_ID`, `EMBEDDING_DIM`). **`EMBEDDING_DIM` is baked into the vector column on first startup** — set it before the first run; changing it later needs a fresh schema.
- `backend.env.OCR_ENABLED` / `backend.env.OCR_SERVICE_URL` — attachment OCR through a separate service; empty URL records attachments by name only.

The chart only owns AI-Classifier objects; anything owned by another team or operator (monitoring, HPA, CRs) is deployed separately, not through this chart.

### 1.2 Exposure — what is reachable, and when

The defaults publish nothing:

| Surface | Default | What it takes to publish it |
|---|---|---|
| API / dashboard outside the cluster | **nothing** | `ingress.enabled: true` (opt in per environment) — pair it with `ingress.tls` |
| Service types | `ClusterIP` for api, frontend, ocr | setting `*.service.type: NodePort`/`LoadBalancer` publishes on every node / via a cloud LB — avoid outside dev |
| Data-revealing routes through the proxy | `/reset`, `/test/*`, `/docs`, `/redoc`, `/openapi.json` → **403** at nginx | edit `frontend.configMap.conf`, and justify why |
| Database | **not in this chart at all** | the platform/DB team provides the server; the chart only takes `database.*` connection values (§ README §3) |

`kubectl port-forward` binds `127.0.0.1` by default and is the sanctioned way to look at a deployment. **Never** add `--address 0.0.0.0`: that publishes the dashboard on every host interface — including a VPN/Tailscale interface. (Learned the hard way during the kind try-out: the forward showed connections from a Tailscale peer.)

### 1.3 Protecting the data paths — the platform gates it, not the chart

The dashboard itself has no login, so the read endpoints (`/incidents`, `/all-incidents`, `/status`, `/reports/*`, `/clusters`, `/review-queue`) serve incident data to whoever can reach the frontend. **The chart ships no access control** — gate the app outside it:

**a) Platform SSO / external auth (recommended)** — let the ingress controller delegate to oauth2-proxy; it is pure `ingress.annotations`, no chart or app change:

```yaml
ingress:
  enabled: true
  annotations:
    nginx.ingress.kubernetes.io/auth-url: "https://oauth2.example.com/oauth2/auth"
    nginx.ingress.kubernetes.io/auth-signin: "https://oauth2.example.com/oauth2/start?rd=$scheme://$host$request_uri"
    nginx.ingress.kubernetes.io/auth-response-headers: "x-auth-request-user, x-auth-request-email"
```

**b) Network-level** — keep `ingress.enabled: false` (the app is then reachable in-cluster only) and/or restrict at the platform (VPN, firewall, a `NetworkPolicy` the platform team owns).

Keep `/health` and `/ready` reachable — they are served ungated on purpose, so the ingress controller and external monitors can still probe the app.

## 2. Prerequisites checklist — questions for the DevOps team

Ask these **before** the first deploy; the answers change values files:

1. **Registry:** which container registry, and what are the size limits? The API image is **~1.1 GB** (no model — embeddings/OCR/LLM are external endpoints).
2. **Egress:** the API must reach the LLM endpoint (`https://llms.elm.sa/v1` or OpenRouter) from inside the cluster. If there's an egress proxy or NetworkPolicy default-deny, we need an allowance. Without it, classification silently degrades to low-confidence fallbacks — **test the LLM path from a pod on day one** (`curl -H "Authorization: Bearer $INTEGRATION_API_TOKEN" .../test/llm` — the endpoint is token-gated now, see gotcha #7).
3. **Postgres (required platform infra, their responsibility):** we do **not** deploy a database (only the models are third-party external services). Ask for host / port / database name / user, and how the password reaches the namespace (their secret tooling). Requirement on the server: the **pgvector extension installed** and the app role permitted to run `CREATE EXTENSION IF NOT EXISTS vector` — the app also creates its own tables at first startup. Backups, HA and upgrades are theirs; if they cannot provide pgvector, the app cannot start (that is a hard blocker, not a config we can work around).
4. **Ingress controller + TLS:** which ingress class, who issues certs (cert-manager?), and what hostname do we get.
5. **Secrets workflow:** how do secrets land in the namespace in their GitOps flow (we must not commit them to the DevOps repo).
6. **GitOps tooling:** ArgoCD or Flux or plain `helm upgrade` in CI? This decides the repo layout (§5).

## 3. Images to build (CI in the DevOps repo)

| Image | Dockerfile | Context |
|---|---|---|
| `ai-classifier` (API) | `backend/Dockerfile` | `backend/` |
| `ai-classifier-frontend` | `frontend/Dockerfile` | `frontend/` |
| `ai-classifier-ocr` (optional) | `backend/ocr/Dockerfile` | `backend/ocr/` |

Why a frontend image: the dashboard files must be baked into an image (a stock nginx + the files). The nginx **config** stays out of the image (it's the chart's ConfigMap, whose body is `frontend.configMap.conf` in values — the proxy target is a cluster DNS name Helm computes, and `tpl` lets values reference it).

Tagging rule: immutable tags (git SHA or semver), never `latest` in prod. The chart defaults `image.tag` to `Chart.appVersion` when empty.

## 4. First deploy — step by step

```bash
# 0) namespace
kubectl create namespace ai-classifier

# 1) secrets (or via the team's secret tooling)
kubectl -n ai-classifier create secret generic ai-classifier-secrets \
  --from-literal=LLM_API_KEY='<key>' \
  --from-literal=INTEGRATION_API_TOKEN='<token>' \
  --from-literal=TICKETING_API_TOKEN=''            # empty until SMAX creds exist
kubectl -n ai-classifier create secret generic ai-classifier-db \
  --from-literal=PG_PASSWORD='<pw>'             # for the DB they provide

# 2) install
helm upgrade --install ai-classifier deploy/helm/ai-classifier \
  -n ai-classifier \
  -f values-dev.yaml            # your env overlay (see *.example.yaml)

# 3) verify — in this order
kubectl -n ai-classifier rollout status deploy/ai-classifier-backend   # seconds — no model to load
kubectl -n ai-classifier port-forward svc/ai-classifier-backend 8000 &
curl -fsS localhost:8000/health      # liveness
curl -fsS localhost:8000/ready       # db + embedding + llm reachability
curl -fsS localhost:8000/test/all    # full battery incl. a real classify call
```

If `/test/all` shows `llm: error` → egress problem (checklist #2). If `db: error` → check PG secret/host. The app **fails loudly at startup** if `LLM_MODEL` is unset or an OpenRouter model has no key — that's by design (lifespan guard), not a chart bug.

## 5. DevOps repo layout (suggested)

```
devops-repo/
├── charts/ai-classifier/          # this chart, vendored (or referenced from the app repo via CI publish)
├── envs/
│   ├── dev/values.yaml            # from values-dev.example.yaml
│   └── prod/values.yaml           # from values-prod.example.yaml
└── (ArgoCD Application / Flux HelmRelease pointing chart→env values)
```

Two workable ownership models — pick with the DevOps team:
- **Chart lives in the app repo**, CI packages + pushes it to a chart/OCI registry on tag; DevOps repo only holds env values and the ArgoCD/Flux definition. (Cleaner: chart changes ride with app changes.)
- **Chart lives in the DevOps repo**; app CI only pushes images and bumps the tag in env values (e.g. via a bot PR). (Simpler if DevOps wants full control.)

Either way: **the only thing the app team should routinely change in the DevOps repo is the image tag** — everything else is config review.

## 6. App-specific gotchas (the important part)

1. **No models in the cluster — three endpoints instead.** The API image ships no model (no torch, no weights; the build asserts it). It needs: `LLM_API_BASE` + key (classification), `EMBEDDING_API_URL` (similarity/clustering) and `OCR_SERVICE_URL` (attachments). Consequence to plan for: **the pods now need egress to all three**, and `EMBEDDING_DIM` must match what the embeddings endpoint returns, because the `vector(N)` columns are created on the first startup. A wrong dimension shows up as an `ERROR … returned dim X but this DB's column is vector(N)` line and a skipped similarity search — fix it and start on a fresh schema.
2. **`backend.replicas` must stay 1.** The API process itself runs the sync poller, the cluster sweep worker, the status monitor, and the integration retry worker as daemon threads. Two replicas = two sweeps and two pollers racing. The deployment uses `strategy: Recreate` for the same reason (no overlap during rollout — accept the brief downtime). The real fix is splitting workers into their own Deployment/CronJobs — that's part of the refactor plan (`REFACTOR_AND_SMAX_INTEGRATION_PLAN.md`), after which the API becomes horizontally scalable.
3. **Writable filesystem required.** The sync worker writes a `.last_sync` stamp inside the app package directory (and note: due to a path bug it currently lands at `ai_classification/.last_sync` — documented as BUG-3 in the refactor plan). Consequences for K8s: (a) don't enable `readOnlyRootFilesystem` in any PodSecurity settings yet; (b) the stamp is lost on every pod restart → the poller re-scans a wider window. That's *safe* (server-side dedupe is content-hash gated) but wasteful. Once BUG-3 is fixed with a `SYNC_STAMP_PATH` env var, point it at an `emptyDir` (or drop the file entirely and store the stamp in Postgres — the better long-term fix for K8s).
4. **Readiness probe choice.** `/ready` genuinely probes db + embedding + **LLM reachability**. That's great for honesty but means a flaky LLM endpoint takes your pod out of the Service. Chart default: readiness on `/health`, switch to `/ready` (prod overlay shows how) only after confirming stable LLM egress. Liveness must stay `/health` — never `/ready` (an LLM outage must not restart-loop the pod).
5. **Slow requests.** A `/classify` call runs a multi-stage LLM cascade — can take tens of seconds. The chart's nginx ConfigMap sets `proxy_read_timeout 300s`; the prod ingress overlay sets the same on the ingress controller. If DevOps has a global LB timeout < 300s, raise it for this host or route external systems to the **async** API (`POST /api/v1/incidents` → 202 + poll), which was built exactly to avoid long-held connections.
6. **First startup DDL.** The app creates its own schema (`CREATE EXTENSION vector`, `CREATE TABLE IF NOT EXISTS ...`) at boot. With managed PG, the app role needs those privileges at least once; afterwards it's idempotent. Also run `scripts/migrate_classifier_v3.py up` once against a fresh prod DB (or verify the app-level DDL covers v3 columns on your version).
7. **Dangerous endpoints exposed.** `POST /reset` deletes ALL incidents and `/test/llm` spends LLM tokens; both used to sit on the same unauthenticated surface as the dashboard endpoints (only `/api/v1/*` had bearer auth). In compose this was fine (private host); behind a real ingress it is not. **This is now closed in two layers:**
   - **App-level (the real fix):** both endpoints require the same Bearer token as `/api/v1/*` (`INTEGRATION_API_TOKEN`, via `api/auth.py` → `require_token`). This works regardless of network path — ingress, pod-to-pod, port-forward, anything. 401 without a valid token. While testing on a machine of your own you can set `AUTH_OPTIONAL=1` (in `backend.env`) to make that guard optional: every guarded endpoint then accepts a request with no token — or a stale one, which is what saves you when a token is rotated out from under you — and `/admin/*` opens too. It logs a loud warning at startup; leave it off for anything shared or exposed.
   - **nginx (chart default):** the frontend ConfigMap hard-blocks `location = /reset` and `location = /test/llm` with `return 403` (exact-match locations that win over the proxy regex and the SPA fallback) — defense in depth, zero cost.
   - Residuals: `/reset`, `/test/llm`, the whole `/test/` surface and the schema/docs routes (`/docs`, `/redoc`, `/openapi.json`) are now blocked with 403 in the chart's nginx config, so they are not reachable from outside even if the host is exposed. In-cluster access to the backend Service is fine (that's how smoke tests run); just never port-forward the pod with `--address 0.0.0.0`.
8. **CORS is `*`** in the app. Harmless while the frontend proxies same-origin, but tighten it (env-configurable origin) before exposing the API host directly to browsers.
9. **SMAX polling in K8s:** leave `TICKETING_API_TOKEN` empty until real SMAX creds exist — the app logs one line and idles (by design, no crash-loop). The `integrations/smax` connector keeps its own configuration and its own service definition (`.env.example` + README, inside that folder), so nothing SMAX-specific belongs in this chart's values. If the platform team wants it as a cluster workload rather than a sidecar process next to the app, the manifest belongs in that folder too and should reference the same `SMAX_*` / `CLASSIFIER_API_*` names rather than inventing chart-side copies of them.

## 7. Resource sizing (starting points, tune from metrics)

| Component | Request | Limit | Why |
|---|---|---|---|
| api | 250m / 512Mi | 1 / 2Gi | No model in the image; the LLM, embeddings and OCR are external endpoints |
| frontend | 50m / 64Mi | 200m / 128Mi | static nginx |
| ocr | — (not in this chart) | — | The OCR service is a separate deployment; the api only calls it over HTTP |

No HPA for the api (see gotcha #2). No PVC in this chart — the database runs on the platform's storage, not ours.

## 8. Rollout / rollback

- Deploy = new image tag in env values → `helm upgrade` (or ArgoCD sync). `Recreate` strategy → a few seconds of downtime while the old pod stops and the new one starts (nothing to load, so it is quick); schedule accordingly or prioritize the worker split.
- Rollback = `helm rollback` / revert the tag. DB schema is additive-idempotent, so rolling the app back is safe; **never** run `migrate_classifier_v3.py --down` against a DB that newer code wrote to.
- Post-deploy check is always the same three: `/health`, `/ready`, `/test/all`.

## 8b. Running it: what to watch, and the per-ticket time budget

**Watch these — they tell you tickets are being lost:**

- `curl -fsS <api>/test/all` (token) after every change: llm / db / embedding / taxonomy battery.
- The connector's write-back sweep line (every `SMAX_POLL_S`):
  `Write-back sweep: checked= ready= written= dry_run= pending= requeued= abandoned= skipped=`
  A **growing `abandoned` or `skipped`** means results are not reaching SMAX; a growing `pending` means the API is slower than the sweep.
- `GET /api/v1/jobs` (token) — jobs sitting in `retryable` / `flagged`, with their error code.

**Per-ticket time budget.** The worker claims jobs with `FOR UPDATE SKIP LOCKED` and processes them **serially**, so one slow ticket delays everything behind it. Worst case for ONE incident with the shipped defaults:

```
LLM_TIMEOUT_S=60 × (1 + LLM_MAX_RETRIES=3 = 4 attempts) × ~3 LLM calls per cascade run
              × (1 + CLASSIFY_CASCADE_RETRIES=2 runs)   ≈ 36 minutes
```

There is **no per-ticket deadline** — every call can burn its full timeout. For production, lower the knobs (e.g. `LLM_TIMEOUT_S=30`, `LLM_MAX_RETRIES=2`, `CLASSIFY_CASCADE_RETRIES=1` → ~3 minutes worst case) and treat a threaded deadline through the LLM calls as the real fix (follow-up, not a config value).

**Backup / restore before the first ingest.** The database belongs to the platform team, but take one anyway (`pg_dump`), keep the matching restore command next to it, and verify the restore once on a scratch DB. Nothing outside Postgres is stateful — the poller's sync-stamp file is throwaway, and incidents/embeddings/clusters all live in the DB.

**Rollback order** (matters): ① stop the SMAX connector (it is the writer of new tickets), ② `helm rollback` / revert the image tag, ③ restart the API. Schema changes are additive-idempotent, so an app rollback is safe — **never** run `migrate_classifier_v3.py --down` against a DB that newer code has written to.

**Dedupe rule worth knowing when you triage:** idempotency is by **ticket id**; the content hash (digit-blanked title + description) only gates feeds that arrive *without* a ticket id. Two different SMAX tickets with identical text are two incidents — that is deliberate, because merging them would drop the second ticket's reference and its later status updates would match nothing.

## 9. Admin console (`/admin.html`)

A bearer-auth admin page (same token as `/api/v1/*`) at `/admin.html` covering:
- **Status** — incident/cluster/embedding counts, model, store health.
- **Taxonomy** — add services/offerings on top of the FROZEN base taxonomy. Overrides persist in the `taxonomy_overrides` table and take effect immediately (the cascade + validation read the merged view at call time; the base `domain/taxonomy.py` never changes).
- **Credentials** — in Kubernetes the env comes from the Deployment (chart values + the Secret), not a writable file: set `LLM_API_KEY` / `INTEGRATION_API_TOKEN` in the Secret and `helm upgrade`. Settings load at startup → **restart the pod to apply**. Secrets are never returned in full (masked only).
- **Add incident** — classify + store one ticket.
- **Groups** — create/adjust cluster groups (rename, status, add/remove members).
- **Run tests** — in-container `smoke_test.sh` (against the live API) and the full `pytest` suite (against `ai_incidents_test`). Requires `tests/`, `scripts/`, `smoke_test.sh` mounted into the api container (compose does this) and pytest in the image (Dockerfile installs it).
- **Danger zone** — full DB reset (incidents + clusters + review queue + ingestion jobs).

Notes for prod: `/admin/*` is proxied by nginx (the values-driven ConfigMap regex includes `admin/`), so the same ingress-level protections apply — the bearer token is the only auth, keep the host internal. The test-runner endpoints execute subprocesses in the container; keep them behind the token (they already are).
