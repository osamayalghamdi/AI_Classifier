"""FastAPI application — incident classification and reporting."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI

from .config import settings
from .models import (
    ClassifyRequest, ClassifyResponse, RelatedIncident,
    ReportResponse, ReportCluster, ClassificationResult,
)
from .classifier import classify, summarize_cluster
from .incident_store import IncidentStore

store = IncidentStore()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"  model … {settings.llm_model}")
    print(f"  host … {settings.host}:{settings.port}")
    store.setup()
    print(f"  store … {'ready' if store.ready else 'FAILED (embeddings disabled)'}")
    yield
    store.close()


app = FastAPI(title="AI Incident Classification", version="0.2.0", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "model": settings.llm_model, "store_ready": store.ready}


# ── Classify ──────────────────────────────────────────────────────────


def _classify_and_store(title: str, description: str, extracted_text: str = "") -> ClassifyResponse:
    result = classify(title, description)
    text = f"{title} {description}"

    # Centroid match — O(clusters), fast path before scanning all incidents.
    cluster_id = store.find_cluster_by_similarity(text, extracted_text=extracted_text, classification=result)

    # Per-incident similarity — populates related_incidents and fallback clustering.
    matches = store.find_similar(text, extracted_text=extracted_text, classification=result)
    related = [
        RelatedIncident(id=m.id, title=m.title, similarity=round(m.similarity, 4), classification=m.classification)
        for m in matches
    ]

    incident_id = store.generate_id()
    store.save_incident(incident_id, title, description, result, extracted_text)

    if cluster_id:
        store.add_to_cluster(cluster_id, incident_id)
    else:
        cluster_id = store.link_to_cluster(incident_id, result, matches)

    if cluster_id:
        all_members = store.get_cluster_incidents(cluster_id)
        if len(all_members) >= 2:
            sample = all_members[-20:]
            full = []
            for m in sample:
                try:
                    cls = ClassificationResult.model_validate(json.loads(m["classification_json"]))
                    full.append({"title": m["title"], "description": m["description"], "classification": cls})
                except Exception:
                    pass
            if full:
                store.update_cluster_summary(cluster_id, summarize_cluster(full))

    return ClassifyResponse(
        incident_title=title,
        classification=result,
        incident_id=incident_id,
        related_incidents=related,
    )


@app.post("/classify", response_model=ClassifyResponse)
def classify_incident(req: ClassifyRequest):
    return _classify_and_store(req.title, req.description, req.extracted_text)


@app.get("/classify", response_model=ClassifyResponse)
def classify_incident_get(title: str, description: str = ""):
    return _classify_and_store(title, description)


# ── Reports ───────────────────────────────────────────────────────────


def _build_report(since: datetime | None, until: datetime | None, label: str) -> ReportResponse:
    clusters_data = store.get_report(
        since=since.isoformat() if since else None,
        until=until.isoformat() if until else None,
    )
    return ReportResponse(
        period=label,
        total_incidents=sum(c["count"] for c in clusters_data),
        clusters=[
            ReportCluster(
                cluster_id=c["cluster_id"],
                summary=c["summary"] or f"{c['count']} related incidents.",
                affected_system=c["affected_system"],
                affected_service=c["affected_service"],
                count=c["count"],
                worst_severity=c["worst_severity"],
                incidents=c["incidents"],
            )
            for c in clusters_data
        ],
    )


@app.get("/reports/daily", response_model=ReportResponse)
def report_daily():
    now = datetime.now(timezone.utc)
    return _build_report(since=now - timedelta(days=1), until=now, label="Today")


@app.get("/reports/weekly", response_model=ReportResponse)
def report_weekly():
    now = datetime.now(timezone.utc)
    return _build_report(since=now - timedelta(days=7), until=now, label="This week")
