# AI Classifier — Kubernetes / Helm Deployment Guide

Companion to the `ai-classifier/` Helm chart in this folder. Read the **gotchas** section before the first deploy — several are specific to how this app is built, not generic K8s advice.

---

## 1. What gets deployed

| Component | Kind | Notes |
|---|---|---|
| `api` | Deployment (replicas **must stay 1** for now) | FastAPI + in-process workers (sync poller, cluster sweep, status monitor, integration retry worker). Embedding model bge-m3 baked into the image. |
| `frontend` | Deployment + ConfigMap | nginx serving the dashboard, proxying API paths to the api Service. **Needs a new image** — see §3. |
| `postgres` | StatefulSet (optional) | `pgvector/pgvector:pg17`. Dev/test only — prod should use external/managed PG. |
| `ocr` | Deployment (optional, off by default) | EasyOCR sidecar service. |
| `ingress` | Ingress | Single host → frontend (which proxies API), same shape as the compose stack. |

Secrets are **never** in values files — they come from pre-created K8s Secrets (`api.existingSecret`, `postgres.auth.existingSecret`), ideally via whatever the DevOps team standard is (ExternalSecrets / SealedSecrets / Vault).

## 2. Prerequisites checklist — questions for the DevOps team

Ask these **before** the first deploy; the answers change values files:

1. **Registry:** which container registry, and what are the size limits? The API image is **several GB** (bge-m3 model baked in at build time). Confirm pull times / node disk are acceptable, or agree on a strategy (see gotcha #1).
2. **Egress:** the API must reach the LLM endpoint (`https://llms.elm.sa/v1` or OpenRouter) from inside the cluster. If there's an egress proxy or NetworkPolicy default-deny, we need an allowance. Without it, classification silently degrades to low-confidence fallbacks — **test the LLM path from a pod on day one** (`curl -H "Authorization: Bearer $INTEGRATION_API_TOKEN" .../test/llm` — the endpoint is token-gated now, see gotcha #7).
3. **Postgres:** can they provide managed Postgres **with the pgvector extension available**? The app runs `CREATE EXTENSION IF NOT EXISTS vector` at startup — that requires the extension to be installed on the server and the role to have permission. If not possible, we run the in-chart StatefulSet (acceptable for now, but ask about backup policy for the PVC).
4. **Ingress controller + TLS:** which ingress class, who issues certs (cert-manager?), and what hostname do we get.
5. **Secrets workflow:** how do secrets land in the namespace in their GitOps flow (we must not commit them to the DevOps repo).
6. **GitOps tooling:** ArgoCD or Flux or plain `helm upgrade` in CI? This decides the repo layout (§5).

## 3. Images to build (CI in the DevOps repo)

| Image | Dockerfile | Context |
|---|---|---|
| `ai-classifier` (API) | existing `Dockerfile` at repo root | repo root |
| `ai-classifier-frontend` | `frontend.Dockerfile` (provided in this folder — copy to repo root) | repo root |
| `ai-classifier-ocr` (optional) | existing `ocr/Dockerfile` | `ocr/` |

Why a frontend image: docker-compose bind-mounts `./frontend/dashboard` and `nginx.conf` into a stock nginx container. Bind mounts don't exist in K8s — the dashboard files must be baked into an image. The nginx **config** stays out of the image (it comes from the chart ConfigMap, because the proxy target is a cluster DNS name that Helm computes).

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
kubectl -n ai-classifier create secret generic ai-classifier-pg \
  --from-literal=POSTGRES_PASSWORD='<pw>'          # only when postgres.enabled=true

# 2) install
helm upgrade --install ai-classifier ./ai-classifier \
  -n ai-classifier \
  -f values-dev.yaml            # your env overlay (see *.example.yaml)

# 3) verify — in this order
kubectl -n ai-classifier rollout status deploy/ai-classifier-api   # model load ≤ ~5 min
kubectl -n ai-classifier port-forward svc/ai-classifier-api 8000 &
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

1. **Huge API image.** The Dockerfile pre-downloads bge-m3 into the image (`HF_HUB_OFFLINE=1` at runtime — deliberate, so pods never hit HuggingFace). Cost: multi-GB image → slow first pull per node. Mitigations if it hurts: pin the api to a node pool (nodeSelector) so the image stays cached, or move the model to a PVC/initContainer later. Do **not** just remove the bake — offline startup is a feature (no DNS dependency).
2. **`api.replicas` must stay 1.** The API process itself runs the sync poller, the cluster sweep worker, the status monitor, and the integration retry worker as daemon threads. Two replicas = two sweeps and two pollers racing. The deployment uses `strategy: Recreate` for the same reason (no overlap during rollout — accept the brief downtime). The real fix is splitting workers into their own Deployment/CronJobs — that's part of the refactor plan (`REFACTOR_AND_SMAX_INTEGRATION_PLAN.md`), after which the API becomes horizontally scalable.
3. **Writable filesystem required.** The sync worker writes a `.last_sync` stamp inside the app package directory (and note: due to a path bug it currently lands at `ai_classification/.last_sync` — documented as BUG-3 in the refactor plan). Consequences for K8s: (a) don't enable `readOnlyRootFilesystem` in any PodSecurity settings yet; (b) the stamp is lost on every pod restart → the poller re-scans a wider window. That's *safe* (server-side dedupe is content-hash gated) but wasteful. Once BUG-3 is fixed with a `SYNC_STAMP_PATH` env var, point it at an `emptyDir` (or drop the file entirely and store the stamp in Postgres — the better long-term fix for K8s).
4. **Readiness probe choice.** `/ready` genuinely probes db + embedding + **LLM reachability**. That's great for honesty but means a flaky LLM endpoint takes your pod out of the Service. Chart default: readiness on `/health`, switch to `/ready` (prod overlay shows how) only after confirming stable LLM egress. Liveness must stay `/health` — never `/ready` (an LLM outage must not restart-loop the pod).
5. **Slow requests.** A `/classify` call runs a multi-stage LLM cascade — can take tens of seconds. The chart's nginx ConfigMap sets `proxy_read_timeout 300s`; the prod ingress overlay sets the same on the ingress controller. If DevOps has a global LB timeout < 300s, raise it for this host or route external systems to the **async** API (`POST /api/v1/incidents` → 202 + poll), which was built exactly to avoid long-held connections.
6. **First startup DDL.** The app creates its own schema (`CREATE EXTENSION vector`, `CREATE TABLE IF NOT EXISTS ...`) at boot. With managed PG, the app role needs those privileges at least once; afterwards it's idempotent. Also run `scripts/migrate_classifier_v3.py up` once against a fresh prod DB (or verify the app-level DDL covers v3 columns on your version).
7. **Dangerous endpoints exposed.** `POST /reset` deletes ALL incidents and `/test/llm` spends LLM tokens; both used to sit on the same unauthenticated surface as the dashboard endpoints (only `/api/v1/*` had bearer auth). In compose this was fine (private host); behind a real ingress it is not. **This is now closed in three layers:**
   - **App-level (the real fix):** both endpoints require the same Bearer token as `/api/v1/*` (`INTEGRATION_API_TOKEN`, via `api/auth.py` → `require_token`). This works regardless of network path — ingress, pod-to-pod, port-forward, anything. 401 without a valid token.
   - **nginx (chart default):** the frontend ConfigMap hard-blocks `location = /reset` and `location = /test/llm` with `return 403` (exact-match locations that win over the proxy regex and the SPA fallback) — defense in depth, zero cost.
   - **NetworkPolicy (optional, prod hardening):** `networkPolicy.enabled: true` restricts api ingress to the frontend pods only (kubelet probes + `kubectl port-forward` bypass it, so smoke tests still work). The prod overlay enables it. If you run `frontend.enabled=false`, you MUST set `networkPolicy.ingressController.namespace` or Helm fails the render — the api would otherwise be unreachable from outside.
   - Residuals: `/test/all` (full battery) is still unauthenticated — it also spends LLM tokens, review gating it when exposing the host publicly. In-cluster access to the api Service is fine (that's how smoke tests run); just never port-forward the pod to a public machine.
8. **CORS is `*`** in the app. Harmless while the frontend proxies same-origin, but tighten it (env-configurable origin) before exposing the API host directly to browsers.
9. **SMAX polling in K8s:** leave `TICKETING_API_TOKEN` empty until real SMAX creds exist — the app logs one line and idles (by design, no crash-loop). When the `integrations/smax` connector from the refactor plan lands, it becomes its own Deployment in this chart (a `smaxConnector:` values block + Deployment template — one small chart bump).

## 7. Resource sizing (starting points, tune from metrics)

| Component | Request | Limit | Why |
|---|---|---|---|
| api | 500m / 3Gi | 2 / 5Gi | bge-m3 resident ≈ 2–2.5Gi; CPU spikes on embed + JSON parse |
| postgres (in-chart) | 250m / 512Mi | 1 / 2Gi | 91 incidents today — tiny; revisit with volume |
| frontend | 50m / 64Mi | 200m / 128Mi | static nginx |
| ocr (if enabled) | 500m / 2Gi | 2 / 4Gi | EasyOCR models are heavy |

No HPA for the api (see gotcha #2). PVC 20Gi for pgdata is generous for current volume.

## 8. Rollout / rollback

- Deploy = new image tag in env values → `helm upgrade` (or ArgoCD sync). `Recreate` strategy → ~1–5 min gap while the new pod loads the model; schedule accordingly or prioritize the worker split.
- Rollback = `helm rollback` / revert the tag. DB schema is additive-idempotent, so rolling the app back is safe; **never** run `migrate_classifier_v3.py --down` against a DB that newer code wrote to.
- Post-deploy check is always the same three: `/health`, `/ready`, `/test/all`.
