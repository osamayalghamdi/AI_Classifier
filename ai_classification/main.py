"""FastAPI application — incident classification endpoint."""

from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager

from .config import settings
from .models import ClassifyRequest, ClassifyResponse, RelatedIncident
from .classifier import classify
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
    version="0.1.0",
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


@app.post("/classify", response_model=ClassifyResponse)
def classify_incident(req: ClassifyRequest):
    """Classify an incident ticket and return structured labels."""
    try:
        result = classify(req.title, req.description)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # ── Semantic similarity search ────────────────────────────────
    text = f"{req.title} {req.description}"
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

    # ── Persist ───────────────────────────────────────────────────
    incident_id = store.generate_id()
    store.save_incident(incident_id, req.title, req.description, result)

    return ClassifyResponse(
        incident_title=req.title,
        classification=result,
        incident_id=incident_id,
        related_incidents=related,
    )


@app.get("/classify", response_model=ClassifyResponse)
def classify_incident_get(title: str, description: str = ""):
    """GET variant — accepts title/description as query params."""
    try:
        result = classify(title, description)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    text = f"{title} {description}"
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

    incident_id = store.generate_id()
    store.save_incident(incident_id, title, description, result)

    return ClassifyResponse(
        incident_title=title,
        classification=result,
        incident_id=incident_id,
        related_incidents=related,
    )
