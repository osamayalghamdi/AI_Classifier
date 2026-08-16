"""Integration API endpoints (E1-E9) — async ingest, fetch, batch,
dry-run, readiness. Auth (Bearer token) on every endpoint EXCEPT
/health (liveness) and /ready (readiness) — those are exempt by design.

Structured errors: {"error": {"code": <stable>, "message": ..., "reference": ...}}.
Validation failures (unknown fields, bad types) map to 422 INVALID_PAYLOAD
via the app-level handler registered in routes.py.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import ValidationError

from ..config import settings
from ..integration import enqueue, get_job, list_jobs, worker_tick
from ..integration.schemas import Err, IntegrationBatch, IntegrationIncident, error_body
from ..seams.port import Incident
from ..seams.pipeline import persist_result, process_incident

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["integration"])


# ── Auth (E6) — every non-health endpoint ─────────────────────────────

def require_token(authorization: str | None = Header(None)) -> None:
    expected = settings.integration_token
    if not expected:
        raise HTTPException(
            status_code=401,
            detail=error_body(Err.UNAUTHORIZED, "Integration token is not configured on the server"),
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail=error_body(Err.UNAUTHORIZED, "Missing Authorization header (Bearer <token>)"),
        )
    supplied = authorization[len("Bearer "):]
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail=error_body(Err.UNAUTHORIZED, "Invalid token"),
        )


# ── E1: async ingest ──────────────────────────────────────────────────

@router.post("/incidents", status_code=202, dependencies=[Depends(require_token)])
def ingest_incident(inc: IntegrationIncident):
    """Accept ONE incident; returns immediately with the reference.
    Processing happens asynchronously — the caller never waits on the LLM."""
    job = enqueue(inc.model_dump())
    assert job is not None  # just inserted/fetched
    _log.info("Integration ingest accepted — reference=%s status=%s",
              inc.source_reference, job["status"])
    return {
        "reference": inc.source_reference,
        "status": job["status"],
        "location": f"/api/v1/incidents/{inc.source_reference}",
    }


# ── E2: fetch result by reference ─────────────────────────────────────

@router.get("/incidents/{reference}", dependencies=[Depends(require_token)])
def fetch_incident_job(reference: str):
    """Structured result, or pending / retryable / flagged status."""
    job = get_job(reference)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=error_body(Err.NOT_FOUND, f"No job with reference '{reference}'", reference),
        )
    _log.info("Integration fetch — reference=%s status=%s", reference, job["status"])
    return job


# ── E5: dry-run (sync, writes nothing, persists nothing) ──────────────

@router.post("/incidents/dry-run", dependencies=[Depends(require_token)])
def dry_run_incident(inc: IntegrationIncident):
    """Same contract as ingest but SYNCHRONOUS and side-effect-free:
    returns exactly what would be written; nothing persisted, no job row."""
    incident = Incident(
        source_reference=inc.source_reference,
        title=inc.title,
        description=inc.description,
        attachments=inc.attachments,
        status=inc.status,
        created_at=inc.created_at,
        updated_at=inc.updated_at,
    )
    result = process_incident(incident)
    outcome = persist_result(result, dry_run=True)  # read-only by construction
    _log.info("Integration dry-run — reference=%s would_write=%s",
              inc.source_reference, outcome)
    return {
        "reference": inc.source_reference,
        "is_new": result.is_new,
        "classification": result.classification.model_dump() if result.classification else None,
        "similar_tickets": result.similar_tickets,
        "suggestions": result.suggestions,
        "confidence": result.confidence,
        "model_version": result.model_version,
        "prompt_version": result.prompt_version,
        "would_write": outcome,
        "write_back": {"mode": settings.integration_write_back, "applied": False},
    }


# ── E3: batch / backfill ──────────────────────────────────────────────

@router.post("/backfill", status_code=202, dependencies=[Depends(require_token)])
def backfill(batch: IntegrationBatch):
    """One-time historical run: enqueue up to 200 incidents; each is
    processed asynchronously and its result fetched by reference."""
    references = []
    for inc in batch.incidents:
        job = enqueue(inc.model_dump())
        assert job is not None
        references.append(job["source_reference"])
    _log.info("Integration backfill accepted — %d references", len(references))
    return {
        "total": len(references),
        "references": references,
        "location_prefix": "/api/v1/incidents/",
    }


# ── Ops ───────────────────────────────────────────────────────────────

@router.get("/jobs", dependencies=[Depends(require_token)])
def jobs(limit: int = Query(default=20, ge=1, le=100)):
    """Recent jobs, newest first (ops view for the integration queue)."""
    return {"jobs": list_jobs(limit)}


@router.post("/worker/tick", dependencies=[Depends(require_token)])
def tick_worker(limit: int = Query(default=10, ge=1, le=100)):
    """Advance the job queue manually (e.g. after a deploy before the
    background poll picks things up). Returns the number processed."""
    n = worker_tick(limit)
    return {"processed": n}


# ── E4: readiness — exempt from auth, checks reported individually ────
# App-level (no /api/v1 prefix) so it sits next to /health.

ready_router = APIRouter(tags=["integration"])


@ready_router.get("/ready")
def readiness():
    checks: dict[str, str] = {}

    # DB
    try:
        from ..integration import _connect
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        checks["db"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["db"] = f"error: {exc}"

    # Embedding model
    try:
        from ..core.store import store
        if store._model is None:
            checks["embedding"] = "error: model not loaded"
        else:
            checks["embedding"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["embedding"] = f"error: {exc}"

    # LLM reachability — a real 1-token probe with a short timeout.
    try:
        from litellm import completion
        kwargs: dict = dict(
            model=settings.llm_model,
            max_tokens=1,
            temperature=0.0,
            timeout=5.0,
            messages=[{"role": "user", "content": "ping"}],
        )
        if settings.llm_api_base:
            kwargs["api_base"] = settings.llm_api_base
        if settings.llm_api_key:
            kwargs["api_key"] = settings.llm_api_key
        completion(**kwargs)
        checks["llm"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["llm"] = f"unreachable: {type(exc).__name__}"

    degraded = checks.get("db") != "ok" or checks.get("embedding") != "ok"
    status = "ok" if (not degraded and checks.get("llm") == "ok") else (
        "degraded" if not degraded else "unhealthy"
    )
    code = 200 if not degraded else 503
    return _json_response(code, {"status": status, "checks": checks})


def _json_response(code: int, body: dict):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=code, content=body)
