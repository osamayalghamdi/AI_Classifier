#!/usr/bin/env bash
# doctor.sh — "is this thing working, and if not, what do I set?"
#
# One command, one verdict. It checks the things that actually break a run and
# prints, for each: OK / WARN / FAIL plus the exact thing to change. Nothing here
# mutates anything — it is read-only, so it is always safe to run.
#
#   scripts/doctor.sh                       # the deployment in the cluster (default)
#   scripts/doctor.sh --api http://host:8000 # a host-side run instead
#   NS=ai-classifier REL=ai-classifier scripts/doctor.sh
#
# Exit code: 0 when the normal flow is expected, 1 when something blocks it.
set -uo pipefail

NS="${NS:-ai-classifier}"
REL="${REL:-ai-classifier}"
# scripts/ lives in backend/, so the chart is two levels up — the wrong path
# here does not fail loudly, it just makes the drift check read nothing.
VALUES="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/deploy/helm/ai-classifier/values.yaml"
API=""
while [ $# -gt 0 ]; do
    case "$1" in
        --api) API="${2:-}"; shift 2 ;;
        --ns) NS="${2:-}"; shift 2 ;;
        --release) REL="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "doctor: unknown argument $1" >&2; exit 2 ;;
    esac
done

FAILED=0
ok()   { printf '  \033[32mOK  \033[0m %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILED=$((FAILED + 1)); }
info() { printf '       %s\n' "$*"; }
head2() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ── the report the app itself serves ─────────────────────────────────
# Parsed with python so a missing/oddly-shaped response is reported, not
# crashed on. Prints one line per check and one per model issue.
report_ready() {
    python3 - "$1" <<'PY'
import json, sys, urllib.error, urllib.request

# Accepts a URL (host run) or the JSON itself (already fetched inside the pod,
# where the API is only reachable on its loopback). A non-2xx IS a report: an
# unconfigured deployment answers /ready with 503 and the body that says why,
# so an HTTPError must be read, not mistaken for "no answer".
arg = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    if arg.lstrip().startswith("{"):
        raw = arg.encode()
    else:
        try:
            with urllib.request.urlopen(arg, timeout=120) as r:
                raw = r.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b""
    # Parse OUTSIDE both branches: inside the else, the already-fetched-JSON
    # path (used by the cluster check) died with NameError on `body`.
    body = json.loads(raw.decode() or "{}")
except Exception as exc:                     # noqa: BLE001 — this IS the report
    print(f"UNREACHABLE\t{type(exc).__name__}: {exc}")
    raise SystemExit(0)

print(f"STATUS\t{body.get('status')}")
for name, state in (body.get("checks") or {}).items():
    print(f"CHECK\t{name}\t{state}")
models = body.get("models") or {}
print(f"CONFIGURED\t{models.get('configured')}")
for issue in models.get("issues") or []:
    print("ISSUE\t{cap}\t{block}\t{summary}\t{env}\t{fix}".format(
        cap=issue.get("capability"),
        block="blocking" if issue.get("blocking") else "advisory",
        summary=issue.get("summary"),
        env=" / ".join(issue.get("env") or []),
        fix=issue.get("fix"),
    ))
PY
}

describe_report() {
    local raw="$1"
    if [ -z "${raw//[[:space:]]/}" ] || printf '%s' "$raw" | grep -q '^UNREACHABLE'; then
        fail "the app does not answer /ready: $(printf '%s' "$raw" | cut -f2- | tr -d '\t')"
        if [ -n "$API" ]; then
            info "is it up?  curl -s $API/health   (start: cd backend && source scripts/env-from-chart.sh && .venv/bin/python -m uvicorn ai_classification.app:app --port 8000)"
        else
            info "pod running?  kubectl -n $NS get pods"
            info "logs:         kubectl -n $NS logs $BACKEND --tail=50"
            info "the pod must exist before this check means anything"
        fi
        return
    fi
    local status configured
    status=$(printf '%s' "$raw" | awk -F'\t' '$1=="STATUS"{print $2}')
    configured=$(printf '%s' "$raw" | awk -F'\t' '$1=="CONFIGURED"{print $2}')
    printf '%s' "$raw" | awk -F'\t' '$1=="CHECK"{printf "       %-10s %s\n", $2":", $3}'
    if [ "$configured" = "True" ]; then
        ok "models: configured ($status)"
    else
        warn "models: NOT fully configured ($status) — the app runs, but see below"
    fi
    printf '%s' "$raw" | awk -F'\t' '$1=="ISSUE"{
        printf "\n       %s [%s] %s\n         set: %s\n         %s\n", ($3=="blocking" ? "BLOCKING" : "advisory"), $2, $4, $5, $6
    }'
    if printf '%s' "$raw" | awk -F'\t' '$1=="ISSUE" && $3=="blocking"' | grep -q ISSUE; then
        fail "a blocking model issue is listed above"
        if [ -n "$API" ]; then
            info "set it in $VALUES (one file), then: source scripts/env-from-chart.sh"
        else
            info "set it in $VALUES (one file), then: helm upgrade $REL deploy/helm/ai-classifier -n $NS"
        fi
    fi
}

pod_py() { kubectl -n "$NS" exec "$1" -- python3 -c "$2" 2>/dev/null; }

# ── host-side run ────────────────────────────────────────────────────
if [ -n "$API" ]; then
    head2 "Host run at $API"
    health=$(curl -s --max-time 20 "$API/health" || true)
    if printf '%s' "$health" | grep -q '"status":"ok"'; then
        ok "/health $health"
    else
        fail "/health did not answer ok: ${health:-no response}"
        info "start it:  cd backend && source scripts/env-from-chart.sh && .venv/bin/python -m uvicorn ai_classification.app:app --port 8000"
    fi
    head2 "Configuration report (what the app needs)"
    describe_report "$(report_ready "$API/ready")"
    head2 "Verdict"
    [ "$FAILED" -eq 0 ] && echo "  Normal flow expected." || echo "  $FAILED item(s) to fix — see above."
    exit $([ "$FAILED" -eq 0 ] && echo 0 || echo 1)
fi

# ── cluster deployment ───────────────────────────────────────────────
head2 "Cluster: release $REL in namespace $NS"
if ! command -v kubectl >/dev/null 2>&1; then
    fail "kubectl not found"
    info "for a host run instead: $0 --api http://localhost:8000"
    exit 1
fi
if kubectl -n "$NS" get deploy >/dev/null 2>&1; then
    ok "namespace $NS reachable"
else
    fail "namespace $NS not reachable (cluster down? wrong context?)"
    exit 1
fi
if helm -n "$NS" status "$REL" >/dev/null 2>&1; then
    ok "helm release $REL deployed (revision $(helm -n "$NS" status "$REL" -o json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin).get("version"))' 2>/dev/null))"
else
    warn "no helm release $REL — deploy it: helm upgrade --install $REL deploy/helm/ai-classifier -n $NS"
fi

# The rendered name is <release>-<chart>-<component>, so resolve it from the
# chart's own labels instead of guessing (the guess made this script report a
# healthy deployment as missing).
BACKEND=$(kubectl -n "$NS" get deploy -l app.kubernetes.io/component=backend -o name 2>/dev/null | head -1)
FRONTEND=$(kubectl -n "$NS" get deploy -l app.kubernetes.io/component=frontend -o name 2>/dev/null | head -1)
[ -n "$BACKEND" ] || BACKEND="deploy/${REL}-backend"
[ -n "$FRONTEND" ] || FRONTEND="deploy/${REL}-frontend"

head2 "Pods"
for d in "$BACKEND" "$FRONTEND"; do
    if kubectl -n "$NS" get "$d" >/dev/null 2>&1; then
        ready=$(kubectl -n "$NS" get "$d" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
        restarts=$(kubectl -n "$NS" get pods -l app.kubernetes.io/instance="$REL" -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{"\n"}{end}' 2>/dev/null | sort -rn | head -1)
        if [ "${ready:-0}" -ge 1 ]; then
            ok "$d ready (restarts: ${restarts:-0})"
        else
            fail "$d not ready — kubectl -n $NS describe $d ; kubectl -n $NS logs $d --tail=50"
        fi
    else
        warn "$d not found"
    fi
done

head2 "Environment the running pod actually has"
POD_ENV=$(pod_py "$BACKEND" "import os;print('\n'.join(f'{k}={os.environ.get(k,\"\")}' for k in ('LLM_MODEL','LLM_API_BASE','EMBEDDING_API_URL','OCR_ENABLED','PG_HOST')))")
if [ -z "$POD_ENV" ]; then
    fail "cannot read the pod's environment (pod not ready?)"
else
    while IFS= read -r line; do [ -n "$line" ] && info "$line"; done <<<"$POD_ENV"
    # Drift: the pod runs what the ConfigMap had at pod start, not what is in
    # values.yaml now. Editing values with no upgrade/reload is the quietest
    # way to "change the config and see nothing happen".
    for key in LLM_MODEL LLM_API_BASE EMBEDDING_API_URL; do
        want=$(grep -m1 "^\s*${key}:" "$VALUES" 2>/dev/null | sed -E 's/^\s*[A-Z_]+:\s*//; s/\s+#.*$//; s/^"//; s/"$//')
        have=$(printf '%s' "$POD_ENV" | awk -F= -v k="$key" '$1==k{sub(/^[^=]*=/,""); print}')
        if [ -n "$want" ] && [ "$want" != "$have" ]; then
            fail "$key differs — values.yaml says '$want', the pod has '$have'"
            info "apply it:  helm upgrade $REL deploy/helm/ai-classifier -n $NS"
        fi
    done
fi

head2 "The app's own configuration report (/ready, read inside the pod)"
describe_report "$(report_ready "$(pod_py "$BACKEND" "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/ready',timeout=120).read().decode())")")"

head2 "Model server reachable FROM THE POD"
LLM_BASE=$(printf '%s' "$POD_ENV" | awk -F= '$1=="LLM_API_BASE"{print $2}')
if [ -z "$LLM_BASE" ]; then
    warn "LLM_API_BASE is empty in the pod — nothing to probe"
else
    TAGS=$(pod_py "$BACKEND" "import json,urllib.request;print(urllib.request.urlopen('${LLM_BASE%/}/api/tags',timeout=20).read().decode())")
    if [ -z "$TAGS" ]; then
        fail "no answer from ${LLM_BASE}/api/tags inside the pod"
        info "a pod cannot use localhost: point LLM_API_BASE at the host's LAN address"
        info "and make sure Ollama runs there:  ollama serve  (or systemctl status ollama)"
    else
        MODELS=$(printf '%s' "$TAGS" | python3 -c 'import json,sys;print(", ".join(m["name"] for m in json.load(sys.stdin).get("models",[])))' 2>/dev/null)
        ok "Ollama answers at $LLM_BASE"
        info "models there: $MODELS"
        want=$(printf '%s' "$POD_ENV" | awk -F= '$1=="LLM_MODEL"{print $2}')
        base="${want#ollama/}"
        if [ -n "$base" ] && ! printf '%s' "$MODELS" | tr ',' '\n' | grep -q "^ *${base%:latest}"; then
            fail "the configured model '$base' is not in that list"
            info "pull it:   ollama pull $base"
        fi
    fi
fi

head2 "Secrets"
for s in ai-classifier-secrets ai-classifier-db; do
    if kubectl -n "$NS" get secret "$s" >/dev/null 2>&1; then
        keys=$(kubectl -n "$NS" get secret "$s" -o go-template='{{range $k,$v := .data}}{{$k}} {{end}}' 2>/dev/null)
        ok "$s present ($keys)"
    else
        fail "secret $s missing — tokens 401 and the DB refuses to connect without it"
        info "kubectl -n $NS create secret generic $s --from-literal=... (see values.yaml)"
    fi
done

head2 "Verdict"
if [ "$FAILED" -eq 0 ]; then
    echo "  Normal flow expected: the dashboard, ingest and classification are all configured."
else
    echo "  $FAILED item(s) to fix — each one above names the variable or command."
fi
exit $([ "$FAILED" -eq 0 ] && echo 0 || echo 1)
