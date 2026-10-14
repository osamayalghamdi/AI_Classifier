#!/usr/bin/env bash
# local-deploy.sh — make the local cluster run what is in your working tree.
#
#   scripts/local-deploy.sh            # build, load, apply values.yaml, restart, verify
#   scripts/local-deploy.sh --no-build # just re-apply + restart + verify
#
# It closes two traps that silently serve stale code/UI:
#
#  1. A same-tag image (`:latest`) does not change the Deployment's pod
#     template, so `helm upgrade` has NOTHING to roll — the pod keeps running
#     the old image even after `kind load`. This script restarts the
#     deployments after loading, which is what makes the new image take effect.
#  2. A release keeps its own user-supplied values, and those SHADOW
#     values.yaml. `--reset-values` discards them so the file is the only
#     source; the script prints `helm get values` at the end so you can see it
#     is empty (a stray `frontend.image.tag: try` is exactly how a UI change
#     never arrives).
#
# It finishes by comparing a source file inside each pod against the working
# tree: "the deployment is running your code" is a sha256, not a vibe.
set -euo pipefail

NS="${NS:-ai-classifier}"
REL="${REL:-ai-classifier}"
CLUSTER="${CLUSTER:-ai-classifier}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD=1
[ "${1:-}" = "--no-build" ] && BUILD=0

api_img="ai_classifier-api:latest"
fe_img="ai-classifier-frontend:latest"

if [ "$BUILD" = "1" ]; then
    echo "==> building images (context = the directory with the sources)"
    docker build -f "$REPO/backend/Dockerfile" -t "$api_img" "$REPO/backend/" | tail -1
    docker build -f "$REPO/frontend/Dockerfile" -t "$fe_img" "$REPO/frontend/" | tail -1
fi

echo "==> loading into kind ($CLUSTER)"
kind load docker-image "$api_img" "$fe_img" --name "$CLUSTER" | tail -2

echo "==> applying values.yaml (no overrides, no -f)"
helm upgrade --install "$REL" "$REPO/deploy/helm/ai-classifier" -n "$NS" --reset-values | grep -E "STATUS|REVISION"

echo "==> restarting so the loaded images are actually used"
# The rendered name is <release>-<chart>-<component>, so ask the chart's labels
# instead of guessing (a guessed name fails with "deployment not found" while the
# deployment is perfectly healthy).
backend=$(kubectl -n "$NS" get deploy -l app.kubernetes.io/component=backend -o name | head -1)
frontend=$(kubectl -n "$NS" get deploy -l app.kubernetes.io/component=frontend -o name | head -1)
[ -n "$backend" ] || backend="deploy/${REL}-backend"
[ -n "$frontend" ] || frontend="deploy/${REL}-frontend"
kubectl -n "$NS" rollout restart "$backend" >/dev/null
kubectl -n "$NS" rollout status "$backend" --timeout=300s | tail -1
if [ -n "$frontend" ]; then
    kubectl -n "$NS" rollout restart "$frontend" >/dev/null
    kubectl -n "$NS" rollout status "$frontend" --timeout=180s | tail -1
fi

echo "==> is the pod running THIS working tree?"
pod_sha() {  # $1 = deployment, $2 = path inside the image, $3 = path in the repo
    kubectl -n "$NS" exec "$1" -- sha256sum "$2" 2>/dev/null | cut -c1-16
}
local_sha() { sha256sum "$1" 2>/dev/null | cut -c1-16; }

p=$(pod_sha "$backend" /app/ai_classification/api/integration.py); l=$(local_sha "$REPO/backend/ai_classification/api/integration.py")
[ -n "$p" ] && [ "$p" = "$l" ] && echo "    backend  code matches (${p})" || echo "    backend  MISMATCH: pod=${p:-?} tree=${l:-?} — the image is stale"
if [ -n "$frontend" ]; then
    p=$(pod_sha "$frontend" /usr/share/nginx/html/app.js); l=$(local_sha "$REPO/frontend/dashboard/app.js")
    [ -n "$p" ] && [ "$p" = "$l" ] && echo "    frontend code matches (${p})" || echo "    frontend MISMATCH: pod=${p:-?} tree=${l:-?} — the image is stale"
fi

echo "==> release overrides (empty = values.yaml is the only source)"
helm get values "$REL" -n "$NS"

echo "==> health"
kubectl -n "$NS" exec "$backend" -- python3 -c "
import json, urllib.request
b = json.loads(urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=120).read().decode())
print('    /ready:', b['status'], b['checks'], '| models.configured:', b['models']['configured'])
" 2>/dev/null || echo "    /ready unavailable — run scripts/doctor.sh"
