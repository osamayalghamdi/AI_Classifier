"""FastAPI application — incident classification endpoint."""

from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager

from .config import settings
from .models import ClassifyRequest, ClassifyResponse
from .classifier import classify


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"  model … {settings.llm_model}")
    print(f"  host … {settings.host}:{settings.port}")
    yield


app = FastAPI(
    title="AI Incident Classification",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Health ───────────────────────────────────────────────────────────


@app.get("/health")
def health():
    return {"status": "ok", "model": settings.llm_model}


# ── Classify ─────────────────────────────────────────────────────────


@app.post("/classify", response_model=ClassifyResponse)
def classify_incident(req: ClassifyRequest):
    """Classify an incident ticket and return structured labels."""
    try:
        result = classify(req.title, req.description)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return ClassifyResponse(
        incident_title=req.title,
        classification=result,
    )


@app.get("/classify", response_model=ClassifyResponse)
def classify_incident_get(title: str, description: str = ""):
    """GET variant — accepts title/description as query params."""
    try:
        result = classify(title, description)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return ClassifyResponse(
        incident_title=title,
        classification=result,
    )
