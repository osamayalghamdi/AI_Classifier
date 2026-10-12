# ai-classifier — Helm chart (DevOps handover)

Deploys the AI Incident Classifier (FastAPI API + nginx dashboard + optional OCR).
**Application objects only** — everything infrastructure-shaped is a value you set,
or something your platform already owns.

---

## 1. What is in the chart, and what is deliberately not

| In this chart | Not in this chart (you provide / platform owns) |
|---|---|
| `ServiceAccount`, `Service` (api, frontend) | **The database** — external, managed by the DB/platform team (§3) |
| `ConfigMap` (nginx config, body is a value) | Ingress controller, TLS certificates |
| `Deployment` (api, frontend) | Monitoring, HPA, VPA, ServiceMonitor, backups |
| `NetworkPolicy` (on by default) | Secret *contents* (SealedSecrets / ExternalSecrets / Vault) |
| `Ingress` (only when you enable it) | Any other team's CRs → inject via `extraManifests` |

Rule of thumb: if it is not an object this application owns, it does not live in
`templates/` — it comes from values (`database.*`, `ingress.*`, `extraManifests`).

---

## 1b. Layout — one folder per side (mirrors the repo)

The chart keeps the same front/back split as the repository
(`backend/` + `frontend/`), so what you change is obvious:

```
deploy/helm/ai-classifier/
├── Chart.yaml                  version 0.4.0
├── values.yaml                 the whole configuration (see §4)
├── values-{dev,prod}.example.yaml
└── templates/
    ├── backend/                ← the service (Python API + workers)
    │   ├── deployment.yaml
    │   └── service.yaml
    ├── frontend/               ← the dashboard (nginx)
    │   ├── deployment.yaml
    │   └── configmap.yaml      (the nginx config body is a value)
    ├── shared/                 ← platform objects, both sides
    │   ├── ingress.yaml        (off by default)
    │   ├── networkpolicy.yaml  (on by default)
    │   ├── serviceaccount.yaml
    │   └── extra-manifests.yaml (third-party objects, from values)
    ├── _helpers.tpl            naming/label helpers
    └── NOTES.txt               post-install output
```

Values follow the same split: **`backend.*`** (image, env, probes,
resources, secrets), **`frontend.*`** (image, nginx config, access control),
and the shared keys (`database.*`, `ingress.*`, `networkPolicy.*`,
`extraManifests`). Rendered objects are `<release>-backend` and
`<release>-frontend`.

> Renamed in chart **0.4.0**: the values key `api:` → `backend:`, the
> templates moved into `templates/backend|frontend|shared/`, and the workload
> names became `…-backend` / `…-frontend`. `service: api` in an ingress path
> now fails the render with a message telling you to use `backend`.

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
kubectl -n ai-classifier rollout status deploy/ai-classifier-api      # first boot loads the model (~1–5 min)
kubectl -n ai-classifier port-forward svc/ai-classifier-api 8000      # binds 127.0.0.1 — never --address 0.0.0.0
curl -fsS localhost:8000/health
curl -fsS localhost:8000/ready       # db + embedding + LLM reachability
curl -fsS localhost:8000/test/all    # full battery (reach the api Service directly; /test/* is blocked at the proxy)
```

---

## 3. Database — **external, not managed by this chart**

The chart only wires the connection. The team that owns the database is
responsible for provisioning, HA, backups, upgrades and the schema.

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
`extraManifests` (or your operator) — keeping it out of this chart is deliberate.

---

## 4. Values reference

| Key | Default | Notes |
|---|---|---|
| `nameOverride` / `fullnameOverride` | `""` | Override the generated names |
| `image.repository` / `.tag` / `.pullPolicy` | `REGISTRY/ai-classifier` / `""` / `IfNotPresent` | Tag empty → `Chart.appVersion`; the image is multi-GB (`bge-m3` baked in) |
| `imagePullSecrets` | `[]` | Registry pull secret(s) |
| `serviceAccount.create` / `.name` / `.annotations` | `true` / `""` / `{}` | IRSA/workload-identity annotations go here |
| `backend.replicas` | `1` | **Keep 1** — the process runs in-process workers; see DEPLOYMENT_GUIDE §6 |
| `backend.port` | `8000` | Container/service port |
| `backend.env` | LLM + integration settings | Non-secret env, merged verbatim |

**Every capability is an external endpoint — the image ships no model:**

| Values | Default | What it means |
|---|---|---|
| `backend.env.EMBEDDING_PROVIDER` / `.EMBEDDING_API_URL` / `.EMBEDDING_MODEL_ID` / `.EMBEDDING_DIM` | `external` / — / `bge-m3` / `1024` | Embeddings for similarity/clustering come from an OpenAI-compatible `/embeddings` endpoint. `EMBEDDING_DIM` must match what it returns — it is baked into the vector column at first startup. Key: `EMBEDDING_API_KEY` in `backend.existingSecret` (optional). |
| `backend.env.OCR_ENABLED` / `.OCR_SERVICE_URL` | `"1"` / `https://ocr.example.internal/ocr` | The API POSTs every incident attachment to the OCR service (a **separate deployment**) and stores the returned text on the record (`extracted_text` + file names in `documents`). Empty URL → attachments recorded by name only. |
| `backend.extraEnv` | `[]` | Extra env (`{name, value}` or `valueFrom`) |
| `backend.existingSecret` | `ai-classifier-secrets` | `LLM_API_KEY`, `INTEGRATION_API_TOKEN`, `ADMIN_TOKEN`, `TICKETING_API_TOKEN`, `EMBEDDING_API_KEY` (all optional except `LLM_API_KEY`) |
| `backend.resources` | 250m/512Mi → 2/5Gi | No model in the image — the LLM, embeddings and OCR run on external endpoints |
| `backend.securityContext` | non-root, no caps | Runs as UID 1000 |
| `backend.probes.*` | `/health` | Switch readiness to `/ready` once LLM egress is confirmed |
| `backend.strategy` | `Recreate` | No overlap between replicas (worker duplication) |
| `backend.podAnnotations` / `.nodeSelector` | `{}` | Scheduling hints (e.g. pin to the node pool caching the big image) |
| `api.service.type` / `.port` / `.annotations` | `ClusterIP` / `""` / `{}` | Avoid `NodePort`/`LoadBalancer` outside dev |
| `frontend.enabled` / `.replicas` / `.port` | `true` / `1` / `8082` | nginx dashboard + API proxy |
| `frontend.image.*` | `REGISTRY/ai-classifier-frontend` | Baked dashboard files |
| `frontend.service.*` | `ClusterIP` | Same warnings as the api Service |
| `frontend.accessControl.allowlist` | `[]` | nginx `allow/deny` in front of the data paths |
| `frontend.accessControl.basicAuth.enabled` / `.existingSecret` / `.realm` | `false` / `""` / `AI Classifier` | htpasswd Secret; render fails without `existingSecret` |
| `frontend.configMap.conf` | nginx server block | The whole config is a value (rendered with `tpl`) |
| `ocr.enabled` / `.image.*` / `.port` / `.env` / `.existingSecret` | `false` / our image / `8001` / company endpoint / `""` | OCR sidecar — calls the company vision endpoint (`ELM_API_URL`/`ELM_MODEL`, key from `.existingSecret`); no local model, so the render fails when enabled without the secret |
| `database.*` | host required | §3 |
| `ingress.enabled` | **`false`** | Turning it on publishes the app — opt in per environment |
| `ingress.className` / `.host` / `.tls` / `.annotations` | `""` / example / `[]` / `{}` | Ask your controller which class; add TLS before exposing |
| `ingress.paths` | `[]` | Blank → everything to the frontend. Set to own the routing (`service: api\|frontend\|ocr`, or `serviceName:`) |
| `extraManifests` | `[]` | Any extra object (HPA, ServiceMonitor, ExternalSecret, CRs) — rendered with `tpl` |
| `networkPolicy.enabled` | **`true`** | Only frontend pods may reach the api; kubelet probes and port-forward still work |

---

## 5. Exposure & access control (defaults publish nothing)

| Surface | Default | To publish / protect |
|---|---|---|
| API + dashboard outside the cluster | nothing | `ingress.enabled: true` (+ `ingress.tls`) |
| Service types | `ClusterIP` | `*.service.type` (`NodePort`/`LoadBalancer` publish on every node / via a cloud LB) |
| Pod-to-pod access to the api | `networkPolicy.enabled: true` | it is the default; turning it *off* is the deliberate act |
| Data routes (`/reset`, `/test/*`, `/docs`, `/redoc`, `/openapi.json`) | **403** in the proxy | edit `frontend.configMap.conf` if a route must be reachable |
| Dashboard data (`/incidents`, `/status`, `/reports/*`, …) | open to anyone who can reach the frontend | `frontend.accessControl.basicAuth` **or** `.allowlist` **or** platform SSO via `ingress.annotations` (DEPLOYMENT_GUIDE §1.3) |

Two rules:

- **Route everything through the frontend** (leave `ingress.paths` empty). A path routed straight to the api Service skips the frontend gate, and the chart then requires `networkPolicy.ingressController.namespace` so the controller can reach the api at all.
- `/health` and `/ready` stay ungated on purpose, so your probes and monitors keep working.

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
| `frontend.accessControl.basicAuth.enabled` without `existingSecret` | "existingSecret is required when basicAuth.enabled=true" |
| an ingress path routed to `api` (or `frontend.enabled=false`) while `networkPolicy.enabled=true` and `networkPolicy.ingressController.namespace` is empty | "the api must accept traffic from outside the frontend … set networkPolicy.ingressController.namespace" |

These exist because each one otherwise fails *silently* at runtime.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ImagePullBackOff` on the api | registry/image tag wrong, or the multi-GB image is still pulling |
| api pod `0/1` for minutes then Ready | first boot loads `bge-m3` (up to ~5 min; `startupProbe` allows it) |
| `/ready` reports `llm: error` | LLM egress blocked from the cluster or `LLM_API_KEY` missing — `/health` and the dashboard still work |
| **504** through the ingress on `/api/*` | path routed straight to the api Service while the NetworkPolicy only admits the frontend → set `networkPolicy.ingressController.namespace` |
| **401/403** on the dashboard | `frontend.accessControl` is on: authenticate, or your source IP is outside `allowlist` |
| readiness flapping | readiness is pointing at `/ready` while LLM egress is flaky — keep it on `/health` |
| pods killed by PodSecurity | API image runs as UID 1000; the OCR image needs the default `securityContext` (no `runAsNonRoot`) |

---

## 9. Who owns what

| Area | Owner |
|---|---|
| Chart, images, application config, the values reference above | Application team |
| Environment values (per env), GitOps wiring, secrets delivery | DevOps / platform |
| Database server, pgvector extension, backups, HA, credentials | Database / platform team |
| Ingress controller, TLS, SSO/oauth2-proxy, NetworkPolicy exceptions | Platform team |

Deeper app-specific notes (probe choices, worker duplication, first-startup DDL,
dangerous endpoints): see `../../DEPLOYMENT_GUIDE.md`.
