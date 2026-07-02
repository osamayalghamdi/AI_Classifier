"""FastAPI application — endpoints only."""

from fastapi import FastAPI

from .models import ClassifyRequest, ClassifyResponse, ReportResponse
from .service import lifespan, get_health, classify_and_store, build_daily_report, build_weekly_report

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


@app.get("/reports/daily", response_model=ReportResponse)
def report_daily():
    return build_daily_report()


@app.get("/reports/weekly", response_model=ReportResponse)
def report_weekly():
    return build_weekly_report()
