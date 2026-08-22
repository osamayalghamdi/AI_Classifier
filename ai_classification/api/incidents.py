"""Incident endpoints — classify (single/batch), read, import, reset.

Moved from ai_classification/services/ingest/routes.py (C-3 restructure) —
endpoint behavior, status codes, and response shapes are unchanged.

Pipeline position: 50_api — FastAPI endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from ai_classification.api.auth import require_token
from ai_classification.api.schemas import (
    ClassifyRequest, ClassifyResponse, ClassifyBatchRequest, ClassifyBatchResponse,
    ResolveResponse, BulkImportRequest, IncidentResponse, IncidentListResponse,
)
from ai_classification.domain.models import ClassificationResult
from ai_classification.shared.store import store
from ai_classification.services.classify.classifier import classify_and_store, classify_batch
from ai_classification.services.ingest.import_service import import_incidents_from_file, import_incidents_from_body

_log = logging.getLogger(__name__)

router = APIRouter(tags=["incidents"])


# ── Thin store-facing wrappers (moved from shared/store.py) ────────────
# The api modules call store.<method> through these local wrappers so the
# previous module-level names keep working with identical logging.

def resolve_incident(incident_id: str) -> bool:
    ok = store.resolve_incident(incident_id)
    if ok:
        _log.info("Incident %s resolved", incident_id)
    else:
        _log.warning("Resolve failed — incident %s not found", incident_id)
    return ok


def get_incident(incident_id: str) -> dict | None:
    inc = store.get_incident(incident_id)
    if inc is None:
        _log.debug("Incident %s not found", incident_id)
    return inc


def delete_all_incidents() -> int:
    count = store.delete_all()
    _log.warning("All incidents deleted — count=%d", count)
    return count


def list_incidents(status: str | None = None) -> list[dict]:
    items = store.list_incidents(status)
    _log.debug("Listed %d incidents (status=%s)", len(items), status or "all")
    return items


# Classify a new incident
@router.post("/classify", response_model=ClassifyResponse)
def classify_incident(req: ClassifyRequest):
    _log.info("POST /classify — title='%s', group='%s', priority=%s",
              req.title[:60], req.assign_group, req.priority)
    return classify_and_store(
        req.title, req.description, req.extracted_text,
        documents=req.documents,
        assign_group=req.assign_group,
        assignee=req.assignee,
        priority=req.priority,
        notes=req.notes,
        discussion_history=req.discussion_history,
        escalation_info=req.escalation_info,
        completion_code=req.completion_code,
        source_ticket_id=req.source_ticket_id,
        affected_system=req.affected_system,
    )


# Classify via GET (quick testing)
@router.get("/classify", response_model=ClassifyResponse)
def classify_incident_get(title: str = "", description: str = "", extracted_text: str = "",
                          source_ticket_id: str = ""):
    _log.info("GET /classify — title='%s', ticket_id='%s'", title[:60], source_ticket_id)
    return classify_and_store(title, description, extracted_text, source_ticket_id=source_ticket_id)


# Classify multiple incidents at once
@router.post("/classify/batch", response_model=ClassifyBatchResponse)
def classify_batch_endpoint(req: ClassifyBatchRequest):
    _log.info("POST /classify/batch — %d incidents", len(req.incidents))
    return classify_batch([inc.model_dump() for inc in req.incidents])


# Mark an incident as resolved
@router.post("/incidents/{incident_id}/resolve", response_model=ResolveResponse)
def resolve(incident_id: str):
    _log.info("POST /incidents/%s/resolve", incident_id)
    if not resolve_incident(incident_id):
        raise HTTPException(status_code=404, detail="Incident not found")
    _log.info("Incident %s resolved", incident_id)
    return ResolveResponse(incident_id=incident_id, status="resolved")


# List all incidents, optional ?status= filter
@router.get("/incidents")
def list_all(status: str | None = Query(None, description="Filter by status (e.g. 'active', 'resolved')")):
    _log.debug("GET /incidents — status=%s", status)
    return list_incidents(status)


# Get a single incident by ID
@router.get("/incidents/{incident_id}", response_model=IncidentResponse)
def get_one(incident_id: str):
    _log.debug("GET /incidents/%s", incident_id)
    inc = get_incident(incident_id)
    if inc is None:
        _log.warning("Incident %s not found", incident_id)
        raise HTTPException(status_code=404, detail="Incident not found")
    return _to_incident_response(inc)


def _to_incident_response(inc: dict) -> IncidentResponse:
    """Map a store row dict → typed IncidentResponse. classification_dict is
    validated through the ClassificationResult Pydantic model; unparseable
    or absent classifications become None instead of 500ing the endpoint."""
    data = dict(inc)
    raw = data.get("classification_dict") or {}
    cls = None
    if raw:
        try:
            cls = ClassificationResult.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 — one bad row must not kill the read path
            _log.warning("Incident %s has an invalid classification: %s", inc.get("id"), exc)
            cls = None
    data["classification"] = cls
    return IncidentResponse(**data)


# ── Clean read endpoints ──────────────────────────────────────────────
# GET /all-incidents — every incident, no clusters, no rollup.


@router.get("/all-incidents", response_model=IncidentListResponse)
def all_incidents(status: str | None = Query(None, description="Filter by status (e.g. 'active', 'resolved')")):
    """Every incident as stored (classification included). Minimal wrapper
    over the store — handy for exports and integrations that want the raw
    list without cluster/report structure."""
    _log.info("GET /all-incidents — status=%s", status)
    incs = list_incidents(status)
    return {"total": len(incs), "incidents": [_to_incident_response(i) for i in incs]}


# Import bulk incidents from a JSON file — only title + description
@router.post("/import/{filename}")
def import_bulk(filename: str):
    _log.info("POST /import/%s", filename)
    result = import_incidents_from_file(filename)
    _log.info("Import %s: %d/%d classified", filename, result.total - result.failed, result.total)
    return result


# Import incidents from request body — DisplayLabel/Description format
@router.post("/import")
def import_bulk_from_body(req: BulkImportRequest):
    _log.info("POST /import — %d incidents from body", len(req.incidents))
    result = import_incidents_from_body([inc.model_dump() for inc in req.incidents])
    _log.info("Import from body: %d/%d classified", result.total - result.failed, result.total)
    return result


# Delete all incidents (resets the store) — destructive, auth-gated.
# Same bearer check as the /api/v1/* integration API (api/auth.py).
@router.post("/reset", dependencies=[Depends(require_token)])
def reset_all():
    count = delete_all_incidents()
    _log.warning("Reset complete — %d incidents deleted", count)
    return {"status": "reset", "deleted": count}
