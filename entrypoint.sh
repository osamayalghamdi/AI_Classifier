#!/bin/bash
set -e

# ── Wait for Ollama ───────────────────────────────────────────────────
echo "==> Waiting for Ollama at ${LLM_API_BASE:-http://ollama:11434} ..."
until curl -s "${LLM_API_BASE}/api/tags" > /dev/null 2>&1; do
    sleep 2
done
echo "==> Ollama is ready."

# ── Pull model ────────────────────────────────────────────────────────
MODEL="${LLM_MODEL#ollama/}"
echo "==> Checking model: $MODEL"
MODELS=$(curl -s "${LLM_API_BASE}/api/tags")
if ! echo "$MODELS" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if any(m['name']=='$MODEL' for m in d['models']) else 1)" 2>/dev/null; then
    echo "==> Pulling model: $MODEL"
    curl -s -X POST "${LLM_API_BASE}/api/pull" \
        -d "{\"name\": \"$MODEL\"}" > /dev/null
    echo "==> Model $MODEL pulled."
else
    echo "==> Model $MODEL already available."
fi

# ── Start API ─────────────────────────────────────────────────────────
exec uvicorn ai_classification.main:app \
    --host 0.0.0.0 \
    --port 8000
