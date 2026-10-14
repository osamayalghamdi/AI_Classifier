#!/bin/bash
set -e

# ── LLM backend detection ─────────────────────────────────────────────
# Ollama is OPTIONAL: wait/pull only when the backend IS Ollama
# (LLM_MODEL starts with ollama/ or LLM_API_BASE points at an ollama host).
# With a remote API (OpenRouter: LLM_MODEL=openrouter/... + LLM_API_KEY)
# there is nothing to wait for — start uvicorn immediately.
# The wait is BOUNDED (OLLAMA_WAIT_S, default 60): an Ollama that never
# answers must not keep the dashboard/API dark — the app hosts anyway and
# reports the LLM as unreachable (startup log + GET /ready).
IS_OLLAMA=0
case "${LLM_MODEL:-}" in
    ollama/*) IS_OLLAMA=1 ;;
esac
case "${LLM_API_BASE:-}" in
    *ollama*) IS_OLLAMA=1 ;;
esac

if [ "$IS_OLLAMA" = "1" ]; then
    BASE="${LLM_API_BASE:-http://ollama:11434}"
    WAIT_S="${OLLAMA_WAIT_S:-60}"
    echo "==> Ollama backend detected (base=$BASE) — waiting up to ${WAIT_S}s for it ..."
    waited=0
    until curl -s --max-time 5 "${BASE}/api/tags" > /dev/null 2>&1; do
        if [ "$waited" -ge "$WAIT_S" ]; then
            echo "==> WARNING: Ollama at $BASE did not answer within ${WAIT_S}s — starting anyway." >&2
            echo "==> The API will host and report 'llm: unreachable' (startup log, GET /ready)." >&2
            echo "==> Fix: point LLM_API_BASE at a reachable model server, or set MODEL_<NAME>_ENABLED=1" >&2
            echo "==>      with a catalog model (see GET /ready -> models.issues)." >&2
            exec uvicorn ai_classification.app:app --host 0.0.0.0 --port 8000
        fi
        sleep 2
        waited=$((waited + 2))
    done
    echo "==> Ollama is ready."

    # ── Pull model if missing ─────────────────────────────────────────
    MODEL="${LLM_MODEL#ollama/}"
    echo "==> Checking model: $MODEL"
    MODELS=$(curl -s --max-time 10 "${BASE}/api/tags")
    if ! echo "$MODELS" | python3 -c "
import sys, json
d = json.load(sys.stdin)
model = '${MODEL}'
sys.exit(0 if any(m['name'] == model for m in d.get('models', [])) else 1)
" 2>/dev/null; then
        echo "==> Pulling model: $MODEL"
        curl -s --max-time 600 -X POST "${BASE}/api/pull" \
            -d "{\"name\": \"$MODEL\"}" > /dev/null || \
            echo "==> WARNING: pull of $MODEL failed — starting anyway." >&2
        echo "==> Model $MODEL pull finished."
    else
        echo "==> Model $MODEL already available."
    fi
else
    echo "==> Remote LLM backend (LLM_MODEL=${LLM_MODEL:-unset}) — skipping Ollama wait."
fi

# ── Start API ─────────────────────────────────────────────────────────
exec uvicorn ai_classification.app:app \
    --host 0.0.0.0 \
    --port 8000
