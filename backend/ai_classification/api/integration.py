"""Integration API endpoints (E1-E9) — async ingest, fetch, batch,
dry-run, readiness. Auth (Bearer token) on every endpoint EXCEPT
/health (liveness) and /ready (readiness) — those are exempt by design.

Structured errors: {"error": {"code": <stable>, "message": ..., "reference": ...}}.
Validation failures (unknown fields, bad types) map to 422 INVALID_PAYLOAD
via the app-level handler registered in app.py.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from ai_classification.api.auth import require_token
from ai_classification.shared.config import settings
from ai_classification.services.jobs.integration import enqueue, get_job, list_jobs, worker_tick
from ai_classification.services.jobs.integration.schemas import (
    Err,
    IntegrationBatch,
    IntegrationIncident,
    StatusUpdate,
    error_body,
)
from ai_classification.seams.port import Incident
from ai_classification.seams.pipeline import persist_result, process_incident
from ai_classification.shared.model_config import classifier_blocking, summary as model_config_summary

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["integration"])


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


# ── E10: status update by reference ─────────────────────────────────
# The SMAX "same ID → change the status" rule, as a normalized contract
# endpoint: an incident already ingested under a source_reference gets a
# STATUS-ONLY update — no re-classification, no new row. The raw status is
# dynamic (any value accepted) and stored verbatim in source_status; the
# local active/resolved view is derived. 404 when the reference is unknown
# (ingest it first via E1).

@router.post("/incidents/{reference}/status", dependencies=[Depends(require_token)])
def update_incident_status(reference: str, update: StatusUpdate):
    from ai_classification.shared.store import store

    updated = store.update_status_by_reference(reference, update.status)
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail=error_body(Err.NOT_FOUND, f"No incident with reference '{reference}'", reference),
        )
    _log.info("Integration status update — reference=%s status=%r local=%s",
              reference, updated.get("source_status"), updated.get("status"))
    return {
        "action": "updated",
        "reference": reference,
        "incident_id": updated["id"],
        "status": updated.get("status"),
        "source_status": updated.get("source_status"),
        "updated_at": update.updated_at.isoformat() if update.updated_at else None,
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
        affected_system=inc.affected_system,
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
#
# An unconfigured MODEL never makes the host unready (that is the point of the
# model-configuration report): /ready is 503 only when the DB is down or the
# embedding capability is genuinely unavailable, and the `models` block carries
# the full list of what is missing, what to set, and what degrades meanwhile.

ready_router = APIRouter(tags=["integration"])


@ready_router.get("/ready")
def readiness(llm: bool = Query(True, description="false = skip the live LLM probe (use for k8s readiness probes)")):
    checks: dict[str, str] = {}

    # DB
    try:
        from ai_classification.services.jobs.integration import _connect
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        from ai_classification.shared.store import store
        # A reachable DB is not enough: if store.setup() failed at boot the
        # store silently drops every write, so the pod must not be Ready.
        checks["db"] = "ok" if store.ready else "error: store not initialised"
    except Exception as exc:  # noqa: BLE001 — details go to the log, not to anonymous callers
        _log.warning("/ready db check failed: %s", exc)
        checks["db"] = f"error: {type(exc).__name__}"

    # Embedding capability — asks the store's own public probe, which knows the
    # difference between a local model (loaded = ok) and an external endpoint
    # (configured = ok). Reading store._model directly reported "model not
    # loaded" for every external-embedding deployment, i.e. a permanent 503
    # that named no fix.
    try:
        from ai_classification.shared.store import store
        if not settings.embedding_api_url and not store.embedding_provider_is_local():
            checks["embedding"] = "error: embeddings are external but EMBEDDING_API_URL is unset"
        elif store.embedding_ready():
            checks["embedding"] = ("ok" if store.embedding_provider_is_local()
                                   else f"ok: external {settings.embedding_api_url}")
        else:
            checks["embedding"] = "error: the local embedding model is not loaded"
    except Exception as exc:  # noqa: BLE001
        _log.warning("/ready embedding check failed: %s", exc)
        checks["embedding"] = f"error: {type(exc).__name__}"

    # LLM reachability — a real 1-token probe with a short timeout.
    # Skippable (?llm=false): a k8s probe hitting this every 10s would spend
    # LLM calls and flap the pod whenever the provider is slow.
    # When the classifier is not CONFIGURED the probe is pointless (it would
    # time out against a placeholder endpoint): report why, name the fix, and
    # do not spend a call.
    blocked = classifier_blocking(settings)
    if not llm:
        checks["llm"] = "skipped"
    elif blocked is not None:
        checks["llm"] = f"not configured: {blocked.summary} — set {' / '.join(blocked.env)}"
    else:
        try:
            from litellm import completion
            # Generous by default for a LOCAL model: on CPU even one token can
            # take longer than a hosted API's whole round trip, and reporting a
            # working model as down is worse than waiting (LLM_PROBE_TIMEOUT_S).
            probe_timeout = float(getattr(settings, "llm_probe_timeout_s", 5.0) or 5.0)
            kwargs: dict = dict(
                model=settings.llm_model,
                max_tokens=1,
                temperature=0.0,
                timeout=probe_timeout,
                messages=[{"role": "user", "content": "ping"}],
            )
            if settings.llm_api_base:
                kwargs["api_base"] = settings.llm_api_base
            if settings.llm_api_key:
                kwargs["api_key"] = settings.llm_api_key
            completion(**kwargs)
            checks["llm"] = "ok"
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            if "timeout" in name.lower() or "timeout" in str(exc).lower():
                # Say the useful thing: a slow model is not a missing one.
                checks["llm"] = (
                    f"slow: no answer within {float(getattr(settings, 'llm_probe_timeout_s', 5.0) or 5.0):.0f}s "
                    f"({name}) — a local model on CPU needs longer; raise LLM_PROBE_TIMEOUT_S "
                    "(classification itself uses LLM_TIMEOUT_S)"
                )
            else:
                checks["llm"] = f"unreachable: {name}"

    degraded = checks.get("db") != "ok" or not checks.get("embedding", "").startswith("ok")
    status = "ok" if (not degraded and checks.get("llm") in ("ok", "skipped")) else (
        "degraded" if not degraded else "unhealthy"
    )
    code = 200 if not degraded else 503
    body = {"status": status, "checks": checks, "models": model_config_summary(settings)}
    return _json_response(code, body)


def _json_response(code: int, body: dict):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=code, content=body)


# ── /status — live per-service detail (no auth, next to /health /ready) ──
# Returns the status monitor's cached state: each service (db, embedding,
# llm) with its resolved config and last check time, plus a rollup. The
# monitor logs LOUDLY on state changes, so this endpoint is the "look now"
# view while the logs are the "noticed without looking" view.

@ready_router.get("/status")
def service_status():
    from ai_classification.services.ingest.status_monitor import monitor

    snap = monitor.snapshot()
    if not snap:
        return _json_response(503, {
            "status": "warming",
            "detail": "status monitor has not completed its first probe cycle yet",
            "services": {},
        })
    services = {
        name: {
            "status": st.status,
            "detail": st.detail,
            "resolved": st.resolved,
            "last_checked": st.last_checked,
        }
        for name, st in sorted(snap.items())
    }
    any_down = any(st.status != "ok" for st in snap.values())
    return _json_response(
        200 if not any_down else 503,
        {"status": "degraded" if any_down else "ok", "services": services},
    )
