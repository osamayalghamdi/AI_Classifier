"""Regenerate cluster Arabic names after a reclassify sweep.

Cluster membership is embedding-driven (ticket text only) and does NOT move
when classifications change. But the cluster's `name_ar` is an LLM title
STORED on the cluster row, generated from members at creation time — it does
not regenerate when member classifications change. After a reclassify sweep
fixes failed classifications, clusters whose dominant system/service changed
keep a stale/mislabelled name. This script fixes exactly that:

    # 1) BEFORE the reclassify sweep — snapshot current cluster labels:
    python -m scripts.regen_cluster_names --snapshot /tmp/cluster_labels.json

    # 2) run scripts.reclassify_v3 --only-failed ...

    # 3) AFTER — dry-run the diff, then regenerate names for changed clusters:
    python -m scripts.regen_cluster_names --diff /tmp/cluster_labels.json
    python -m scripts.regen_cluster_names --regen /tmp/cluster_labels.json \
        [--sleep 1] [--dry-run]

Only clusters whose dominant (system, service) tuple changed are touched.
Reuses persistent.regenerate_name (the existing Arabic-title generator with
its guards: Arabic script required, ≤60 chars, ≤_AR_NAME_MAX_WORDS words).

Usage (inside the API container, which has LLM keys + DB access):
    docker exec ai_classifier-api-1 python -m scripts.regen_cluster_names --snapshot out.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

_log = logging.getLogger(__name__)


def _dominant(members_incs: list[dict]) -> tuple[str, str]:
    """Most common (affected_system, service) among member incidents —
    same rule as the live report (_dominant_labels in grouping.py)."""
    from collections import defaultdict
    systems: dict[str, int] = defaultdict(int)
    services: dict[str, int] = defaultdict(int)
    for inc in members_incs:
        data = inc.get("classification_dict") or {}
        if data:
            systems[str(data.get("affected_system", "Unknown"))] += 1
            services[str(data.get("service", "Unknown"))] += 1
    top_sys = max(systems, key=lambda k: systems[k]) if systems else "Unknown"
    top_svc = max(services, key=lambda k: services[k]) if services else "Unknown"
    return top_sys, top_svc


def _members_for(cluster_id: str, incidents_by_id: dict) -> list[dict]:
    from ai_classification.shared.store import store
    members = store.list_cluster_members(cluster_id)
    return [incidents_by_id[m["incident_id"]] for m in members
            if m["incident_id"] in incidents_by_id]


def snapshot(path: str) -> None:
    """Write cluster_id -> {name_ar, sys, svc, member_count} to JSON."""
    from ai_classification.shared.store import store
    store.setup()
    incidents_by_id = {i["id"]: i for i in store.list_incidents()}
    out = {}
    for c in store.list_clusters(status="active"):
        sys_, svc = _dominant(_members_for(c["id"], incidents_by_id))
        out[c["id"]] = {
            "name_ar": c["name_ar"],
            "sys": sys_, "svc": svc,
            "member_count": len(_members_for(c["id"], incidents_by_id)),
        }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"snapshot: {len(out)} clusters -> {path}")


def _changed(path: str) -> list[tuple[str, str, str, str, str]]:
    """Return [(cluster_id, old_sys, old_svc, new_sys, new_svc)] for clusters
    whose dominant labels changed since the snapshot."""
    from ai_classification.shared.store import store
    store.setup()
    with open(path, encoding="utf-8") as fh:
        before = json.load(fh)
    incidents_by_id = {i["id"]: i for i in store.list_incidents()}
    changed = []
    for cluster_id, prev in before.items():
        c = store.get_cluster(cluster_id)
        if c is None:
            continue
        new_sys, new_svc = _dominant(_members_for(cluster_id, incidents_by_id))
        if (prev["sys"], prev["svc"]) != (new_sys, new_svc):
            changed.append((cluster_id, prev["sys"], prev["svc"], new_sys, new_svc))
    return changed


def diff(path: str) -> None:
    changed = _changed(path)
    if not changed:
        print("diff: no clusters changed dominant labels — nothing to do.")
        return
    print(f"diff: {len(changed)} cluster(s) changed dominant labels:")
    for cid, osys, osvc, nsys, nsvc in changed:
        print(f"  {cid[:12]}  {osys} / {osvc}  ->  {nsys} / {nsvc}")


def regen(path: str, *, sleep_s: float, dry_run: bool) -> None:
    from ai_classification.services.cluster.persistent import regenerate_name
    changed = _changed(path)
    if not changed:
        print("regen: no clusters changed dominant labels — nothing to do.")
        return
    print(f"regen: regenerating name_ar for {len(changed)} cluster(s)...")
    for i, (cid, osys, osvc, nsys, nsvc) in enumerate(changed, 1):
        print(f"  [{i}/{len(changed)}] {cid[:12]}  {osys}/{osvc} -> {nsys}/{nsvc}")
        if dry_run:
            continue
        try:
            new_name = regenerate_name(cid)
            print(f"      name_ar -> {new_name!r}")
        except Exception as exc:  # noqa: BLE001 — naming is best-effort
            _log.warning("name regeneration failed for %s: %s", cid[:12], exc)
        if sleep_s:
            time.sleep(sleep_s)
    print("regen: done.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", metavar="FILE",
                        help="write current cluster labels to FILE")
    parser.add_argument("--diff", metavar="FILE",
                        help="show clusters whose dominant labels changed since snapshot FILE")
    parser.add_argument("--regen", metavar="FILE",
                        help="regenerate name_ar for clusters changed since snapshot FILE")
    parser.add_argument("--sleep", type=float, default=1.0,
                        help="seconds between LLM naming calls (default 1.0)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would change without calling the LLM")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    actions = sum(bool(getattr(args, a)) for a in ("snapshot", "diff", "regen"))
    if actions != 1:
        print("exactly one of --snapshot / --diff / --regen required", file=sys.stderr)
        return 2
    if args.snapshot:
        snapshot(args.snapshot)
    elif args.diff:
        diff(args.diff)
    else:
        regen(args.regen, sleep_s=args.sleep, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
