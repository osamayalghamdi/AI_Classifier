# ── Base ──────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# ── System deps ───────────────────────────────────────────────────────
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── App code + deps ───────────────────────────────────────────────────
COPY pyproject.toml ./
COPY ai_classification/ ./ai_classification/
COPY entrypoint.sh ./

RUN pip install --no-cache-dir -e . 2>&1 | tail -5 \
    && chmod +x entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
