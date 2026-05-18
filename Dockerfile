# ── Builder stage: pre-download embedding model ─────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
        git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e . 2>&1 | tail -5

# Pre-download the embedding model so the runtime image doesn't need
# network access or pip at startup.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"


# ── Runtime image ──────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# ── System deps ────────────────────────────────────────────────────────
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps (copied from builder) ──────────────────────────────────
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# ── App code ───────────────────────────────────────────────────────────
COPY pyproject.toml ./
COPY ai_classification/ ./ai_classification/
COPY entrypoint.sh ./

RUN chmod +x entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
