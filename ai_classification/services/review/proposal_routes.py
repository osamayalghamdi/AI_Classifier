"""Proposal review API — human-in-the-loop gate for the sub-offering engine.

GET  /proposals                          list (filter: status, offering_id)
POST /proposals/{id}/decision            approve | reject | merge
  approve -> mint ACTIVE sub_offering + exemplars = cluster tickets; pool drained
  reject  -> members stay in pool with 24h cooldown (excluded from next batch)
  merge   -> attach members as exemplars of an existing sub_offering; pool drained

One-shot rule: only 'pending' proposals can be decided; repeat calls return the
row unchanged (idempotent, mirrors store.decide_proposal).
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ai_classification.core.store import store
from ai_classification.core.suboffering import embed_pure

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/proposals", tags=["proposals"])


class ProposalDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject|merge)$")
    target_sub_offering_id: str = ""
    note: str = ""
    # W3: approve with a new_offering_name mints a NEW OFFERING (first
    # sub-offering under that offering id) instead of the proposal's pool
    # offering id — the OFFERING-000 path (proposal.offering_id == "OFFERING-000").
    new_offering_name: str = ""


def _mint_sub_offering(proposal: dict, offering_id: str | None = None) -> dict:
    """Approve: mint ACTIVE sub_offering + exemplars from cluster tickets.

    offering_id defaults to the proposal's pool offering; pass an explicit
    offering id to mint under a NEW offering (OFFERING-000 decisions)."""
    incident_ids = proposal["member_ids"]
    incidents = {i["id"]: i for i in store.list_incidents()}
    sub = store.create_sub_offering(
        offering_id=offering_id or proposal["offering_id"],
        name=proposal.get("proposed_label") or "sub-offering",
        created_from_cluster_id=proposal["id"],
        status="active",
    )
    if sub is None:
        raise HTTPException(status_code=500, detail="store unavailable")
    for iid in incident_ids:
        inc = incidents.get(iid)
        if inc is None:
            continue
        emb = embed_pure(inc.get("title", ""), inc.get("description", ""))
        if emb is not None:
            store.add_exemplar(sub["id"], iid, inc.get("title", ""),
                               inc.get("description", ""), emb)
    store.pool_remove_many(proposal["offering_id"], incident_ids)
    _log.info("proposal %s approved -> sub_offering %s (%d exemplars)",
              proposal["id"], sub["id"], len(incident_ids))
    return sub


@router.get("")
def list_proposals(status: str | None = None, offering_id: str | None = None):
    _log.info("GET /proposals — status=%s offering=%s", status, offering_id)
    props = store.list_proposals(status=status, offering_id=offering_id)
    # Enrich with member titles so the review UI needs no N+1 detail calls.
    by_id = {i["id"]: i for i in store.list_incidents()}
    for p in props:
        p["members"] = [{"id": iid, "title": (by_id.get(iid, {}) or {}).get("title", "")}
                        for iid in p.get("member_ids", [])]
    return {"proposals": props, "total": len(props)}


@router.get("/{proposal_id}")
def get_proposal(proposal_id: str):
    prop = store.get_proposal(proposal_id)
    if prop is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return prop


@router.post("/{proposal_id}/decision")
def decide(proposal_id: str, req: ProposalDecisionRequest):
    prop = store.get_proposal(proposal_id)
    if prop is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if prop["status"] != "pending":
        raise HTTPException(status_code=409,
                            detail=f"Proposal already decided: {prop['status']}")

    if req.decision == "approve":
        # W3: approve + new_offering_name -> mint under a NEW offering
        sub = _mint_sub_offering(prop, offering_id=req.new_offering_name or None)
        store.decide_proposal(proposal_id, "approve", target_sub_offering_id=sub["id"],
                              note=req.note)
    elif req.decision == "reject":
        until = datetime.now(timezone.utc) + timedelta(hours=24)
        store.pool_set_cooldown(prop["offering_id"], prop["member_ids"], until)
        store.decide_proposal(proposal_id, "reject", note=req.note)
    elif req.decision == "merge":
        target = req.target_sub_offering_id
        if not target:
            raise HTTPException(status_code=422,
                                detail="merge requires target_sub_offering_id")
        if store.get_sub_offering(target) is None:
            raise HTTPException(status_code=404, detail="Target sub-offering not found")
        incidents = {i["id"]: i for i in store.list_incidents()}
        for iid in prop["member_ids"]:
            inc = incidents.get(iid)
            if inc is None:
                continue
            emb = embed_pure(inc.get("title", ""), inc.get("description", ""))
            if emb is not None:
                store.add_exemplar(target, iid, inc.get("title", ""),
                                   inc.get("description", ""), emb)
        store.pool_remove_many(prop["offering_id"], prop["member_ids"])
        store.decide_proposal(proposal_id, "merge", target_sub_offering_id=target,
                              note=req.note)

    updated = store.get_proposal(proposal_id)
    _log.info("proposal %s decision=%s", proposal_id, req.decision)
    return updated
