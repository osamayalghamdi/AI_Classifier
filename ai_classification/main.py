"""FastAPI application — endpoints only."""

from fastapi import FastAPI, HTTPException

from .models import ClassifyRequest, ClassifyResponse, ResolveResponse
from .service import lifespan, get_health, classify_and_store, resolve_incident

app = FastAPI(title="AI Incident Classification", version="0.2.0", lifespan=lifespan)


@app.get("/health")
def health():
    return get_health()


@app.post("/classify", response_model=ClassifyResponse)
def classify_incident(req: ClassifyRequest):
    return classify_and_store(req.title, req.description, req.extracted_text)


@app.get("/classify", response_model=ClassifyResponse)
def classify_incident_get(title: str, description: str = ""):
    return classify_and_store(title, description)


@app.post("/incidents/{incident_id}/resolve", response_model=ResolveResponse)
def resolve(incident_id: str):
    if not resolve_incident(incident_id):
        raise HTTPException(status_code=404, detail="Incident not found")
    return ResolveResponse(incident_id=incident_id, status="resolved")
