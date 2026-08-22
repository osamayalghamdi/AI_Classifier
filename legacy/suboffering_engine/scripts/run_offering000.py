#!/usr/bin/env python3
"""OFFERING-000 batch job (W3) — cluster the cross-domain pool of tickets the
cascade could NOT assign an offering (service without '.' first segment).

Reuses the W2 engine end-to-end (suboffering_cluster.run_pool) — this script
only selects the pool and prints the report + per-edge audit for the human gate.

Usage:
  python scripts/run_offering000.py [--shuffle-seed N] [--cache PATH]
Env: PG_* (default config), LLM_MODEL/LLM_API_KEY from repo .env (load_dotenv
is CWD-relative — export them or run from the repo root).
"""
import argparse
import json
import random
import sys

sys.path.insert(0, ".")

from legacy.suboffering_engine.store_suboffering import store
from ai_classification.services.match.suboffering import offering_of, OFFERING_000
from legacy.suboffering_engine.suboffering_cluster import run_pool, OFFERING000_MAX_MEMBERS
from legacy.suboffering_engine.verifier import Verifier


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shuffle-seed", type=int, default=None,
                    help="shuffle pool order with this seed before clustering (F3)")
    ap.add_argument("--cache", default=None, help="verifier cache path override")
    args = ap.parse_args()

    store.setup()
    if not store.ready:
        print("store not ready", file=sys.stderr)
        return 1

    incidents = store.list_incidents()
    pool = [i for i in incidents
            if offering_of((i.get("classification_dict") or {}).get("service", "")) is None]
    pool.sort(key=lambda i: i["id"])

    print(f"OFFERING-000 pool: {len(pool)} tickets of {len(incidents)} total")
    for inc in pool:
        svc = (inc.get("classification_dict") or {}).get("service", "?")
        svc = (inc.get("classification_dict") or {}).get("service", "?")
        print(f"  {inc['id']}  svc='{svc}'  {inc['title'][:60]}")

    if args.shuffle_seed is not None:
        random.Random(args.shuffle_seed).shuffle(pool)

    verifier = Verifier(cache_path=args.cache) if args.cache else Verifier()
    report = run_pool(OFFERING_000, pool, verifier, max_proposal_members=OFFERING000_MAX_MEMBERS)

    print("\n=== RUN REPORT ===")
    for k in ("n_pool", "candidates", "auto_accepted", "llm_pairs", "yes_edges",
              "unresolved", "oversize_before", "oversize_after", "edges_dropped",
              "tickets_covered", "timing_s"):
        print(f"  {k}: {report[k]}")
    if report["drift"]:
        print("  drift:", json.dumps(report["drift"], ensure_ascii=False))
    print("  needs_review_clusters:",
          json.dumps(report["needs_review_clusters"], ensure_ascii=False))

    print("\n=== PROPOSALS (F1 audit material) ===")
    by_id = {i["id"]: i for i in pool}
    for p in report["proposals"]:
        members = p["member_ids"]
        print(f"\nPROPOSAL {p['id']}  offering={p['offering_id']}  n={len(members)}  "
              f"mean_sim={p['mean_sim']}  label='{p.get('proposed_label', '')}'")
        print("  flags:", json.dumps(p["purity_flags"], ensure_ascii=False))
        for mid in members:
            inc = by_id.get(mid, {})
            svc = (inc.get("classification_dict") or {}).get("service", "?")
            print(f"    {mid}  svc={svc}  | {inc.get('title', '')[:60]} | {inc.get('description', '')[:100]}")
        for edge, reason in (p.get("verifier_reasons") or {}).items():
            a, b = edge.split("~")
            ia = by_id.get(next((m for m in members if m.startswith(a)), ""), {})
            ib = by_id.get(next((m for m in members if m.startswith(b)), ""), {})
            print(f"    EDGE {a}~{b}  sim?  reason: {reason}")
            print(f"      A: {ia.get('title', '')[:50]} | {ia.get('description', '')[:80]}")
            print(f"      B: {ib.get('title', '')[:50]} | {ib.get('description', '')[:80]}")

    print("\n=== VERIFIER USAGE ===")
    print(json.dumps(verifier.usage, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
