# ── Builder stage: pre-download embedding model ─────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# HF cache inside the builder so the pre-downloaded model can be copied to
# the runtime image (the default ~/.cache/huggingface would be lost).
ENV HF_HOME=/build/hf_cache

RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
        git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
# CPU-only torch first: the API runs the embedding model on CPU, and the
# default CUDA wheels alone add ~10 GB to the image.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch
# pytest is needed by the admin console's in-container test runner.
RUN pip install --no-cache-dir -e . pytest 2>&1 | tail -5

# Pre-download the embedding model so the runtime image doesn't need
# network access or pip at startup. The project uses BAAI/bge-m3
# (EMBEDDING_MODEL default, config.py:29 — 1024-dim), NOT all-MiniLM-L6-v2.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3')"


# ── Runtime image ──────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

ENV HF_HOME=/app/hf_cache

# Unprivileged runtime user. UID 1000 matches the usual host user, so the
# compose bind mounts (./ai_classification, ./.env) stay readable/writable.
RUN useradd --uid 1000 --user-group --no-create-home --home-dir /app app

# ── System deps ────────────────────────────────────────────────────────
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps (copied from builder) ──────────────────────────────────
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# ── Pre-downloaded embedding model cache (from builder) ─────────────────
COPY --from=builder --chown=app:app /build/hf_cache /app/hf_cache

# ── App code ───────────────────────────────────────────────────────────
COPY --chown=app:app pyproject.toml ./
COPY --chown=app:app ai_classification/ ./ai_classification/
COPY --chown=app:app entrypoint.sh ./

# /app itself must be writable by the app user (.last_sync stamp, admin
# console .env writes).
RUN chmod +x entrypoint.sh && chown app:app /app

USER app

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
