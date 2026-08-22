"""Cluster / report endpoints — dashboard grouping, clusters, review queue,
manual sweep trigger.

Moved from ai_classification/services/ingest/routes.py (C-3 restructure) —
endpoint behavior, status codes, and response shapes are unchanged.

Pipeline position: 50_api — FastAPI endpoints."""

import logging

from fastapi import APIRouter

from ai_classification.shared.store import store
from ai_classification.services.cluster.persistent import build_clusters, sweep_pool

_log = logging.getLogger(__name__)

router = APIRouter(tags=["reports"])


# Return grouped clusters for the dashboard
@router.get("/api/reports/{period}")
def reports(period: str = "daily"):
    _log.info("GET /api/reports/%s — building clusters", period)
    result = build_clusters(period)
    _log.info("Reports %s: %d incidents, %d clusters, %d subsystems",
              period, result.get("total_incidents", 0),
              len(result.get("clusters", [])),
              len(result.get("subsystem_summary", [])))
    return result


# Same, without /api prefix (frontend compat)
@router.get("/reports/{period}")
def reports_no_prefix(period: str = "daily"):
    """Frontend-compat alias for /api/reports/{period} — dashboard uses this path."""
    return reports(period)


# The clusters ONLY (names, counts, severity, member ids); no subsystem
# rollup, no per-member incident dumps.
@router.get("/clusters")
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


# Manual-review queue (Recovery job: exhausted retries) — feeds review.html
@router.get("/review-queue")
def review_queue():
    items = store.queue_list()
    by_id = {i["id"]: i for i in store.list_incidents()}
    for it in items:
        it["title"] = (by_id.get(it["incident_id"], {}) or {}).get("title", "")
    return {"items": items}


# Manually trigger a Flow B pool sweep (v2 persistent clustering). Returns
# the sweep stats; proposals land in /cluster-proposals for the human gate.
@router.post("/cluster/sweep")
def trigger_sweep(dry_run: bool = False):
    _log.info("POST /cluster/sweep — dry_run=%s", dry_run)
    return sweep_pool(dry_run=dry_run)
