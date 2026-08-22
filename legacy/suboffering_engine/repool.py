"""Job 2 — REPOOL (continuous, every 15 min): give unassigned tickets
second chances as clusters grow. NEVER re-classifies — re-matches only.

1. Unassigned = in the unmatched_pool (have an offering, no sub-offering).
2. Phase 1 — match each against ACTIVE sub-offerings of its OWN offering
   (feed_incident, threshold 0.60); matched -> becomes an exemplar of that
   cluster (moved).
3. Phase 2 (cross-offering) — survivors are matched against ALL active
   sub-offerings, not just their own offering's: a real problem can span
   two offerings (e.g. "payments" and "billing" both see "system timeout").
   Stricter threshold (0.75) so the wider net doesn't become a grab-bag.
4. Still unmatched -> group by offering; if enough cluster together
   (engine: candidates + LLM verifier) -> a PROPOSAL is created.
   Proposals NEVER auto-mint — the human review gate decides
   (frontend/dashboard/review.html).
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

# Cross-offering matches must clear a much higher bar than within-offering:
# phase 1 already had the ticket's own offering at 0.60; a different
# offering only gets the ticket when the match is near-certain.
CROSS_OFFERING_THRESHOLD = 0.75


def repool_once(*, dry_run: bool = False) -> dict:
    from legacy.suboffering_engine.store_suboffering import store
    from ai_classification.services.match.suboffering import (
        OFFERING_000,
        embed_pure,
        offering_of,
    )
    from legacy.suboffering_engine.suboffering import feed_incident, match_against_exemplars
    from legacy.suboffering_engine.suboffering_cluster import run_all_pools
    from legacy.suboffering_engine.verifier import Verifier

    stats = {"pool_before": 0, "matched": 0, "phase1_moved": 0, "phase2_moved": 0,
             "remaining": 0, "clustered_pools": 0, "proposals_created": 0,
             "pool_after": 0, "dry_run": dry_run}

    pool = store.pool_list()
    stats["pool_before"] = len(pool)
    if not pool:
        return stats

    incidents = {i["id"]: i for i in store.list_incidents()}
    pending: list[dict] = []          # survived phase 1 -> phase 2 candidates
    leftover: dict[str, list[dict]] = defaultdict(list)

    for entry in pool:
        inc = incidents.get(entry["incident_id"])
        if inc is None:
            continue
        if dry_run:
            pending.append(inc)  # counting only — no embedding, no writes
            continue
        # Phase 1 — match within the ticket's OWN offering.
        routed = feed_incident(inc)
        if routed.get("matched"):
            svc = (inc.get("classification_dict") or {}).get("service", "")
            store.pool_remove(offering_of(svc) or OFFERING_000, inc["id"])
            stats["matched"] += 1
            stats["phase1_moved"] += 1
            _log.info("Repool phase1: %s -> sub-offering %s (sim=%s)", inc["id"][:10],
                      routed.get("sub_offering_id", "?")[:10], routed.get("sim"))
        else:
            pending.append(inc)

    # Phase 2 — cross-offering matching (stricter threshold). Skipped in
    # dry-run: same contract as the rest of the sweep (count only, no LLM,
    # no embedding, nothing written).
    if pending and not dry_run:
        all_subs = store.list_sub_offerings(status="active")
        exemplars_by_sub = {s["id"]: store.list_exemplars(s["id"]) for s in all_subs}
        for inc in pending:
            svc = (inc.get("classification_dict") or {}).get("service", "")
            offering = offering_of(svc) or OFFERING_000
            title = inc.get("title", "") or ""
            description = inc.get("description", "") or ""
            emb = embed_pure(title, description)
            if emb is None:
                leftover[offering].append(inc)
                continue
            best_id, best_sim = None, -1.0
            for sub in all_subs:
                exs = exemplars_by_sub.get(sub["id"]) or []
                if not exs:
                    continue
                sid, sim = match_against_exemplars(emb, exs)
                if sid is not None and sim > best_sim:
                    best_id, best_sim = sid, sim
            if best_id is not None and best_sim >= CROSS_OFFERING_THRESHOLD:
                store.add_exemplar(best_id, inc["id"], title, description, emb)
                store.pool_remove(offering, inc["id"])
                stats["phase2_moved"] += 1
                _log.info("Repool phase2 (cross-offering): %s -> sub-offering %s (sim=%s)",
                          inc["id"][:10], best_id[:10], round(best_sim, 4))
            else:
                leftover[offering].append(inc)
    else:
        for inc in pending:
            svc = (inc.get("classification_dict") or {}).get("service", "")
            leftover[offering_of(svc) or OFFERING_000].append(inc)
    stats["remaining"] = len(pending) - stats["phase2_moved"]

    # Step 3 — self-cluster leftovers per offering -> PROPOSALS (gated).
    pools = {o: v for o, v in leftover.items() if len(v) >= 3}
    if pools and not dry_run:
        stats["clustered_pools"] = len(pools)
        before = len(store.list_proposals(status="pending"))
        run_all_pools(pools, Verifier())
        after = len(store.list_proposals(status="pending"))
        stats["proposals_created"] = max(0, after - before)
    elif pools:
        stats["clustered_pools"] = len(pools)
        stats["proposals_created"] = -1  # dry-run: would cluster N pools, no proposals written

    stats["pool_after"] = len(store.pool_list())
    if not dry_run:
        _log.info("repool sweep: phase1_moved=%d phase2_moved=%d remaining=%d",
                  stats["phase1_moved"], stats["phase2_moved"], stats["remaining"])
    return stats


def start_repool_worker(interval: float | None = None) -> threading.Thread:
    """Background daemon: periodic repool sweep (default: settings.repool_interval_seconds, 900s)."""
    from ai_classification.shared.config import settings

    interval = interval if interval is not None else float(getattr(settings, "repool_interval_seconds", 900))

    def _loop() -> None:
        _log.info("Repool worker started (interval=%ss)", interval)
        while True:
            try:
                stats = repool_once()
                if stats.get("matched") or stats.get("phase2_moved") or stats.get("proposals_created"):
                    _log.info("Repool sweep: %s", stats)
            except Exception as exc:  # noqa: BLE001
                _log.error("Repool sweep failed: %s", exc)
            time.sleep(interval)

    t = threading.Thread(target=_loop, name="seams-repool", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO)

    from ai_classification.shared.store import store

    store.setup()
    stats = repool_once(dry_run="--dry-run" in sys.argv)
    print(f"repool: {stats}")
    print(f"finished at {datetime.now(timezone.utc).isoformat()}")
