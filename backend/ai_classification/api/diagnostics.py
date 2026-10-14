"""Diagnostics endpoints — liveness, smoke tests, full system battery.

Which endpoint for which consumer (they overlap deliberately; the split is
discoverability, not redundancy):

| Endpoint   | Consumer          | What it does                                |
|------------|-------------------|---------------------------------------------|
| /health    | k8s / load balancer liveness | process alive? (returns {status, model, store_ready}) |
| /ready     | k8s readiness (in api/integration.py) | db / embedding / llm one-shot readiness    |
| /status    | dashboard top bar  | per-service status: db / embedding / llm    |
| /test/llm  | human smoke test   | ask the configured model anything, live (auth-gated: spends LLM tokens) |
| /test/all  | human full battery | db → embedding → llm → classify → similar → clusters |

Moved from ai_classification/services/ingest/routes.py (C-3 restructure) —
endpoint behavior, status codes, and response shapes are unchanged.

Pipeline position: 50_api — FastAPI endpoints."""

import logging

from fastapi import APIRouter, Depends, Query

from ai_classification.api.auth import require_token
from ai_classification.shared.config import settings
from ai_classification.shared.store import store
from ai_classification.services.cluster.persistent import build_clusters

_log = logging.getLogger(__name__)

router = APIRouter(tags=["diagnostics"])


# Return service health status (shape identical to the old
# ai_classification.shared.store.get_health)
def get_health() -> dict:
    # Report the ACTIVE classifier — what will actually run. `settings.llm_model`
    # falls back to a default id in config.py, so returning it bare made an
    # unconfigured host look configured: /health named a model while /ready said
    # "not configured" (the two disagreed about the same host).
    active = settings.active_classifier_model
    return {
        "status": "ok",
        "model": active.model_id if active else f"{settings.llm_model} (not configured)",
        "store_ready": store.ready,
    }


# Health check
@router.get("/health")
def health():
    _log.debug("Health check")
    return get_health()


# ── LLM test endpoint — ask the configured model anything ──────────────
# Live smoke test of the LLM: sends a raw prompt to the CURRENTLY
# configured model (whatever .env selects) and returns the raw answer
# plus timing + resolved config. Handy on a fresh VM to confirm the
# company endpoint + key work before anything else.
#
# Auth-gated (Bearer token, same as the /api/v1/* API — api/auth.py):
# this endpoint spends LLM tokens per call, so it must not be open on a
# public ingress. /test/all is gated for the same reason (it runs a real
# LLM completion + a full classify).
@router.post("/test/llm", dependencies=[Depends(require_token)])
@router.get("/test/llm", dependencies=[Depends(require_token)])
def test_llm(question: str = Query("Say hello in one short sentence.", max_length=2000),
             max_tokens: int = Query(200, ge=1, le=1000)):
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
@router.get("/test/all", dependencies=[Depends(require_token)])
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
        from ai_classification.services.jobs.integration import ping
        return ping()

    # 2. Embedding model — encode a string, confirm shape
    def _embedding():
        from ai_classification.shared.store import store
        if not store.embedding_ready():
            raise RuntimeError("embedding model not loaded")
        return f"model={settings.embedding_model_name}, dim={store.embedding_dim()}"

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

    # 6. Clusters — report from the persistent cluster tables + the v5
    #    invariants an operator relies on: a cluster serves ONE system, and a
    #    cluster is >= 2 tickets (1 incident = individual). A violation here is
    #    a WRONG dashboard, so it fails the battery instead of hiding in the
    #    data (cross-system members and empty active rows used to sit in prod
    #    unnoticed for weeks).
    def _clusters():
        rep = build_clusters("daily")
        active = store.list_clusters(status="active")
        cross = sum(len(store.cross_system_members(c["id"], c.get("affected_system") or ""))
                    for c in active)
        empty = sum(1 for c in active if not store.cluster_member_ids(c["id"]))
        detail = (f"{rep.get('total_incidents')} incidents, {len(rep.get('clusters', []))} clusters, "
                  f"{len(active)} active rows")
        if cross or empty:
            raise RuntimeError(
                f"{detail} — INVARIANT VIOLATION: {cross} cross-system member(s), "
                f"{empty} empty active cluster(s)")
        return detail

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
