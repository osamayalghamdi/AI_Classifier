#!/usr/bin/env python3
"""Engine run — offering pools -> LLM-verified sub-offering PROPOSALS.

STOPS at proposals. Minting (proposals -> ACTIVE sub-offerings) happens
ONLY through the human review gate (dashboard review.html / POST
/proposals/{id}/decision). This is deliberate: dyncat's auto-mint produced
7 wrong merges + a 30-ticket mega-cluster, so proposals never
auto-become sub-offerings.

Usage (working LLM env required — OpenRouter or company endpoint):
    export LLM_MODEL=... LLM_API_KEY=... LLM_API_BASE=...
    export PG_HOST=localhost PG_PORT=5432 PG_USER=aiuser PG_PASSWORD=... PG_DATABASE=ai_incidents
    python scripts/run_engine.py
"""
import sys
from collections import Counter

sys.path.insert(0, ".")

from ai_classification.shared.store import store
from ai_classification.services.match.suboffering import OFFERING_000, offering_of
from ai_classification.services.cluster.suboffering_cluster import run_all_pools
from ai_classification.services.cluster.verifier import Verifier

store.setup()

incidents = store.list_incidents()
print(f"incidents: {len(incidents)}")

pools: dict[str, list[dict]] = {}
for inc in incidents:
    svc = (inc.get("classification_dict") or {}).get("service", "")
    pools.setdefault(offering_of(svc) or OFFERING_000, []).append(inc)
print("pools:", dict(sorted(((k, len(v)) for k, v in pools.items()), key=lambda t: -t[1])))

run_all_pools(pools, Verifier())

props = store.list_proposals(status="pending")
print(f"\npending proposals: {len(props)} — REVIEW REQUIRED before any minting")
print("  -> dashboard: frontend/dashboard/review.html")
print("  -> API:       GET /proposals · POST /proposals/{id}/decision (approve|reject|merge)")
labels = Counter(p["proposed_label"][:45] for p in props)
for lab, n in labels.most_common():
    print(f"    - {lab}  (x{n})")
