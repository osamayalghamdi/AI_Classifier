"""Service layer — business logic, orchestration, and app lifecycle."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import settings
from .models import ClassifyResponse, SimilarOpenIncident
from .classifier import classify
from .incident_store import IncidentStore

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


# ── Classify ───────────────────────────────────────────────────────────


def classify_and_store(
    title: str,
    description: str,
    extracted_text: str = "",
) -> ClassifyResponse:
    result = classify(title, description)
    text = f"{title} {description}"

    # Live deduplication — only checks against other active (unresolved) incidents.
    matches = store.find_similar(text, extracted_text=extracted_text, classification=result)

    incident_id = store.generate_id()
    store.save_incident(incident_id, title, description, result, extracted_text)

    return ClassifyResponse(
        incident_title=title,
        classification=result,
        incident_id=incident_id,
        similar_open_incidents=[
            SimilarOpenIncident(
                id=m.id,
                title=m.title,
                similarity=round(m.similarity, 4),
                classification=m.classification,
            )
            for m in matches
        ],
    )


# ── Resolve ────────────────────────────────────────────────────────────


def resolve_incident(incident_id: str) -> bool:
    """Mark an incident resolved so it stops surfacing in duplicate checks."""
    return store.resolve_incident(incident_id)
