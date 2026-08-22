"""FastAPI application — endpoints only. No business logic.
Pipeline position: 50_api — FastAPI endpoints."""

import logging
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from ai_classification.api.schemas import (
    ClassifyRequest, ClassifyResponse, ClassifyBatchRequest, ClassifyBatchResponse,
    ResolveResponse, BulkImportRequest, IncidentResponse, IncidentListResponse,
)
from ai_classification.domain.models import ClassificationResult
from ai_classification.shared.store import (
    lifespan, get_health, resolve_incident, get_incident, list_incidents, delete_all_incidents,
    store,
)
from ai_classification.services.classify.classifier import classify_and_store, classify_batch
from ai_classification.shared.config import settings
from ai_classification.services.cluster.persistent import build_clusters, sweep_pool
from ai_classification.services.ingest.import_service import import_incidents_from_file, import_incidents_from_body
from ai_classification.services.review.cluster_proposal_routes import router as cluster_proposal_router
from ai_classification.services.review.taxonomy_gaps_routes import router as taxonomy_gaps_router

_log = logging.getLogger(__name__)

app = FastAPI(title="AI Incident Classification", version="0.2.0", lifespan=lifespan)


# ── Structured validation errors (E8) — unknown fields / bad types map
#    to 422 INVALID_PAYLOAD with stable machine-readable codes instead of
#    FastAPI's default 422 shape.
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from ai_classification.services.jobs.integration.schemas import Err


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_request, exc: RequestValidationError):
    fields = [
        {
            "field": ".".join(str(p) for p in err.get("loc", []) if p not in ("body", "query", "path")),
            "issue": err.get("type", "invalid"),
            "message": err.get("msg", ""),
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": Err.INVALID_PAYLOAD,
                "message": "Payload failed validation — see fields",
                "fields": fields,
            }
        },
    )


# Structured HTTP errors: dict details with an "error" key are returned
# as-is ({"error": {...}}); plain-string details keep FastAPI's default
# {"detail": ...} shape so existing endpoints are untouched.
from fastapi import HTTPException as _HTTPException


@app.exception_handler(_HTTPException)
async def _http_error_handler(_request, exc: _HTTPException):
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

# Cluster-proposal review API (v2 persistent clustering — human gate)
app.include_router(cluster_proposal_router)
# Taxonomy gaps review API (classifier v3 OFFERING-GAP surface)
app.include_router(taxonomy_gaps_router)
from ai_classification.services.ingest.integration_routes import router as integration_router, ready_router
app.include_router(integration_router)
app.include_router(ready_router)

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


# ── LLM test endpoint — ask the configured model anything ──────────────
# Live smoke test of the LLM: sends a raw prompt to the CURRENTLY
# configured model (whatever .env selects) and returns the raw answer
# plus timing + resolved config. Handy on a fresh VM to confirm the
# company endpoint + key work before anything else.
@app.post("/test/llm")
@app.get("/test/llm")
def test_llm(question: str = "Say hello in one short sentence.", max_tokens: int = 200):
    import time

    from ai_classification.services.classify.llm import call_llm

    _log.info("GET /test/llm — question='%s'", question[:80])
    t0 = time.time()
    try:
        answer = call_llm(
            [{"role": "user", "content": question}],
            max_tokens=max_tokens,
            temperature=0.2,
        )
        return {
            "status": "ok",
            "model": settings.llm_model,
            "api_base": settings.llm_api_base or "(provider default)",
            "question": question,
            "answer": answer,
            "latency_s": round(time.time() - t0, 2),
        }
    except Exception as exc:  # noqa: BLE001 — report the failure, don't hide it
        _log.warning("GET /test/llm FAILED — %s", exc)
        return {
            "status": "error",
            "model": settings.llm_model,
            "api_base": settings.llm_api_base or "(provider default)",
            "question": question,
            "error": str(exc)[:300],
            "latency_s": round(time.time() - t0, 2),
        }


# ── Full system test — one call runs the whole battery ─────────────────
# db → embedding → llm → classify → similar → clusters. Each check is a
# REAL call against the live stack; failures are reported per-check (the
# battery continues, never aborts mid-way). Rollup: all ok = HTTP 200.
@app.get("/test/all")
def test_all():
    import time as _t

    from ai_classification.services.classify.llm import call_llm

    _log.info("GET /test/all — running full system battery")
    results: list[dict] = []

    def _check(name: str, fn) -> None:
        t0 = _t.time()
        try:
            detail = fn()
            results.append({
                "check": name, "status": "ok",
                "detail": detail, "latency_s": round(_t.time() - t0, 2),
            })
        except Exception as exc:  # noqa: BLE001
            results.append({
                "check": name, "status": "error",
                "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
                "latency_s": round(_t.time() - t0, 2),
            })

    # 1. DB — connect + count incidents
    def _db():
        from ai_classification.services.jobs.integration import _connect
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM incidents")
                return f"connected, {cur.fetchone()[0]} incidents"

    # 2. Embedding model — encode a string, confirm shape
    def _embedding():
        import numpy as np
        from ai_classification.shared.store import store
        if store._model is None:
            raise RuntimeError("embedding model not loaded")
        v = store._model.encode("test ticket")
        return f"model={settings.embedding_model_name}, dim={np.asarray(v).shape[-1]}"

    # 3. LLM — real completion against the configured endpoint
    def _llm():
        answer = call_llm(
            [{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=10, temperature=0.0,
        )
        return f"model={settings.llm_model}, base={settings.llm_api_base or '(default)'}, reply={answer.strip()[:40]!r}"

    # 4. Classification — full pipeline on a sample ticket (no store write)
    def _classify():
        from ai_classification.services.classify.classifier import classify
        r = classify("Rawdah permit booking fails on date selection", "error on the done button")
        return f"{r.affected_system} / {r.service} / {r.severity}"

    # 5. Similar-ticket retrieval — nearest neighbours for a sample
    def _similar():
        from ai_classification.shared.store import store
        if not store.ready:
            raise RuntimeError("store not ready")
        hits = store.find_similar("Rawdah permit booking fails on date selection", top_k=3)
        return f"{len(hits)} similar found"

    # 6. Clusters — report from the persistent cluster tables
    def _clusters():
        rep = build_clusters("daily")
        return f"{rep.get('total_incidents')} incidents, {len(rep.get('clusters', []))} clusters"

    _check("db", _db)
    _check("embedding", _embedding)
    _check("llm", _llm)
    _check("classify", _classify)
    _check("similar", _similar)
    _check("clusters", _clusters)

    ok = all(r["status"] == "ok" for r in results)
    return {
        "status": "ok" if ok else "degraded",
        "model": settings.llm_model,
        "api_base": settings.llm_api_base or "(provider default)",
        "checked_at": _t.strftime("%Y-%m-%d %H:%M:%S"),
        "checks": results,
    }


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
        source_ticket_id=req.source_ticket_id,
        affected_system=req.affected_system,
    )


# Classify via GET (quick testing)
@app.get("/classify", response_model=ClassifyResponse)
def classify_incident_get(title: str = "", description: str = "", extracted_text: str = "",
                          source_ticket_id: str = ""):
    _log.info("GET /classify — title='%s', ticket_id='%s'", title[:60], source_ticket_id)
    return classify_and_store(title, description, extracted_text, source_ticket_id=source_ticket_id)


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
@app.get("/incidents/{incident_id}", response_model=IncidentResponse)
def get_one(incident_id: str):
    _log.debug("GET /incidents/%s", incident_id)
    inc = get_incident(incident_id)
    if inc is None:
        _log.warning("Incident %s not found", incident_id)
        raise HTTPException(status_code=404, detail="Incident not found")
    return _to_incident_response(inc)


def _to_incident_response(inc: dict) -> IncidentResponse:
    """Map a store row dict → typed IncidentResponse. classification_dict is
    validated through the ClassificationResult Pydantic model; unparseable
    or absent classifications become None instead of 500ing the endpoint."""
    data = dict(inc)
    raw = data.get("classification_dict") or {}
    cls = None
    if raw:
        try:
            cls = ClassificationResult.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 — one bad row must not kill the read path
            _log.warning("Incident %s has an invalid classification: %s", inc.get("id"), exc)
            cls = None
    data["classification"] = cls
    return IncidentResponse(**data)


# ── Clean read endpoints ──────────────────────────────────────────────
# GET /all-incidents — every incident, no clusters, no rollup.
# GET /clusters — the clusters ONLY (names, counts, severity, member ids);
#                 no subsystem rollup, no per-member incident dumps.


@app.get("/all-incidents", response_model=IncidentListResponse)
def all_incidents(status: str | None = Query(None, description="Filter by status (e.g. 'active', 'resolved')")):
    """Every incident as stored (classification included). Minimal wrapper
    over the store — handy for exports and integrations that want the raw
    list without cluster/report structure."""
    _log.info("GET /all-incidents — status=%s", status)
    incs = list_incidents(status)
    return {"total": len(incs), "incidents": [_to_incident_response(i) for i in incs]}


@app.get("/clusters")
def clusters_only(period: str = "daily"):
    """The clusters only: name, count, worst severity, member incident IDs.
    Lightweight — the dashboard report (/api/reports/{period}) carries the
    same clusters plus subsystem rollup and full member details; this
    endpoint returns just the grouping summary."""
    _log.info("GET /clusters — period=%s", period)
    result = build_clusters(period)
    clusters = []
    for c in result.get("clusters", []):
        clusters.append({
            "cluster_id": c.get("cluster_id"),
            "name": c.get("name"),
            "description": c.get("description") or c.get("name"),
            "affected_system": c.get("affected_system"),
            "affected_service": c.get("affected_service"),
            "worst_severity": c.get("worst_severity"),
            "count": c.get("count"),
            "member_ids": [i.get("id") for i in c.get("incidents", [])],
        })
    return {
        "total_incidents": result.get("total_incidents", 0),
        "clusters": clusters,
    }


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
    """Frontend-compat alias for /api/reports/{period} — dashboard uses this path."""
    return reports(period)


# Manual-review queue (Recovery job: exhausted retries) — feeds review.html
@app.get("/review-queue")
def review_queue():
    items = store.queue_list()
    by_id = {i["id"]: i for i in store.list_incidents()}
    for it in items:
        it["title"] = (by_id.get(it["incident_id"], {}) or {}).get("title", "")
    return {"items": items}


# Import bulk incidents from a JSON file — only title + description
@app.post("/import/{filename}")
def import_bulk(filename: str):
    _log.info("POST /import/%s", filename)
    result = import_incidents_from_file(filename)
    _log.info("Import %s: %d/%d classified", filename, result.total - result.failed, result.total)
    return result


# Import incidents from request body — DisplayLabel/Description format
@app.post("/import")
def import_bulk_from_body(req: BulkImportRequest):
    _log.info("POST /import — %d incidents from body", len(req.incidents))
    result = import_incidents_from_body([inc.model_dump() for inc in req.incidents])
    _log.info("Import from body: %d/%d classified", result.total - result.failed, result.total)
    return result


# Delete all incidents (resets the store)
@app.post("/reset")
def reset_all():
    count = delete_all_incidents()
    _log.warning("Reset complete — %d incidents deleted", count)
    return {"status": "reset", "deleted": count}


# Manually trigger a Flow B pool sweep (v2 persistent clustering). Returns
# the sweep stats; proposals land in /cluster-proposals for the human gate.
@app.post("/cluster/sweep")
def trigger_sweep(dry_run: bool = False):
    _log.info("POST /cluster/sweep — dry_run=%s", dry_run)
    return sweep_pool(dry_run=dry_run)
