"""LLM-first persistent clustering (v2) — clusters are DB rows, the LLM decides.

Replaces the stateless 5-minute rebuild (grouping.py's _build_clusters +
start_rebuild_loop). Core principles:

1. Clusters are persistent rows (tables `clusters` + `cluster_members`).
   A cluster created today exists tomorrow with the same id, name, members.
2. A cluster can be born with 2 members and live — no min-size kill.
3. Cross-offering retrieval — the offering is a FEATURE the LLM sees, not a wall.
4. The LLM decides membership; embeddings only shortlist candidates.
5. Every LLM decision is logged to `assignment_log` (the audit trail).
6. Human review gates cluster creation: proposals -> approve -> active.
   Assignment to an existing ACTIVE cluster is automatic.

Flows:
  Flow A — assign_incident(): per new classified ticket. Embed -> retrieve top-5
           active clusters -> LLM verdict (assign | none_fit). assign+high/medium
           inserts the member; everything else stays in the derived unassigned
           pool (incidents with no cluster_members row).
  Flow B — sweep_pool(): every sweep interval. Re-runs Flow A on the pool (new
           clusters may have appeared), then batch-groups the remainder into
           PROPOSALS (status='proposed', members attached) for the human gate.
           ID reconciliation + log on mismatch (validate_group safeguard).
  Flow C — audit_cluster(): nightly. LLM reads ALL member texts + the cluster
           description -> keep/remove (reasons) / refined description. Removed
           members return to the pool, logged. 60% pruning floor + ID
           reconciliation kept from validate_group.
  Flow D — regenerate_name(): Arabic name stored ON the cluster row; regenerated
           only when the audit changes membership or description.

Pipeline position: 30_cluster — persistent, incremental, LLM-decided.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time

import numpy as np

from ai_classification.shared.config import settings
from ai_classification.shared.store import store
from ai_classification.services.classify.llm import call_llm, strip_json_fences
from ai_classification.services.match.suboffering import OFFERING_000, embed_pure, offering_of
from ai_classification.services.cluster.grouping import (
    _class_field,
    _dominant_labels,
    _extract_canonical_statement,
    _extract_severity,
    _parse_classification,
    _subsystem_rollup,
    _worst_severity,
)

_log = logging.getLogger(__name__)

# ── Versioning / constants ────────────────────────────────────────────────

PROMPT_VERSION = "clustering-v2-2026-08"
MAX_CANDIDATES = 5          # active clusters shown to the LLM per assignment
SWEEP_BATCH_SIZE = 20       # tickets per Flow-B grouping call
PROPOSAL_MIN_SIZE = 2       # a proposal needs >= 2 tickets
AUDIT_PRUNE_FLOOR = 0.6     # audit may not remove >60% of a coherent cluster
AUDIT_MAX_MEMBERS = 40      # cap member cards in one audit call
REPRESENTATIVE_MEMBERS = 3  # member texts per cluster card
_CARD_TITLE_MAX = 120
_CARD_DESC_MAX = 200
_TICKET_DESC_MAX = 300

# ── Prompts (versioned — see PROMPT_VERSION) ──────────────────────────────

ASSIGN_PROMPT = """You are triaging an incident ticket on a NOC dashboard into problem clusters.
A cluster = ONE specific underlying problem, not a service area.
Same feature + same failure = same cluster. Same feature + different failure = different cluster.
Example: "cannot enter pilgrim numbers" and "registration rejects numbers not starting with 4"
are BOTH in Registration but are DIFFERENT problems → different clusters.

Ticket: {ticket}
Candidate clusters: {cards}

Return JSON only:
{{"action": "assign" | "none_fit",
 "cluster_id": "<id or null>",
 "confidence": "high" | "medium" | "low",
 "reason": "<one sentence>"}}
Rules: assign ONLY if the ticket describes the SAME underlying problem as the
cluster's description — not merely the same service, screen, or system.
If unsure, return none_fit. none_fit is a safe answer; wrong assignment is not."""

SWEEP_PROMPT = """Here are unassigned incident tickets. Group tickets that describe the SAME
underlying problem. A group needs >= 2 tickets. Tickets that match nothing stay alone.

Return JSON only:
{{"groups": [{{"member_ids": [...], "name_ar": "<short Arabic title for the group, max 6 words>",
"description": "what this problem is, and what it is NOT"}}],
 "singletons": ["<ids that match no group>"]}}
Never invent IDs. Every input ID appears exactly once across groups and singletons."""

AUDIT_PROMPT = """You are auditing an incident cluster on a NOC dashboard for purity.

Cluster description: {description}

Here are ALL tickets currently in the cluster. Each ticket must describe the
SAME underlying problem as the cluster description. A ticket about a different
problem — even in the same service, screen, or system — must be REMOVED.

Tickets:
{tickets}

Return JSON only:
{{"keep": ["<ids that belong>"],
 "remove": [{{"id": "<id>", "reason": "<one sentence — what makes it different>"}}],
 "description": "<optionally refined cluster description, or the original>"}}
Rules: Every input ID appears in exactly one of keep or remove. Never invent or
omit an ID. Be precise: same feature + different failure = remove."""

# Flow-D naming prompt (kept from the legacy engine — same behavior, the name
# is now stored on the cluster row; no fingerprint cache for active clusters).
AR_NAME_PROMPT = """You are naming an incident cluster on a NOC shift dashboard.

Here are the actual tickets in the cluster (title: description). Read them
all and identify the ONE shared problem. Give the cluster a short, simple
ARABIC title (max 6 words) that describes that problem the way an operator
would say it — e.g. "فشل إصدار تصريح الروضة" (Rawdah permit issuance fails),
not the system or service name.

Rules:
- MUST be in Arabic script (العربية), NOT transliterated.
- Short and simple — what is broken, not which system.
- Return ONLY the title — no quotes, no explanation, no English.

Tickets:
{tickets}"""

_AR_NAME_MAX_TICKETS = 15


# ── Small helpers ─────────────────────────────────────────────────────────

def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _cluster_id(name_ar: str, member_ids: list[str]) -> str:
    """Stable cluster id: cl_<sha256(name|sorted member ids)[:12]>."""
    key = f"{name_ar}|{','.join(sorted(member_ids))}"
    return "cl_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _json_class(inc: dict) -> dict:
    data = inc.get("classification_dict")
    if isinstance(data, dict):
        return data
    try:
        return json.loads(inc.get("classification_json") or "{}")
    except Exception:
        return {}


def _offering_of_inc(inc: dict) -> str:
    return offering_of(_json_class(inc).get("service")) or OFFERING_000


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ── Retrieval (embeddings shortlist ONLY — the LLM decides) ───────────────

def retrieve_candidates(incident: dict, top_k: int = MAX_CANDIDATES) -> list[dict]:
    """Top-K active clusters by max cosine(ticket, member centroid / members).

    All offerings. Each card carries the cluster name, description, member
    offerings and 3 representative member texts — everything the LLM needs
    to decide membership. No threshold: candidates are ranked, not gated.
    """
    emb = embed_pure(incident.get("title", ""), incident.get("description", ""))
    if emb is None:
        return []
    active = store.list_clusters(status="active")
    scored: list[tuple[float, dict]] = []
    for c in active:
        members = store.cluster_member_embeddings(c["id"])
        if members:
            vecs = np.stack([m["embedding"] for m in members])
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            vecs = vecs / np.clip(norms, 1e-9, None)
            centroid = vecs.mean(axis=0)
            centroid = centroid / np.clip(np.linalg.norm(centroid), 1e-9, None)
            member_sims = vecs @ emb / np.clip(np.linalg.norm(emb), 1e-9, None)
            score = max(float(centroid @ emb / np.clip(np.linalg.norm(emb), 1e-9, None)),
                        float(member_sims.max()))
        else:
            # No member embeddings — fall back to the description as proxy.
            desc_emb = embed_pure(c.get("name_ar", ""), c.get("description", ""))
            if desc_emb is None:
                continue
            score = _cos(emb, desc_emb)
        scored.append((score, c))
    scored.sort(key=lambda t: -t[0])

    cards = []
    for score, c in scored[:top_k]:
        members = store.list_cluster_members(c["id"])
        offerings = sorted({_offering_of_inc(m) for m in members if isinstance(m, dict)})
        reps = [
            {"id": m["incident_id"],
             "title": (m.get("title") or "")[:_CARD_TITLE_MAX],
             "description": (m.get("description") or "")[:_CARD_DESC_MAX]}
            for m in members[:REPRESENTATIVE_MEMBERS]
        ]
        cards.append({
            "cluster_id": c["id"],
            "name_ar": c["name_ar"],
            "description": c.get("description") or "",
            "offerings": offerings,
            "representative_members": reps,
            "retrieval_score": round(score, 3),
        })
    return cards


# ── Flow A — assign on arrival ────────────────────────────────────────────

def assign_incident(incident_id: str) -> dict:
    """LLM-decided assignment of one ticket to an existing active cluster.

    Verdicts: assign+high/medium -> member inserted; assign+low / none_fit ->
    ticket stays in the derived pool. EVERY call is logged to assignment_log.
    """
    inc = store.get_incident(incident_id)
    if inc is None:
        _log.warning("assign_incident: incident %s not found", incident_id)
        return {"action": "error", "reason": "incident not found"}

    ticket = {
        "id": inc["id"],
        "title": inc.get("title", ""),
        "description": inc.get("description", ""),
        "canonical_statement": _extract_canonical_statement(inc),
        "offering": _offering_of_inc(inc),
    }
    cards = retrieve_candidates(inc)
    cand_ids = [c["cluster_id"] for c in cards]

    if not cards:
        store.log_assignment(incident_id, [], {
            "action": "none_fit", "reason": "no active clusters exist"},
            PROMPT_VERSION, settings.llm_model)
        return {"action": "none_fit", "cluster_id": None, "reason": "no active clusters"}

    try:
        raw = call_llm(
            [{"role": "system", "content": ASSIGN_PROMPT},
             {"role": "user", "content": json.dumps(
                 {"ticket": ticket, "candidate_clusters": cards},
                 ensure_ascii=False)}],
            max_tokens=400, temperature=0.0,
        )
        verdict = json.loads(strip_json_fences(raw))
    except Exception as exc:  # noqa: BLE001 — log and keep the ticket unassigned
        _log.warning("Flow A LLM call failed for %s: %s", incident_id[:10], exc)
        store.log_assignment(incident_id, cand_ids,
                             {"action": "error", "reason": str(exc)[:200]},
                             PROMPT_VERSION, settings.llm_model)
        return {"action": "error", "reason": str(exc)[:200]}

    action = verdict.get("action")
    cluster_id = verdict.get("cluster_id") if action == "assign" else None
    confidence = verdict.get("confidence")
    reason = (verdict.get("reason") or "")[:300]

    # Safeguard: a hallucinated cluster id is never assigned to.
    if action == "assign" and cluster_id and cluster_id not in cand_ids:
        _log.warning("Flow A %s: cluster_id %s not among candidates — treating as none_fit",
                     incident_id[:10], cluster_id)
        verdict["_safeguard"] = "cluster_id not among candidates"
        store.log_assignment(incident_id, cand_ids, verdict, PROMPT_VERSION, settings.llm_model)
        return {"action": "none_fit", "cluster_id": None, "reason": reason}

    if action == "assign" and cluster_id and confidence in ("high", "medium"):
        store.add_cluster_member(cluster_id, incident_id,
                                 assigned_by="llm", confidence=confidence)
        store.log_assignment(incident_id, cand_ids, verdict, PROMPT_VERSION, settings.llm_model)
        _log.info("Flow A: %s -> cluster %s (%s): %s",
                  incident_id[:10], cluster_id[:10], confidence, reason)
        return {"action": "assign", "cluster_id": cluster_id,
                "confidence": confidence, "reason": reason}

    # assign+low / none_fit / malformed -> pool, still logged.
    if action not in ("assign", "none_fit"):
        verdict["_safeguard"] = f"unexpected action {action!r}"
    store.log_assignment(incident_id, cand_ids, verdict, PROMPT_VERSION, settings.llm_model)
    return {"action": "none_fit", "cluster_id": None,
            "confidence": confidence, "reason": reason}


# ── Flow B — pool sweep ───────────────────────────────────────────────────

def _sweep_batch(batch_ids: list[str]) -> list[dict] | None:
    """Group one batch via the LLM. Returns groups, or None when the batch is
    discarded (ID mismatch — validate_group safeguard: returned IDs must equal
    input IDs, else discard batch and log)."""
    incs = {i["id"]: i for i in store.list_incidents()}
    tickets = [
        {"id": iid,
         "title": (incs[iid].get("title") or "")[:_CARD_TITLE_MAX],
         "description": (incs[iid].get("description") or "")[:_TICKET_DESC_MAX]}
        for iid in batch_ids if iid in incs
    ]
    if not tickets:
        return []
    try:
        raw = call_llm(
            [{"role": "system", "content": SWEEP_PROMPT},
             {"role": "user", "content": json.dumps(
                 {"tickets": tickets}, ensure_ascii=False)}],
            max_tokens=2500, temperature=0.0,
        )
        result = json.loads(strip_json_fences(raw))
    except Exception as exc:  # noqa: BLE001
        _log.warning("Flow B batch failed (%d tickets): %s", len(batch_ids), exc)
        return None

    groups = result.get("groups", [])
    singletons = result.get("singletons", [])
    all_ids: list[str] = []
    for g in groups:
        all_ids.extend(g.get("member_ids", []))
    all_ids.extend(singletons)

    if set(all_ids) != set(batch_ids) or len(all_ids) != len(batch_ids):
        _log.warning(
            "Flow B ID mismatch — input=%d returned=%d. Discarding batch.",
            len(batch_ids), len(all_ids))
        store.log_assignment("__sweep_batch__", batch_ids, {
            "action": "discarded", "reason": "id mismatch",
            "returned_ids": all_ids}, PROMPT_VERSION, settings.llm_model)
        return None
    return groups


def sweep_pool(*, dry_run: bool = False) -> dict:
    """Flow B — give unassigned tickets second chances as clusters grow.

    1. Re-run Flow A per pooled ticket (new clusters may have appeared).
    2. Batch-group the remainder (<= SWEEP_BATCH_SIZE per LLM call); each
       returned group of >= 2 becomes a PROPOSAL (status='proposed' with
       members attached) — surfaced in the review UI, human-gated.
    """
    stats = {"pool_before": 0, "flow_a_assigned": 0, "batches": 0,
             "proposals_created": 0, "discarded_batches": 0,
             "pool_after": 0, "dry_run": dry_run}
    pool = store.unassigned_incident_ids()
    stats["pool_before"] = len(pool)
    if not pool:
        stats["pool_after"] = len(store.unassigned_incident_ids())
        return stats

    if not dry_run:
        for iid in pool:
            try:
                r = assign_incident(iid)
                if r.get("action") == "assign":
                    stats["flow_a_assigned"] += 1
            except Exception as exc:  # noqa: BLE001 — one bad ticket must not kill the sweep
                _log.warning("Flow A failed for %s during sweep: %s", iid[:10], exc)

    remaining = store.unassigned_incident_ids()
    if dry_run:
        stats["pool_after"] = len(remaining)
        return stats

    for batch in _chunks(remaining, SWEEP_BATCH_SIZE):
        stats["batches"] += 1
        groups = _sweep_batch(batch)
        if groups is None:
            stats["discarded_batches"] += 1
            continue
        batch_set = set(batch)
        for g in groups:
            members = [m for m in g.get("member_ids", []) if m in batch_set]
            if len(members) < PROPOSAL_MIN_SIZE:
                continue
            name_ar = (g.get("name_ar") or "").strip()[:120]
            description = (g.get("description") or "").strip()[:1000]
            cid = _cluster_id(name_ar or "مقترح جديد", members)
            if store.get_cluster(cid) is not None:
                continue  # id collision — a cluster for these members exists
            store.create_cluster(cid, name_ar or "مقترح جديد", description,
                                 status="proposed")
            for m in members:
                store.add_cluster_member(cid, m, assigned_by="llm", confidence="proposed")
            stats["proposals_created"] += 1
            _log.info("Flow B proposal %s — %d members: %s", cid, len(members), name_ar)

    stats["pool_after"] = len(store.unassigned_incident_ids())
    return stats


# ── Flow C — nightly audit ────────────────────────────────────────────────

def audit_cluster(cluster_id: str) -> dict:
    """LLM purity audit of one ACTIVE cluster. Removed members return to the
    pool (logged); a refined description is kept; name regenerated when the
    audit changes membership or description. Safeguards from validate_group:
    ID reconciliation + 60% pruning floor — a verdict that fails either is
    discarded whole (members untouched)."""
    cluster = store.get_cluster(cluster_id)
    if cluster is None:
        return {"cluster_id": cluster_id, "skipped": "not found"}
    if cluster["status"] != "active":
        return {"cluster_id": cluster_id, "skipped": "not active"}

    members = store.list_cluster_members(cluster_id)
    if len(members) < 2:
        return {"cluster_id": cluster_id, "skipped": "too few members"}

    tickets = [
        {"id": m["incident_id"], "title": m["title"],
         "description": (m["description"] or "")[:_TICKET_DESC_MAX]}
        for m in members[:AUDIT_MAX_MEMBERS]
    ]
    input_ids = {m["incident_id"] for m in members}
    prompt_tickets = tickets
    prompt_members = members

    try:
        raw = call_llm(
            [{"role": "system", "content": AUDIT_PROMPT.format(
                description=cluster.get("description") or cluster["name_ar"],
                tickets=json.dumps(prompt_tickets, ensure_ascii=False))}],
            max_tokens=2500, temperature=0.0,
        )
        verdict = json.loads(strip_json_fences(raw))
    except Exception as exc:  # noqa: BLE001 — audit must never break the loop
        _log.warning("Flow C audit failed for %s: %s", cluster_id[:10], exc)
        return {"cluster_id": cluster_id, "error": str(exc)[:200]}

    keep_ids = set(verdict.get("keep", []))
    remove_list = [r for r in verdict.get("remove", []) if isinstance(r, dict)]
    remove_ids = {r.get("id") for r in remove_list}

    # Safeguard 1 — ID reconciliation (validate_group).
    if keep_ids | remove_ids != input_ids:
        _log.warning("Flow C ID mismatch for %s — input=%d returned=%d. Discarding verdict.",
                     cluster_id[:10], len(input_ids), len(keep_ids | remove_ids))
        store.log_assignment("__audit__", [cluster_id], {
            "action": "discarded", "reason": "id mismatch",
            "keep": sorted(keep_ids), "remove": sorted(remove_ids)},
            PROMPT_VERSION, settings.llm_model)
        return {"cluster_id": cluster_id, "discarded": "id mismatch"}

    # Safeguard 2 — pruning floor (60%).
    if len(remove_ids) / len(input_ids) > AUDIT_PRUNE_FLOOR:
        _log.warning("Flow C wants to remove %.0f%% (>%.0f%%) of %s — discarding verdict.",
                     len(remove_ids) / len(input_ids) * 100,
                     AUDIT_PRUNE_FLOOR * 100, cluster_id[:10])
        store.log_assignment("__audit__", [cluster_id], {
            "action": "discarded", "reason": "pruning floor",
            "keep": sorted(keep_ids), "remove": sorted(remove_ids)},
            PROMPT_VERSION, settings.llm_model)
        return {"cluster_id": cluster_id, "discarded": "pruning floor"}

    removed = []
    for r in remove_list:
        rid = r.get("id")
        if not isinstance(rid, str) or rid not in input_ids:
            continue
        store.remove_cluster_member(cluster_id, rid)
        store.log_assignment(rid, [cluster_id], {
            "action": "audit_remove", "cluster_id": cluster_id,
            "reason": (r.get("reason") or "")[:300]},
            PROMPT_VERSION, settings.llm_model)
        removed.append(rid)
        _log.info("Flow C %s: removed %s — %s", cluster_id[:10], rid[:10],
                  (r.get("reason") or "")[:80])

    changed = bool(removed)
    refined = verdict.get("description")
    if isinstance(refined, str) and refined.strip() \
            and refined.strip() != (cluster.get("description") or ""):
        store.update_cluster_fields(cluster_id, description=refined.strip())
        changed = True

    name_regenerated = False
    if changed:
        try:
            regenerate_name(cluster_id)
            name_regenerated = True
        except Exception as exc:  # noqa: BLE001 — naming is best-effort
            _log.warning("Flow D rename failed for %s: %s", cluster_id[:10], exc)

    return {"cluster_id": cluster_id, "members_before": len(input_ids),
            "removed": removed, "description_refined": bool(refined and changed),
            "name_regenerated": name_regenerated}


# ── Flow D — naming (stored on the cluster row) ───────────────────────────

def regenerate_name(cluster_id: str) -> str:
    """LLM Arabic title from the CURRENT members; stored on the cluster row.
    Called only when the audit changes membership or description (stable
    clusters keep their name — no fingerprint cache needed)."""
    members = store.list_cluster_members(cluster_id)
    incidents = [{"id": m["incident_id"], "title": m["title"],
                  "description": m["description"]} for m in members]
    name = _arabic_cluster_name(incidents)
    store.update_cluster_fields(cluster_id, name_ar=name)
    return name


def _arabic_cluster_name(member_incidents: list[dict]) -> str:
    """Cache-free version of the legacy naming behavior (the fingerprint cache
    dies with the rebuild loop — names now live on the cluster row)."""
    name = member_incidents[0].get("title", "") if member_incidents else "Cluster"
    try:
        tickets = []
        for inc in member_incidents[:_AR_NAME_MAX_TICKETS]:
            t = (inc.get("title") or "").strip()[:_CARD_TITLE_MAX]
            d = (inc.get("description") or "").strip()[:_CARD_DESC_MAX]
            tickets.append(f"- {t}: {d}" if d else f"- {t}")
        if not tickets:
            tickets = ["- (no ticket text)"]
        raw = call_llm(
            [{"role": "user", "content": AR_NAME_PROMPT.format(
                tickets="\n".join(tickets))}],
            max_tokens=40, temperature=0.0,
        )
        label = (raw or "").strip().strip('"').strip("'").strip()
        label = label.splitlines()[0].strip() if label else ""
        if any("\u0600" <= ch <= "\u06FF" for ch in label) and len(label) <= 60:
            name = label
        else:
            _log.warning("Arabic title rejected (no Arabic script or too long): %r",
                         label[:40])
    except Exception as exc:  # noqa: BLE001 — naming is best-effort
        _log.warning("Arabic title generation failed: %s", exc)
    return name


# ── Report builder — /clusters + /api/reports read from tables ────────────

def _centroid_and_sims(member_embeddings: list[dict]) -> tuple[np.ndarray | None, dict[str, float]]:
    """Normalized centroid + per-member cosine-to-centroid for similarity_pct."""
    if len(member_embeddings) < 2:
        return None, {}
    vecs = np.stack([m["embedding"] for m in member_embeddings])
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    vecs = vecs / np.clip(norms, 1e-9, None)
    centroid = vecs.mean(axis=0)
    cn = np.linalg.norm(centroid)
    if cn < 1e-9:
        return None, {}
    centroid = centroid / cn
    sims = {m["incident_id"]: round(float(vecs[i] @ centroid), 3)
            for i, m in enumerate(member_embeddings)}
    return centroid, sims


def _cluster_report(cluster: dict, incidents_by_id: dict[str, dict]) -> dict | None:
    """Emit one cluster in the legacy dashboard response shape, from tables."""
    members = store.list_cluster_members(cluster["id"])
    m_incs = [incidents_by_id[m["incident_id"]] for m in members
              if m["incident_id"] in incidents_by_id]
    if not m_incs:
        return None
    worst_sev = _worst_severity(m_incs)
    top_sys, top_svc = _dominant_labels(m_incs)
    _, sims = _centroid_and_sims(store.cluster_member_embeddings(cluster["id"]))

    incidents = []
    for m in members:
        inc = incidents_by_id.get(m["incident_id"])
        if inc is None:
            continue
        incidents.append({
            "id": inc["id"],
            "title": inc.get("title", ""),
            "severity": _extract_severity(inc),
            "canonical_statement": _extract_canonical_statement(inc)[:200],
            "similarity_pct": round(sims.get(inc["id"], 1.0) * 100, 1)
                if len(m_incs) > 1 else 100.0,
            "description": inc.get("description", "")[:200],
            "classification": _parse_classification(inc),
            "affected_system": _class_field(inc, "affected_system"),
            "service": _class_field(inc, "service"),
            "incident_type": _class_field(inc, "incident_type"),
            "urgency": _class_field(inc, "urgency"),
            "category": _class_field(inc, "category"),
            "assign_group": inc.get("assign_group", ""),
            "assignee": inc.get("assignee", ""),
            "priority": inc.get("priority", "medium"),
            "status": inc.get("status", "active"),
            "created_at": inc.get("created_at", ""),
        })
    name = cluster["name_ar"] or cluster.get("name_en") or "Cluster"
    return {
        "cluster_id": cluster["id"],
        "name": name,
        "description": cluster.get("description") or name,
        "affected_system": top_sys,
        "affected_service": top_svc,
        "worst_severity": worst_sev,
        "count": len(incidents),
        "summary": f"{len(incidents)} tickets — {name}",
        "pruned": [],
        "incidents": incidents,
    }


def build_clusters(period: str = "daily") -> dict:
    """Public report: ACTIVE clusters read from the tables + subsystem rollup
    (kept — it answers a different question). Same response shape as the
    legacy rebuild so the dashboard notices nothing except more clusters."""
    subsystem_summary = _subsystem_rollup(store.list_incidents(status="active"))
    incidents_by_id = {i["id"]: i for i in store.list_incidents()}
    clusters = []
    for c in store.list_clusters(status="active"):
        try:
            rep = _cluster_report(c, incidents_by_id)
        except Exception as exc:  # noqa: BLE001 — one bad cluster must not kill the report
            _log.error("Cluster report failed for %s: %s", c["id"][:10], exc)
            rep = None
        if rep is not None:
            clusters.append(rep)
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return {
        "total_incidents": len(incidents_by_id),
        "clusters": clusters,
        "subsystem_summary": subsystem_summary,
    }


# ── Background workers ────────────────────────────────────────────────────

def start_sweep_worker(interval: float | None = None) -> threading.Thread:
    """Daemon: Flow B pool sweep every interval (default REPOOL_INTERVAL, 900s)
    + Flow C nightly audit of all active clusters (CLUSTER_AUDIT_INTERVAL_S)."""
    interval = interval if interval is not None \
        else float(getattr(settings, "repool_interval_seconds", 900))
    audit_interval = float(getattr(settings, "cluster_audit_interval_s", 86400))

    def _loop() -> None:
        _log.info("Cluster sweep worker started (interval=%ss, audit=%ss)",
                  interval, audit_interval)
        # last_audit starts NOW, not 0 — otherwise the first tick sees
        # now-0 >= audit_interval and fires a full audit at startup (the
        # audit is NIGHTLY; a boot audit doubles LLM traffic for nothing).
        last_audit = time.time()
        while True:
            try:
                stats = sweep_pool()
                if stats.get("flow_a_assigned") or stats.get("proposals_created"):
                    _log.info("Sweep: %s", stats)
            except Exception as exc:  # noqa: BLE001
                _log.error("Sweep failed: %s", exc)
            now = time.time()
            if now - last_audit >= audit_interval:
                try:
                    for c in store.list_clusters(status="active"):
                        audit_cluster(c["id"])
                    last_audit = time.time()
                except Exception as exc:  # noqa: BLE001
                    _log.error("Audit pass failed: %s", exc)
            time.sleep(interval)

    t = threading.Thread(target=_loop, name="persistent-clusters", daemon=True)
    t.start()
    return t


def assign_in_background(incident_id: str) -> None:
    """Fire-and-forget Flow A after a ticket lands — slow inference must not
    delay the classify response (spec: Flow A lives in the ingest path's
    background task)."""
    try:
        t = threading.Thread(target=assign_incident, args=(incident_id,),
                             name=f"flow-a-{incident_id[:8]}", daemon=True)
        t.start()
    except Exception as exc:  # noqa: BLE001
        _log.warning("Could not start Flow A for %s: %s", incident_id[:10], exc)
