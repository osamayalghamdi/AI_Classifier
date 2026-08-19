"""Cluster-proposal review API — the human gate for v2 persistent clustering.

GET  /cluster-proposals                  list proposals (status=proposed default)
POST /cluster-proposals/{id}/decision    approve | reject

  approve -> cluster status flips 'proposed' -> 'active'; members are ALREADY
             attached (Flow B inserts them with the proposal) — no copy.
  reject  -> members are removed (back to the unassigned pool) and the cluster
             is retired (kept for the audit trail).

One-shot rule (mirrors the sub-offering proposals): only status='proposed'
clusters can be decided; repeat calls return the row unchanged.
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ai_classification.shared.store import store

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/cluster-proposals", tags=["cluster-proposals"])


class ClusterProposalDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    note: str = ""


def _enrich(cluster: dict) -> dict:
    """Attach member tickets (id + title + description) for the review UI."""
    members = store.list_cluster_members(cluster["id"])
    cluster["member_ids"] = [m["incident_id"] for m in members]
    cluster["members"] = [{"id": m["incident_id"], "title": m["title"],
                           "description": m["description"]} for m in members]
    return cluster


@router.get("")
def list_cluster_proposals(status: str | None = "proposed"):
    _log.info("GET /cluster-proposals — status=%s", status)
    props = [_enrich(c) for c in store.list_clusters(status=status)]
    return {"proposals": props, "total": len(props)}


@router.get("/{cluster_id}")
def get_cluster_proposal(cluster_id: str):
    c = store.get_cluster(cluster_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return _enrich(c)


@router.post("/{cluster_id}/decision")
def decide_cluster_proposal(cluster_id: str, req: ClusterProposalDecisionRequest):
    cluster = store.get_cluster(cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if cluster["status"] != "proposed":
        raise HTTPException(status_code=409,
                            detail=f"Proposal already decided: {cluster['status']}")

    if req.decision == "approve":
        store.set_cluster_status(cluster_id, "active")
        _log.info("cluster proposal %s APPROVED -> active (%d members)",
                  cluster_id[:10], len(store.cluster_member_ids(cluster_id)))
    else:  # reject
        n = store.remove_cluster_members(cluster_id)
        store.set_cluster_status(cluster_id, "retired")
        _log.info("cluster proposal %s REJECTED -> retired (%d members returned to pool)",
                  cluster_id[:10], n)

    updated = store.get_cluster(cluster_id)
    return _enrich(updated) if updated else cluster
