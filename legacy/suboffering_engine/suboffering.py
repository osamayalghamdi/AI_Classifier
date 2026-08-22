"""Dormant sub-offering matcher + unmatched-pool feed (QUARANTINED).

Superseded by the v2 persistent-clustering path (services/cluster/
persistent.py, wired in lifespan via start_sweep_worker). Kept for
resurrection: the offering helpers (offering_of / embed_pure /
OFFERING_000) still live in ai_classification/services/match/suboffering.py
because live clustering uses them; the ENGINE below is dead code.

Original docstring (kept verbatim for fidelity):
  Offering = FIRST segment of the stored service string (per STATUS.md: pool
  key is the service-level name; sub-offerings are clusters proposed WITHIN
  it). Flow (after offering assignment):
    1. embed pure ticket text (bge-m3, title + "\\n" + description, normalized)
    2. match against ACTIVE sub-offering exemplars of that offering (cosine
       >= 0.60 STARTING threshold — tuning evidence pending, see report)
    3. matched  -> attach ticket as a new exemplar of the sub-offering
       unmatched -> add to the offering's unmatched_pool (batch fodder)
  Tickets with no offering (OFFERING-000) are W3's scope — skipped here.
"""
import json
import logging

import numpy as np

from ai_classification.shared.store import store
from ai_classification.services.match.suboffering import OFFERING_000, embed_pure, offering_of

_log = logging.getLogger(__name__)

MATCH_THRESHOLD = 0.60  # starting value — see report for tuning evidence


def _vec_from_text(text: str) -> np.ndarray | None:
    """Parse 'embedding::text' ([1.0, 2.0, ...]) back to a numpy array."""
    try:
        return np.asarray(json.loads(text), dtype=np.float32)
    except Exception:
        return None


def match_against_exemplars(embedding: np.ndarray, exemplar_rows: list[dict]) -> tuple[str | None, float]:
    """Best ACTIVE exemplar match. Returns (sub_offering_id, cosine)."""
    best_id, best_sim = None, -1.0
    for ex in exemplar_rows:
        vec = _vec_from_text(ex["embedding"]) if isinstance(ex.get("embedding"), str) else ex.get("embedding")
        if vec is None or vec.shape != embedding.shape:
            continue
        sim = float(embedding @ vec)
        if sim > best_sim:
            best_sim, best_id = sim, ex["sub_offering_id"]
    return best_id, best_sim


def feed_incident(incident: dict, match_threshold: float = MATCH_THRESHOLD) -> dict:
    """Route one classified incident into the sub-offering engine (side channel —
    never modifies the incident's classification). Returns the routing result."""
    c = incident.get("classification_dict", {})
    offering = offering_of(c.get("service", ""))
    if offering is None:
        return {"offering": OFFERING_000, "routed": "offering-000", "matched": False}

    emb = embed_pure(incident.get("title", ""), incident.get("description", ""))
    if emb is None:
        store.pool_add(offering, incident["id"])
        return {"offering": offering, "routed": "pool", "matched": False}

    # ACTIVE sub-offerings of this offering + their exemplars
    subs = store.list_sub_offerings(offering_id=offering, status="active")
    exemplars = []
    for sub in subs:
        exemplars.extend(store.list_exemplars(sub["id"]))

    if exemplars:
        sub_id, sim = match_against_exemplars(emb, exemplars)
        if sub_id is not None and sim >= match_threshold:
            store.add_exemplar(sub_id, incident["id"], incident.get("title", ""),
                               incident.get("description", ""), emb)
            return {"offering": offering, "routed": "matched", "matched": True,
                    "sub_offering_id": sub_id, "sim": round(sim, 4)}

    store.pool_add(offering, incident["id"])
    return {"offering": offering, "routed": "pool", "matched": False}


def backfill_all() -> dict:
    """Route all current incidents through the matcher (one-time backfill)."""
    incidents = store.list_incidents()
    stats = {"matched": 0, "pool": 0, "offering-000": 0}
    for inc in incidents:
        r = feed_incident(inc)
        stats[r["routed"]] = stats.get(r["routed"], 0) + 1
        if r.get("matched"):
            stats["matched"] += 1
    _log.info("backfill: %s", stats)
    return stats
