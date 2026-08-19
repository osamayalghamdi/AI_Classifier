"""Seed the v2 persistent cluster tables from the current (legacy) live state.

Migration step 1 of the LLM-first persistent clustering handoff:
  1. The running API still serves the legacy snapshot — save it first:
       curl -s http://localhost:8000/api/reports/daily > /tmp/cluster_snapshot.json
  2. Run this script against the LIVE database (PG_DATABASE=ai_incidents):
       .venv/bin/python scripts/seed_persistent_clusters.py /tmp/cluster_snapshot.json
     - creates the new tables (store.setup() — IF NOT EXISTS, non-destructive)
     - inserts each legacy cluster as an ACTIVE cluster row (id cl_<sha256>),
       members with assigned_by='seed'
     - idempotent: clusters that already exist are skipped, never duplicated

Expected outcome (verified in the migration report): 9 seeded clusters,
76 members, 23 incidents left in the derived unassigned pool.
"""

import json
import sys

from ai_classification.shared.store import store
from ai_classification.services.cluster.persistent import _cluster_id


def seed_from_snapshot(snapshot_path: str) -> dict:
    with open(snapshot_path, encoding="utf-8") as fh:
        snapshot = json.load(fh)

    store.setup()
    if not store.ready:
        raise SystemExit("store not ready — cannot seed")

    stats = {"clusters_seen": 0, "clusters_created": 0, "members_seeded": 0,
             "skipped_existing": 0, "total_incidents": snapshot.get("total_incidents", 0)}

    for c in snapshot.get("clusters", []):
        stats["clusters_seen"] += 1
        name = c.get("name") or "Cluster"
        description = c.get("description") or name
        member_ids = [i.get("id") for i in c.get("incidents", []) if i.get("id")]
        cid = _cluster_id(name, member_ids)

        if store.get_cluster(cid) is not None:
            stats["skipped_existing"] += 1
            continue

        store.create_cluster(cid, name, description, status="active")
        for mid in member_ids:
            store.add_cluster_member(cid, mid, assigned_by="seed", confidence="seed")
            stats["members_seeded"] += 1
        stats["clusters_created"] += 1
        print(f"seeded {cid} — {name} ({len(member_ids)} members)")

    stats["unassigned_pool"] = len(store.unassigned_incident_ids())
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return stats


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: seed_persistent_clusters.py <snapshot.json>")
    seed_from_snapshot(sys.argv[1])
