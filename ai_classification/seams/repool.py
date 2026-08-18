"""Job 2 — REPOOL (continuous, every 15 min): give unassigned tickets
second chances as clusters grow. NEVER re-classifies — re-matches only.

1. Unassigned = in the unmatched_pool (have an offering, no sub-offering).
2. Match each against ACTIVE sub-offerings created since it arrived —
   matched -> becomes an exemplar of that cluster (moved).
3. Still unmatched -> group by offering; if enough cluster together
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


def repool_once(*, dry_run: bool = False) -> dict:
    from ..core.store import store
    from ..core.suboffering import OFFERING_000, feed_incident, offering_of
    from ..core.suboffering_cluster import run_all_pools
    from ..core.verifier import Verifier

    stats = {"pool_before": 0, "matched": 0, "clustered_pools": 0,
             "proposals_created": 0, "pool_after": 0, "dry_run": dry_run}

    pool = store.pool_list()
    stats["pool_before"] = len(pool)
    if not pool:
        return stats

    incidents = {i["id"]: i for i in store.list_incidents()}
    leftover: dict[str, list[dict]] = defaultdict(list)

    for entry in pool:
        inc = incidents.get(entry["incident_id"])
        if inc is None:
            continue
        if dry_run:
            svc = (inc.get("classification_dict") or {}).get("service", "")
            leftover[offering_of(svc) or OFFERING_000].append(inc)
            continue
        # Step 1 — match against ACTIVE sub-offerings (feed_incident routes).
        routed = feed_incident(inc)
        if routed.get("matched"):
            svc = (inc.get("classification_dict") or {}).get("service", "")
            store.pool_remove(offering_of(svc) or OFFERING_000, inc["id"])
            stats["matched"] += 1
            _log.info("Repool: %s -> sub-offering %s (sim=%s)", inc["id"][:10],
                      routed.get("sub_offering_id", "?")[:10], routed.get("sim"))
        else:
            leftover[routed.get("offering") or OFFERING_000].append(inc)

    # Step 2 — self-cluster leftovers per offering -> PROPOSALS (gated).
    # Dry-run stops here: count what WOULD cluster, write nothing.
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
    return stats


def start_repool_worker(interval: float | None = None) -> threading.Thread:
    """Background daemon: periodic repool sweep (default: settings.repool_interval_seconds, 900s)."""
    from ..config import settings

    interval = interval if interval is not None else float(getattr(settings, "repool_interval_seconds", 900))

    def _loop() -> None:
        _log.info("Repool worker started (interval=%ss)", interval)
        while True:
            try:
                stats = repool_once()
                if stats.get("matched") or stats.get("proposals_created"):
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

    from ..core.store import store

    store.setup()
    stats = repool_once(dry_run="--dry-run" in sys.argv)
    print(f"repool: {stats}")
    print(f"finished at {datetime.now(timezone.utc).isoformat()}")
