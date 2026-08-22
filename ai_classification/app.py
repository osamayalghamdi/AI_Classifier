"""FastAPI application assembly — lifespan, CORS, exception handlers, and
router mounting. Endpoints live in ai_classification/api/*.

C-3 restructure: the app previously lived in
ai_classification/services/ingest/routes.py, with the lifespan and worker
startup in ai_classification/shared/store.py — both moved here verbatim
(no behavior, status-code, or response-shape changes).

Pipeline position: 50_api — FastAPI app."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ai_classification.api.diagnostics import router as diagnostics_router
from ai_classification.api.incidents import router as incidents_router
from ai_classification.api.integration import router as integration_router, ready_router
from ai_classification.api.reports import router as reports_router
from ai_classification.services.jobs.integration.schemas import Err
from ai_classification.services.jobs.sync import start_sync_worker
from ai_classification.services.review.cluster_proposal_routes import router as cluster_proposal_router
from ai_classification.services.review.taxonomy_gaps_routes import router as taxonomy_gaps_router
from ai_classification.shared.config import settings
import ai_classification.shared.store as store_mod

_log = logging.getLogger(__name__)


# Start/stop app: init store, begin background sync
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Resolve `store` through the store module at call time (not via a
    # module-level `from ... import`): the original lifespan lived in
    # store.py and referenced the module-global `store`, and tests swap
    # store_mod.store for an isolated instance (see
    # tests/services/ingest/test_integration_api.py) — the lifespan must
    # see the swap.
    store = store_mod.store
    # D2: resolved LLM/DB/embedding config as the FIRST log line — explicit,
    # no implicit defaults. (load_dotenv is CWD-relative: env must be set
    # deliberately by the caller — compose .env, systemd, or export.)
    _log.info(
        "Starting app — model=%s, api_base=%s, db=%s:%s/%s, embedding_model=%s",
        settings.llm_model,
        settings.llm_api_base or "(provider default)",
        settings.pg_host,
        settings.pg_port,
        settings.pg_database,
        settings.embedding_model_name,
    )
    # D3: fail loud on missing/invalid LLM config — never a silent fallback
    # to the ollama default in config.py.
    if not os.environ.get("LLM_MODEL"):
        raise RuntimeError(
            "LLM_MODEL is not set. Export LLM_MODEL explicitly (e.g. "
            "LLM_MODEL=openrouter/qwen/qwen3.6-35b-a3b) — refusing to start "
            "with an implicit default model."
        )
    if settings.llm_model.startswith("openrouter/") and not (settings.llm_api_key or "").strip():
        raise RuntimeError(
            "LLM_API_KEY is required when LLM_MODEL starts with 'openrouter/' — "
            "export LLM_API_KEY. Refusing to start with an unauthenticated LLM config."
        )
    store.setup()
    if store.ready:
        _log.info("Store ready")
    else:
        _log.warning("Store FAILED (embeddings disabled)")

    start_sync_worker(store)

    # v2 persistent clustering: Flow B pool sweep (every REPOOL_INTERVAL,
    # 900s) + Flow C nightly audit. Replaces the legacy 5-min stateless
    # rebuild loop AND the sub-offering repool worker (Flow B supersedes
    # repool's phase logic; the sub-offering engine stays dormant).
    from ai_classification.services.cluster.persistent import start_sweep_worker
    start_sweep_worker()

    # Self-healing: re-classify fallback-marked incidents once the LLM is
    # reachable again (gated on RECLASSIFY_ENABLED; the worker idles when off).
    from ai_classification.services.jobs.heal import start_heal_worker
    start_heal_worker()

    # Service status monitor — loud logging when any service (esp. the LLM
    # endpoint) is unreachable; state exposed via GET /status.
    from ai_classification.services.ingest.status_monitor import monitor
    monitor.start()

    # E1-E9 integration worker (async ingest queue) — gated so tests can
    # drive the queue synchronously (INTEGRATION_WORKER_ENABLED=0).
    if settings.integration_worker_enabled:
        from ai_classification.services.jobs.integration import start_integration_worker
        start_integration_worker()

    yield
    _log.info("Shutting down store")
    store.close()


app = FastAPI(title="AI Incident Classification", version="0.2.0", lifespan=lifespan)


# ── Structured validation errors (E8) — unknown fields / bad types map
#    to 422 INVALID_PAYLOAD with stable machine-readable codes instead of
#    FastAPI's default 422 shape.
@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_request, exc: RequestValidationError):
    fields = [
        {
            "field": ".".join(str(p) for p in err.get("loc", []) if p not in ("body", "query", "path")),
            "issue": err.get("type", "invalid"),
            "message": err.get("msg", ""),
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": Err.INVALID_PAYLOAD,
                "message": "Payload failed validation — see fields",
                "fields": fields,
            }
        },
    )


# Structured HTTP errors: dict details with an "error" key are returned
# as-is ({"error": {...}}); plain-string details keep FastAPI's default
# {"detail": ...} shape so existing endpoints are untouched.
@app.exception_handler(HTTPException)
async def _http_error_handler(_request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# CORS — allow dashboard at any origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Router mounting ────────────────────────────────────────────────────
# Cluster-proposal review API (v2 persistent clustering — human gate)
app.include_router(cluster_proposal_router)
# Taxonomy gaps review API (classifier v3 OFFERING-GAP surface)
app.include_router(taxonomy_gaps_router)
# E1-E9 integration API (+ app-level /ready and /status)
app.include_router(integration_router)
app.include_router(ready_router)
# Incident / report / diagnostics endpoints
app.include_router(incidents_router)
app.include_router(reports_router)
app.include_router(diagnostics_router)
