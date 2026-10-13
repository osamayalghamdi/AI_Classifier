# ── Runtime image: MODEL-FREE by design ─────────────────────────────────
# The app owns no model. Everything it needs is an HTTP endpoint configured
# by env (the .env file is the whole configuration surface):
#     LLM_API_BASE / LLM_MODEL / LLM_API_KEY        → text classification
#     EMBEDDING_API_URL / EMBEDDING_MODEL_ID / …    → embeddings (similarity)
#     OCR_SERVICE_URL                               → attachment text
# There is deliberately no torch, no transformers, no sentence-transformers
# and no model cache in here — the build asserts it (see the guard below).
#
# Build — the context MUST be `backend/` (it contains pyproject.toml,
# ai_classification/, integrations/ and entrypoint.sh). Building from the repo
# root fails with `"/ai_classification": not found`:
#   docker build -f backend/Dockerfile -t REGISTRY/ai-classifier:<tag> backend/
#   # or via compose (already set to `build: ./backend`): docker compose build api
FROM python:3.12-slim

WORKDIR /app

# Unprivileged runtime user. UID 1000 matches the usual host user, so the
# compose bind mounts (./backend/ai_classification, ./.env) stay readable
# and writable.
RUN useradd --uid 1000 --user-group --no-create-home --home-dir /app app

RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# pytest is needed by the admin console's in-container test runner.
# --chown: the runtime user (uid 1000) must own the code it reads — without
# it the cluster's runAsUser: 1000 gets PermissionError on import.
COPY --chown=app:app pyproject.toml ./
COPY --chown=app:app ai_classification/ ./ai_classification/
# The SMAX connector is a separate process that lives in its own folder and
# imports nothing from the app — it ships here so `python -m integrations.smax.main`
# can run from the same image (see integrations/smax/docker-compose.smax.yml).
COPY --chown=app:app integrations/ ./integrations/
# Some files in a dev checkout are 0600; make the code readable for the
# runtime user regardless of the host umask (the cluster runs uid 1000).
RUN chmod -R a+rX /app/ai_classification /app/integrations
RUN pip install --no-cache-dir -e . pytest 2>&1 | tail -5

# Guard: the image must stay model-free. If any dependency ever drags the
# local ML stack back in (torch + nvidia-* + weights ≈ 10 GB), fail the build
# instead of shipping it.
RUN python -c "\
import importlib.util as u; \
bad = [m for m in ('torch', 'sentence_transformers', 'transformers') if u.find_spec(m)]; \
assert not bad, f'local ML stack leaked into the image: {bad} — set EMBEDDING_API_URL instead'; \
print('model-free image confirmed')"

COPY --chown=app:app entrypoint.sh ./
# /app itself must be writable by the app user (.last_sync stamp, admin
# console .env writes).
RUN chmod +x entrypoint.sh && chown app:app /app

USER app

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
