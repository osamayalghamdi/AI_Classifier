# ai-classifier — Helm chart (DevOps handover)

Deploys the AI Incident Classifier (FastAPI API + nginx dashboard + optional OCR).
**Application objects only** — everything infrastructure-shaped is a value you set,
or something your platform already owns.

---

## 1. What is in the chart, and what is deliberately not

| In this chart | Not in this chart (you provide / platform owns) |
|---|---|
| `Service` (api, frontend) | **The database** — required, provided by the DB/platform team (§3); platform infrastructure, *not* a third-party service |
| `ConfigMap` (nginx config, body is a value) | Ingress controller, TLS certificates |
| `Deployment` (api, frontend) | Monitoring, HPA, VPA, ServiceMonitor, backups; Secret *contents* (SealedSecrets / ExternalSecrets / Vault) |
| `Ingress` (only when you enable it) | Any other team's CRs (HPAs, ServiceMonitors, …) |

Rule of thumb: if it is not an object this application owns, it does not live in
`templates/` — it comes from values (`database.*`, `ingress.*`).

---

## 1b. Layout — one folder per side (mirrors the repo)

The chart keeps the same front/back split as the repository
(`backend/` + `frontend/`), so what you change is obvious:

```
deploy/helm/ai-classifier/
├── Chart.yaml                  version 0.5.0
├── values.yaml                 the whole configuration (see §4)
├── values-{dev,prod}.example.yaml
└── templates/
    ├── backend/                ← the service (Python API + workers)
    │   ├── deployment.yaml
    │   └── service.yaml
    ├── frontend/               ← the dashboard (nginx)
    │   ├── deployment.yaml
    │   └── configmap.yaml      (the nginx config body is a value)
    ├── shared/
    │   └── ingress.yaml        (off by default)
    ├── _helpers.tpl            naming/label helpers
    └── NOTES.txt               post-install output
```

Values follow the same split: **`backend.*`** (image, env, probes, resources,
secrets), **`frontend.*`** (image, nginx config), the shared keys
(`database.*`, `ingress.*`), and the top-level `image` / `imagePullSecrets`.
Rendered objects are `<release>-backend` and `<release>-frontend`.

## 2. Install

```bash
kubectl create namespace ai-classifier

# Secrets are created out-of-band — never in values files.
kubectl -n ai-classifier create secret generic ai-classifier-secrets \
  --from-literal=LLM_API_KEY='<llm key>' \
  --from-literal=INTEGRATION_API_TOKEN='<admin/api token>'
kubectl -n ai-classifier create secret generic ai-classifier-db \
  --from-literal=PG_PASSWORD='<db password>'

# Optional but recommended: gate the dashboard data paths (see §5)
htpasswd -bc /tmp/auth opsadmin '<password>'
kubectl -n ai-classifier create secret generic ai-classifier-basic-auth \
  --from-file=htpasswd=/tmp/auth && rm /tmp/auth

helm upgrade --install ai-classifier deploy/helm/ai-classifier \
  -n ai-classifier -f values-prod.yaml --wait
```

Then verify:

```bash
kubectl -n ai-classifier rollout status deploy/ai-classifier-backend   # seconds — no model to load
kubectl -n ai-classifier port-forward svc/ai-classifier-backend 8000   # binds 127.0.0.1 — never --address 0.0.0.0
curl -fsS localhost:8000/health
curl -fsS localhost:8000/ready       # db + embedding + LLM reachability
curl -fsS localhost:8000/test/all    # full battery (reach the backend Service directly; /test/* is blocked at the proxy)
```

---

## 3. Database — **required, provided by the platform team (not in this chart)**

The app needs a database to run, but this chart does not deploy one: the
database is **platform infrastructure** that the DB/platform team already runs
(provisioning, HA, backups, upgrades, schema). It is **not a third-party
service** — the only third-party external services are the models (LLM,
embeddings, OCR, vision), which are endpoint values (§4). The chart only wires
the connection.

Requirements on the server they hand over:

- reachable from the cluster (usually `host.svc.cluster.local:5432` or a managed endpoint)
- the **pgvector** extension available, and the app user allowed to run
  `CREATE EXTENSION IF NOT EXISTS vector`
- the app creates its own tables at first startup (idempotent afterwards); the
  role needs create privileges at least once

```yaml
database:
  host: pg.internal            # REQUIRED — the chart fails to render without it
  port: 5432
  name: ai_incidents
  user: aiuser
  existingSecret: ai-classifier-db   # your Secret, key: PG_PASSWORD
  passwordKey: PG_PASSWORD
```

These become `PG_HOST / PG_PORT / PG_USER / PG_DATABASE / PG_PASSWORD` on the api
pod. Need a throwaway database for a dev sandbox? Add it yourself through
your own operator — keeping it out of this chart is deliberate.

---

## 3b. Images — the two you have to build and push

The chart **deploys** images; it never builds them. Two are needed:

| Values that point at it | Dockerfile | Build context | Size |
|---|---|---|---|
| `image.repository` / `image.tag` (backend) | `backend/Dockerfile` | `backend/` | ~1.1 GB |
| `frontend.image.repository` / `.tag` | `frontend/Dockerfile` | `frontend/` | ~95 MB |

```bash
docker build --platform linux/amd64 -t REGISTRY/ai-classifier:<tag>          backend/
docker build --platform linux/amd64 -t REGISTRY/ai-classifier-frontend:<tag> frontend/
docker push REGISTRY/ai-classifier:<tag>
docker push REGISTRY/ai-classifier-frontend:<tag>
```

Then point the chart at them — **set both tags explicitly**:

```bash
helm upgrade --install ai-classifier deploy/helm/ai-classifier \
  --set image.repository=REGISTRY/ai-classifier          --set image.tag=<tag> \
  --set frontend.image.repository=REGISTRY/ai-classifier-frontend \
  --set frontend.image.tag=<tag> \
  --set database.host=<host> --set database.existingSecret=ai-classifier-db
```

Notes for whoever builds and deploys:

- **Do not leave `image.tag` blank.** Blank renders `Chart.appVersion` (a stale `0.2.0` that was never pushed) — always set the tag CI built.
- **Build for the cluster's architecture** (`--platform linux/amd64` unless it is arm64), or pods die with `exec format error`.
- The backend **runs as UID 1000** (non-root, read-only root FS) and both images are built for that; do not override the container user without rebuilding.
- **No build args, no secrets and no model download at build time** — the only network use is `pip install`. Nothing is baked in: LLM, embeddings and OCR are endpoint values (§4).
- **No database image and no OCR image from this chart** — the DB is platform-provided infra (§3) and OCR is a separate deployment the backend reaches via `backend.env.OCR_SERVICE_URL`.
- `imagePullSecrets` is **top-level** (`imagePullSecrets: [{name: regcred}]`), not per component.
- The template renders `repository:tag`, so a `sha256:` digest cannot be pinned as-is — pin an immutable tag (ask us for a one-line chart change if you want digest support).

---

## 4. Values reference

| Key | Default | Notes |
|---|---|---|
| `image.repository` / `.tag` / `.pullPolicy` | `ai_classifier-api` / `latest` / `IfNotPresent` | Local default: the image is built and `kind load`ed, so nothing is pulled. Ships no model: ~1.1 GB |
| `imagePullSecrets` | `[]` | Registry pull secret(s) |
| `backend.replicas` | `1` | **Keep 1** — the process runs in-process workers; see DEPLOYMENT_GUIDE §6 |
| `backend.port` | `8000` | Container/service port |
| `backend.env` | LLM + integration settings | Non-secret env, merged verbatim. **This block is the configuration** — there is no `.env` in the project. Local defaults: `LLM_MODEL=ollama/classifier-qwen` + `LLM_API_BASE=http://<this host>:11434` |

**The models are the only external (third-party) services — the image ships none:**

| Values | Default | What it means |
|---|---|---|
| `backend.env.EMBEDDING_PROVIDER` / `.EMBEDDING_API_URL` / `.EMBEDDING_MODEL_ID` / `.EMBEDDING_DIM` | `external` / `http://<this host>:11434/v1` / `bge-m3` / `1024` | Embeddings for similarity/clustering come from an OpenAI-compatible `/embeddings` endpoint — locally the same Ollama (`ollama pull bge-m3`). `EMBEDDING_DIM` must match what it returns — it is baked into the vector column at first startup. Key: `EMBEDDING_API_KEY` in `backend.existingSecret` (not needed for a local endpoint). |
| `backend.env.OCR_ENABLED` / `.OCR_SERVICE_URL` | `"0"` / `""` | The API POSTs every incident attachment to the OCR service (a **separate deployment**) and stores the returned text on the record (`extracted_text` + file names in `documents`). Off locally → attachments are recorded by name only; set `OCR_ENABLED=1` and the URL where the service exists. |
| `backend.existingSecret` | `ai-classifier-secrets` | `LLM_API_KEY`, `INTEGRATION_API_TOKEN`, `ADMIN_TOKEN`, `TICKETING_API_TOKEN`, `EMBEDDING_API_KEY` — all optional: a local Ollama needs no key, and the API hosts without one (it reports what is missing instead of refusing to start) |
| `backend.resources` | 250m/512Mi → 2/5Gi | No model in the image — the LLM, embeddings and OCR run on external endpoints |
| `backend.probes.*` | `/health` | Switch readiness to `/ready` once LLM egress is confirmed |
| `backend.strategy` | `Recreate` | No overlap between replicas (worker duplication) |
| `backend.service.type` / `.port` | `ClusterIP` / `""` | Avoid `NodePort`/`LoadBalancer` outside dev |
| `frontend.enabled` / `.replicas` / `.port` | `true` / `1` / `8082` | nginx dashboard + API proxy |
| `frontend.image.*` | `ai-classifier-frontend:latest` | Baked dashboard files (locally built + `kind load`ed) |
| `frontend.pullPolicy` | `IfNotPresent` | Same reason as the backend image |
| `frontend.service.*` | `ClusterIP` | Same warnings as the backend Service |
| `frontend.configMap.conf` | nginx server block | The whole config is a value (rendered with `tpl`) |
| `database.*` | host `db.platform-db.svc.cluster.local` (local) | §3 — the DB is required infrastructure, wired here, never deployed by the chart |
| `ingress.enabled` | **`false`** | Turning it on publishes the app — opt in per environment |
| `ingress.className` / `.host` / `.tls` / `.annotations` | `""` / example / `[]` / `{}` | Ask your controller which class; add TLS before exposing. Routing is fixed (everything → frontend, or the API when `frontend.enabled=false`) |

---

## 5. Exposure (defaults publish nothing)

| Surface | Default | To publish / protect |
|---|---|---|
| API + dashboard outside the cluster | nothing | `ingress.enabled: true` (+ `ingress.tls`) |
| Service types | `ClusterIP` | `*.service.type` (`NodePort`/`LoadBalancer` publish on every node / via a cloud LB) |
| Data routes (`/reset`, `/test/*`, `/docs`, `/redoc`, `/openapi.json`) | **403** in the proxy | edit `frontend.configMap.conf` if a route must be reachable |
| Dashboard data (`/incidents`, `/status`, `/reports/*`, …) | open to anyone who can reach the frontend | gate it at the platform (SSO / VPN) — the chart ships no login |

Routing is fixed: everything goes through the frontend, which proxies the API
paths to the backend. `/health` and `/ready` stay ungated on purpose, so probes
and monitors keep working.

`kubectl port-forward` binds `127.0.0.1` by default — correct for inspecting a
deployment. Never add `--address 0.0.0.0`: it publishes the dashboard on every
host interface, VPN/Tailscale included.

---

## 6. Environment overlays

Copy `values-dev.example.yaml` / `values-prod.example.yaml` into your repo as
`values-dev.yaml` / `values-prod.yaml` and keep **only the deltas**:

```bash
helm upgrade --install ai-classifier deploy/helm/ai-classifier -n ai-classifier -f values-prod.yaml
```

ArgoCD/Flux: point the app at the chart + the env values file; the only routine
change should be the image tag.

---

## 7. Guards — the chart refuses to render when a setting is unsafe

| Guard | Message |
|---|---|
| `database.host` empty | "this chart does not deploy a database — point it at the server your platform team provides" |

These exist because each one otherwise fails *silently* at runtime.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| **Not sure what is configured or what is missing** | run `backend/scripts/doctor.sh` — it prints the pod's real env, `/ready`, whether the model server answers *from the pod*, the secrets, and names the variable or command for each problem (read-only, exit 1 when something blocks) |
| `ImagePullBackOff` on the backend | wrong registry/repository/tag, missing `imagePullSecrets`, or an image built for another architecture |
| backend pod `0/1` for minutes then Ready | first boot creates/validates its tables — a slow or unreachable DB extends it; `/health` is up meanwhile |
| `/ready` reports `llm: not configured: …` | no model selected (`LLM_MODEL` unset / its registry entry off) — the message names the variables; the app keeps hosting and only classification degrades |
| `/ready` reports `llm: slow: no answer within Ns` | a local CPU model needs longer than a hosted API's ping: raise `backend.env.LLM_PROBE_TIMEOUT_S` (60 locally) |
| `/ready` reports `llm: unreachable` but the model works on the host | a pod cannot use `localhost`: point `backend.env.LLM_API_BASE` at the host's LAN address |
| `/ready` reports `llm: error` | LLM egress blocked from the cluster or `LLM_API_KEY` missing — `/health` and the dashboard still work |
| Changed `values.yaml` and nothing happened | the pod holds the ConfigMap from its start: `helm upgrade …`, and if the code changed, rebuild + `kind load docker-image` first. `doctor.sh` fails on this drift by name |
| readiness flapping | readiness is pointing at `/ready` while LLM egress is flaky — keep it on `/health` |

---

## 9. Who owns what

| Area | Owner |
|---|---|
| Chart, images, application config, the values reference above | Application team |
| Environment values (per env), GitOps wiring, secrets delivery | DevOps / platform |
| Database server, pgvector extension, backups, HA, credentials | Database / platform team |
| Ingress controller, TLS, SSO/oauth2-proxy | Platform team |

Deeper app-specific notes (probe choices, worker duplication, first-startup DDL,
dangerous endpoints): see `../../DEPLOYMENT_GUIDE.md`.
