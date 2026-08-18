#!/bin/bash
set -e

# ── LLM backend detection ─────────────────────────────────────────────
# Ollama is OPTIONAL: wait/pull only when the backend IS Ollama
# (LLM_MODEL starts with ollama/ or LLM_API_BASE points at an ollama host).
# With a remote API (OpenRouter: LLM_MODEL=openrouter/... + LLM_API_KEY)
# there is nothing to wait for — start uvicorn immediately.
IS_OLLAMA=0
case "${LLM_MODEL:-}" in
    ollama/*) IS_OLLAMA=1 ;;
esac
case "${LLM_API_BASE:-}" in
    *ollama*) IS_OLLAMA=1 ;;
esac

if [ "$IS_OLLAMA" = "1" ]; then
    BASE="${LLM_API_BASE:-http://ollama:11434}"
    echo "==> Ollama backend detected (base=$BASE) — waiting for it ..."
    until curl -s "${BASE}/api/tags" > /dev/null 2>&1; do
        sleep 2
    done
    echo "==> Ollama is ready."

    # ── Pull model if missing ─────────────────────────────────────────
    MODEL="${LLM_MODEL#ollama/}"
    echo "==> Checking model: $MODEL"
    MODELS=$(curl -s "${BASE}/api/tags")
    if ! echo "$MODELS" | python3 -c "
import sys, json
d = json.load(sys.stdin)
model = '${MODEL}'
sys.exit(0 if any(m['name'] == model for m in d.get('models', [])) else 1)
" 2>/dev/null; then
        echo "==> Pulling model: $MODEL"
        curl -s -X POST "${BASE}/api/pull" \
            -d "{\"name\": \"$MODEL\"}" > /dev/null
        echo "==> Model $MODEL pulled."
    else
        echo "==> Model $MODEL already available."
    fi
else
    echo "==> Remote LLM backend (LLM_MODEL=${LLM_MODEL:-unset}) — skipping Ollama wait."
fi

# ── Start API ─────────────────────────────────────────────────────────
exec uvicorn ai_classification.services.ingest.routes:app \
    --host 0.0.0.0 \
    --port 8000
