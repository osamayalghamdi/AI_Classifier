"""FastAPI application — incident classification endpoint."""

from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import json

from .config import settings
from .models import ClassifyRequest, ClassifyResponse, RelatedIncident, ReportResponse, ReportCluster, ClassificationResult
from .classifier import classify, summarize_cluster
from .incident_store import IncidentStore


# ── Global store (initialised in lifespan) ────────────────────────────

store = IncidentStore()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"  model … {settings.llm_model}")
    print(f"  host … {settings.host}:{settings.port}")
    store.setup()
    print(
        f"  incident store … {'ready' if store.ready else 'FAILED (embeddings disabled)'}"
    )
    yield
    store.close()


app = FastAPI(
    title="AI Incident Classification",
    version="0.2.0",
    lifespan=lifespan,
)


# ── Health ───────────────────────────────────────────────────────────


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": settings.llm_model,
        "store_ready": store.ready,
    }


# ── Classify ─────────────────────────────────────────────────────────


def _classify_and_store(title: str, description: str, extracted_text: str = "") -> ClassifyResponse:
    """Classify, persist, link to cluster (via centroid or per-incident), return."""
    try:
        result = classify(title, description)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    text = f"{title} {description}"

    # ── Step 1: Try centroid match (fast — O(clusters)) ──────────
    cluster_id = store.find_cluster_by_similarity(text, classification=result)

    # ── Step 2: Find individual similar incidents (for display + fallback clustering) ──
    matches = store.find_similar(text, classification=result)
    related = [
        RelatedIncident(
            id=m.id,
            title=m.title,
            similarity=round(m.similarity, 4),
            classification=m.classification,
        )
        for m in matches
    ]

    # ── Step 3: Persist ───────────────────────────────────────────
    incident_id = store.generate_id()
    store.save_incident(incident_id, title, description, result, extracted_text)

    # ── Step 4: Link to cluster ───────────────────────────────────
    if not cluster_id:
        # No centroid matched — use per-incident similarity
        cluster_id = store.link_to_cluster(incident_id, result, matches)
    else:
        # Centroid matched — add directly to the cluster
        if store._db:
            store._db.execute(
                "INSERT OR IGNORE INTO cluster_members (cluster_id, incident_id, similarity) VALUES (?, ?, 1.0)",
                (cluster_id, incident_id),
            )
            store._db.execute(
                "UPDATE clusters SET updated_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), cluster_id),
            )
            store._db.commit()

    # ── Step 5: Generate summary when cluster hits 2-4 members ─────
    if cluster_id:
        all_members = store._db.execute(
            "SELECT i.title, i.description, i.classification_json "
            "FROM incidents i JOIN cluster_members cm ON cm.incident_id = i.id "
            "WHERE cm.cluster_id = ?",
            (cluster_id,),
        ).fetchall() if store._db else []
        if len(all_members) >= 2 and len(all_members) <= 4:
            full = []
            for row in all_members:
                try:
                    cls = ClassificationResult.model_validate(json.loads(row[2]))
                    full.append({"title": row[0], "description": row[1], "classification": cls})
                except Exception:
                    pass
            if full:
                summary = summarize_cluster(full)
                store.update_cluster_summary(cluster_id, summary)

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


# ── Reports (fast SQL reads — no LLM calls) ─────────────────────────


def _build_report(since: datetime | None, until: datetime | None, label: str) -> ReportResponse:
    since_str = since.isoformat() if since else None
    until_str = until.isoformat() if until else None
    clusters_data = store.get_report(since=since_str, until=until_str)
    total = sum(c["count"] for c in clusters_data)

    report_clusters = [
        ReportCluster(
            summary=c["summary"] or f"{c['count']} related incidents.",
            affected_system=c["affected_system"],
            affected_service=c["affected_service"],
            count=c["count"],
            worst_severity=c["worst_severity"],
            incidents=c["incidents"],
        )
        for c in clusters_data
    ]
    return ReportResponse(period=label, total_incidents=total, clusters=report_clusters)


@app.get("/reports/daily", response_model=ReportResponse)
def report_daily():
    now = datetime.now(timezone.utc)
    return _build_report(since=now - timedelta(days=1), until=now, label="Today")


@app.get("/reports/weekly", response_model=ReportResponse)
def report_weekly():
    now = datetime.now(timezone.utc)
    return _build_report(since=now - timedelta(days=7), until=now, label="This week")
