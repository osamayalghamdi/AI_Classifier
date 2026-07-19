"""FastAPI application — endpoints only."""

import json
import logging
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path

from .schemas import ClassifyRequest, ClassifyResponse, ClassifyBatchRequest, ClassifyBatchResponse, ResolveResponse
from ..core.store import (
    lifespan, get_health, resolve_incident, get_incident, list_incidents, delete_all_incidents,
)
from ..core.classifier import classify_and_store, classify_batch
from ..core.grouping import build_clusters, invalidate_cache, request_rebuild

_log = logging.getLogger(__name__)

app = FastAPI(title="AI Incident Classification", version="0.2.0", lifespan=lifespan)

# CORS — allow dashboard at any origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Health check
@app.get("/health")
def health():
    _log.debug("Health check")
    return get_health()


# Classify a new incident
@app.post("/classify", response_model=ClassifyResponse)
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
    )


# Classify via GET (quick testing)
@app.get("/classify", response_model=ClassifyResponse)
def classify_incident_get(title: str = "", description: str = "", extracted_text: str = ""):
    _log.info("GET /classify — title='%s'", title[:60])
    return classify_and_store(title, description, extracted_text)


# Classify multiple incidents at once
@app.post("/classify/batch", response_model=ClassifyBatchResponse)
def classify_batch_endpoint(req: ClassifyBatchRequest):
    _log.info("POST /classify/batch — %d incidents", len(req.incidents))
    return classify_batch([inc.model_dump() for inc in req.incidents])


# Mark an incident as resolved
@app.post("/incidents/{incident_id}/resolve", response_model=ResolveResponse)
def resolve(incident_id: str):
    _log.info("POST /incidents/%s/resolve", incident_id)
    if not resolve_incident(incident_id):
        raise HTTPException(status_code=404, detail="Incident not found")
    _log.info("Incident %s resolved", incident_id)
    return ResolveResponse(incident_id=incident_id, status="resolved")


# List all incidents, optional ?status= filter
@app.get("/incidents")
def list_all(status: str | None = Query(None, description="Filter by status (e.g. 'active', 'resolved')")):
    _log.debug("GET /incidents — status=%s", status)
    return list_incidents(status)


# Get a single incident by ID
@app.get("/incidents/{incident_id}")
def get_one(incident_id: str):
    _log.debug("GET /incidents/%s", incident_id)
    inc = get_incident(incident_id)
    if inc is None:
        _log.warning("Incident %s not found", incident_id)
        raise HTTPException(status_code=404, detail="Incident not found")
    return inc


# ── Grouping / Reports (frontend-facing) ────────────────────────────


# Return grouped clusters for the dashboard
@app.get("/api/reports/{period}")
def reports(period: str = "daily"):
    _log.info("GET /api/reports/%s — building clusters", period)
    result = build_clusters(period)
    _log.info("Reports %s: %d incidents, %d clusters, %d subsystems",
              period, result.get("total_incidents", 0),
              len(result.get("clusters", [])),
              len(result.get("subsystem_summary", [])))
    return result


# Same, without /api prefix (frontend compat)
@app.get("/reports/{period}")
def reports_no_prefix(period: str = "daily"):
    return reports(period)


# Import bulk incidents from a JSON file — only title + description
@app.post("/import/{filename}")
def import_bulk(filename: str):
    _log.info("POST /import/%s", filename)
    if not filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="Only .json files allowed")
    filepath = Path(__file__).parent.parent.parent / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail=f"File {filename} not found at {filepath}")
    try:
        with open(filepath) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    if not isinstance(data, list):
        raise HTTPException(status_code=400, detail="JSON must be an array of incident objects")

    incidents = []
    for inc in data:
        title = (
            inc.get("title", "") or inc.get("Title", "") or
            inc.get("DisplayLabel", "") or inc.get("display_label", "")
        )
        if isinstance(title, str):
            title = title.strip()
        if not title:
            continue
        desc = (
            inc.get("description", "") or inc.get("Description", "") or
            inc.get("desc", "") or ""
        )
        if isinstance(desc, str):
            desc = desc.strip()
        incidents.append({
            "title": title,
            "description": desc,
        })

    if not incidents:
        raise HTTPException(status_code=400, detail="No incidents with a non-empty title found")

    result = classify_batch(incidents)
    _log.info("Import %s: %d/%d classified", filename, result.total - result.failed, result.total)
    return result


# Delete all incidents (resets the store)
@app.post("/reset")
def reset_all():
    count = delete_all_incidents()
    invalidate_cache()
    request_rebuild()
    _log.warning("Reset complete — %d incidents deleted", count)
    return {"status": "reset", "deleted": count}
