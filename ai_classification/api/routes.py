"""FastAPI application — endpoints only."""

from fastapi import FastAPI, HTTPException, Query

from .schemas import ClassifyRequest, ClassifyResponse, ClassifyBatchRequest, ClassifyBatchResponse, ResolveResponse
from ..core.store import (
    lifespan, get_health, resolve_incident, get_incident, list_incidents, delete_all_incidents,
)
from ..core.classifier import classify_and_store, classify_batch
from ..core.grouping import build_clusters

app = FastAPI(title="AI Incident Classification", version="0.2.0", lifespan=lifespan)


# Health check
@app.get("/health")
def health():
    return get_health()


# Classify a new incident
@app.post("/classify", response_model=ClassifyResponse)
def classify_incident(req: ClassifyRequest):
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
def classify_incident_get(title: str, description: str = "", extracted_text: str = ""):
    return classify_and_store(title, description, extracted_text)


# Classify multiple incidents at once
@app.post("/classify/batch", response_model=ClassifyBatchResponse)
def classify_batch_endpoint(req: ClassifyBatchRequest):
    return classify_batch([inc.model_dump() for inc in req.incidents])


# Mark an incident as resolved
@app.post("/incidents/{incident_id}/resolve", response_model=ResolveResponse)
def resolve(incident_id: str):
    if not resolve_incident(incident_id):
        raise HTTPException(status_code=404, detail="Incident not found")
    return ResolveResponse(incident_id=incident_id, status="resolved")


# List all incidents, optional ?status= filter
@app.get("/incidents")
def list_all(status: str | None = Query(None, description="Filter by status (e.g. 'active', 'resolved')")):
    return list_incidents(status)


# Get a single incident by ID
@app.get("/incidents/{incident_id}")
def get_one(incident_id: str):
    inc = get_incident(incident_id)
    if inc is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return inc


# ── Grouping / Reports (frontend-facing) ────────────────────────────


# Return grouped clusters for the dashboard
@app.get("/api/reports/{period}")
def reports(period: str = "daily"):
    return build_clusters(period)


# Same, without /api prefix (frontend compat)
@app.get("/reports/{period}")
def reports_no_prefix(period: str = "daily"):
    return build_clusters(period)


# Delete all incidents (resets the store)
@app.post("/reset")
def reset_all():
    count = delete_all_incidents()
    return {"status": "reset", "deleted": count}
