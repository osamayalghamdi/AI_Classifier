"""Service layer — business logic, orchestration, and app lifecycle."""

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI

from .config import settings
from .models import ClassifyResponse, RelatedIncident, ClassificationResult, ReportResponse, ReportCluster
from .classifier import classify, summarize_cluster, llm_rerank_similar
from .incident_store import IncidentStore, SimilarMatch

_log = logging.getLogger(__name__)

store = IncidentStore()


# ── App lifecycle ──────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"  model … {settings.llm_model}")
    print(f"  host … {settings.host}:{settings.port}")
    store.setup()
    print(f"  store … {'ready' if store.ready else 'FAILED (embeddings disabled)'}")
    yield
    store.close()


# ── Health ─────────────────────────────────────────────────────────────


def get_health() -> dict:
    return {"status": "ok", "model": settings.llm_model, "store_ready": store.ready}


# ── Similarity ─────────────────────────────────────────────────────────


def _cosine_matches(
    title: str,
    description: str,
    extracted_text: str,
    classification: ClassificationResult,
) -> list[SimilarMatch]:
    text = f"{title} {description}"
    return store.find_similar(text, extracted_text=extracted_text, classification=classification)


def _llm_matches(
    title: str,
    description: str,
    extracted_text: str,
    classification: ClassificationResult,
) -> list[SimilarMatch]:
    text = f"{title} {description}"
    candidates = store.find_candidates(
        text,
        extracted_text=extracted_text,
        top_n=settings.llm_rerank_candidates,
        classification=classification,
    )
    if not candidates:
        return []
    try:
        ranked = llm_rerank_similar(title, description, candidates)
    except Exception as exc:
        _log.warning("LLM re-ranking failed: %s", exc)
        return []
    candidate_map = {c["id"]: c for c in candidates}
    results = []
    for item in ranked:
        c = candidate_map.get(item.id)
        if c is None:
            continue
        try:
            cls_result = ClassificationResult.model_validate(json.loads(c["classification_json"]))
        except Exception:
            continue
        results.append(SimilarMatch(
            id=item.id,
            title=c["title"],
            similarity=round(item.similarity / 100, 4),
            classification=cls_result,
            reasoning=item.reasoning or None,
        ))
    return results


def find_matches(
    title: str,
    description: str,
    extracted_text: str,
    classification: ClassificationResult,
) -> list[SimilarMatch]:
    if settings.use_llm_reranking:
        return _llm_matches(title, description, extracted_text, classification)
    return _cosine_matches(title, description, extracted_text, classification)


# ── Cluster ────────────────────────────────────────────────────────────


def update_cluster_summary(cluster_id: str) -> None:
    all_members = store.get_cluster_incidents(cluster_id)
    if len(all_members) < 2:
        return
    full = []
    for m in all_members[-20:]:
        try:
            cls = ClassificationResult.model_validate(json.loads(m["classification_json"]))
            full.append({"title": m["title"], "description": m["description"], "classification": cls})
        except Exception:
            pass
    if full:
        store.update_cluster_summary(cluster_id, summarize_cluster(full))


# ── Classify ───────────────────────────────────────────────────────────


def classify_and_store(
    title: str,
    description: str,
    extracted_text: str = "",
) -> ClassifyResponse:
    result = classify(title, description)
    text = f"{title} {description}"

    cluster_id = store.find_cluster_by_similarity(text, extracted_text=extracted_text, classification=result)
    matches = find_matches(title, description, extracted_text, result)

    incident_id = store.generate_id()
    store.save_incident(incident_id, title, description, result, extracted_text)

    if cluster_id:
        store.add_to_cluster(cluster_id, incident_id, result)
    else:
        cluster_id = store.link_to_cluster(incident_id, result, matches)

    if cluster_id:
        update_cluster_summary(cluster_id)

    return ClassifyResponse(
        incident_title=title,
        classification=result,
        incident_id=incident_id,
        related_incidents=[
            RelatedIncident(
                id=m.id,
                title=m.title,
                similarity=round(m.similarity, 4),
                classification=m.classification,
                reasoning=m.reasoning,
            )
            for m in matches
        ],
    )


# ── Report ─────────────────────────────────────────────────────────────


def build_daily_report() -> ReportResponse:
    now = datetime.now(timezone.utc)
    return build_report(since=now - timedelta(days=1), until=now, label="Today")


def build_weekly_report() -> ReportResponse:
    now = datetime.now(timezone.utc)
    return build_report(since=now - timedelta(days=7), until=now, label="This week")


def build_report(
    since: datetime | None,
    until: datetime | None,
    label: str,
) -> ReportResponse:
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
