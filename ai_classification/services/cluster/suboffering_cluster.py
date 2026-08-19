"""Batch clustering job — per offering pool: candidates -> strict v3 verification
-> union-find -> oversize guard -> drift -> purity floor -> PROPOSALS (never mints).

Frozen params (STATUS.md): floor 0.40, top_n 10, tie-break (sim DESC, id ASC),
auto-accept >=0.90 cap-exempt, purity floor mean_sim<0.45 OR >6 FM codes ->
NEEDS_REVIEW (excluded from proposals), oversize guard >20 re-verify weakest 25%,
drift max_tokens=2000 chunk >25 retry-once-then-FLAG, cache key includes
prompt_version. Cross-offering edges are impossible by construction: pools are
disjoint, candidates are generated inside one pool only.
"""
import json
import logging
import time
from collections import Counter

import numpy as np

from ai_classification.shared.store import store
from ai_classification.services.match.suboffering import embed_pure, offering_of, OFFERING_000
from ai_classification.services.cluster.verifier import Verifier

_log = logging.getLogger(__name__)

FLOOR = 0.40
TOP_N = 10
AUTO_ACCEPT = 0.90
OVERSIZE_THRESHOLD = 20
MIN_CLUSTER_SIZE = 3
PURITY_MIN_SIM = 0.45
PURITY_MAX_SERVICES = 6
DRIFT_CHUNK = 25
OFFERING000_MAX_MEMBERS = 10  # W3 guard: cross-domain pool proposals cap


class UnionFind:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _tiebreak_key(i: int, j: int, sim: float) -> tuple:
    """(sim DESC, id ASC) — deterministic candidate ordering."""
    return (-sim, i, j)


def generate_candidates(incidents, sim, floor=FLOOR, top_n=TOP_N):
    """Top-N neighbors per ticket above floor. Unordered pairs, sim DESC + id ASC."""
    n = len(incidents)
    seen = set()
    cands = []
    for i in range(n):
        order = np.argsort(-sim[i])[:top_n]
        for j in order:
            s = float(sim[i, j])
            if s < floor or s < 0:
                continue
            key = (min(i, j), max(i, j))
            if key in seen:
                continue
            seen.add(key)
            cands.append((key[0], key[1], s))
    cands.sort(key=lambda t: _tiebreak_key(t[0], t[1], t[2]))
    return cands


def _drift_one(verifier, incidents, members):
    """Drift review chunk, max_tokens=2000, retry-once-then-FLAG."""
    lines = [f"- {incidents[i]['id']}: {incidents[i]['title']} | {incidents[i]['description'][:120]}"
             for i in members]
    user = ("These tickets were clustered as ONE problem by similarity. "
            "List any member that is NOT the same underlying problem as the rest.\n\n"
            + "\n".join(lines) + "\n\n"
            'Return ONLY JSON: {"remove": ["<id>", ...]} — empty array if all belong.')
    for _attempt in range(2):
        content = verifier._call([
            {"role": "system", "content": "You audit an incident cluster for outliers. Return ONLY valid JSON."},
            {"role": "user", "content": user},
        ], max_tokens=2000)
        try:
            data = json.loads(content.strip().strip("`").lstrip("json"))
            if isinstance(data, dict) and "remove" in data:
                return "REVIEWED", set(data["remove"])
        except Exception as e:
            _log.warning("drift parse failed: %s", e)
    return "FLAGGED", set()


def drift_review(verifier, incidents, members):
    remove = set()
    idx = list(members)
    if len(idx) > DRIFT_CHUNK:
        outcome = "REVIEWED"
        for start in range(0, len(idx), DRIFT_CHUNK):
            oc, rm = _drift_one(verifier, incidents, idx[start:start + DRIFT_CHUNK])
            if oc == "FLAGGED":
                outcome = "FLAGGED"
            remove |= rm
        return outcome, remove
    return _drift_one(verifier, incidents, idx)


def label_proposals(verifier, incidents, proposals):
    """One LLM call per pool: name each proposal cluster (display only)."""
    if not proposals:
        return {}
    lines = []
    for n, p in enumerate(proposals):
        first = incidents[p["members"][0]]
        lines.append(f"Cluster {n}: ids={[incidents[i]['id'] for i in p['members']]} "
                     f"| first: {first['title']} | {first['description'][:100]}")
    user = ("Each cluster is tickets of the SAME underlying problem. Give each a short "
            "problem name (max 8 words).\n\n" + "\n".join(lines) + "\n\n"
            'Return ONLY JSON: {"0": "name", "1": "name", ...}')
    content = verifier._call([
        {"role": "system", "content": "You name incident clusters. Return ONLY valid JSON."},
        {"role": "user", "content": user},
    ], max_tokens=2000)
    try:
        data = json.loads(content.strip().strip("`").lstrip("json"))
        return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        _log.warning("label parse failed: %s", e)
        return {}


def compute_member_flags(services: list[str]) -> dict:
    """Member-level purity rule: member service not in the cluster's top-2
    service values -> member NEEDS_REVIEW. Deterministic: count DESC, code
    ASC. When the 2nd and 3rd counts tie, both tied values are treated as
    minority (conservative — catches a lone wrong member whose service
    happens to tie the 2nd place)."""
    svc_counts = Counter(services)
    codes_sorted = sorted(svc_counts, key=lambda k: (-svc_counts[k], k))
    third = svc_counts[codes_sorted[2]] if len(codes_sorted) > 2 else 0
    top2_codes = {k for k in codes_sorted if svc_counts[k] > third}
    return {mid: {"needs_review": svc not in top2_codes, "service": svc}
            for mid, svc in enumerate(services)}


def run_pool(offering_id: str, incidents: list[dict], verifier: Verifier | None = None,
             max_proposal_members: int | None = None) -> dict:
    """Cluster ONE offering pool. Returns the run report (never mints sub-offerings).

    max_proposal_members: W3 guard for cross-domain pools (OFFERING-000) — any
    candidate cluster larger than this is auto-flagged NEEDS_REVIEW regardless of
    cohesion. None = W2 behavior (no cap).
    """
    t0 = time.perf_counter()
    verifier = verifier or Verifier()
    n = len(incidents)
    assert offering_id == OFFERING_000 or all(
        offering_of(i.get("classification_dict", {}).get("service", "")) == offering_id
        for i in incidents), "cross-offering incident in pool — impossible by construction"

    embs = [embed_pure(i.get("title", ""), i.get("description", "")) for i in incidents]
    assert all(e is not None for e in embs), "embedding model unavailable"
    sim = np.stack(embs) @ np.stack(embs).T
    np.fill_diagonal(sim, -1.0)

    candidates = generate_candidates(incidents, sim)
    auto = [c for c in candidates if c[2] >= AUTO_ACCEPT]
    remaining = [c for c in candidates if c[2] < AUTO_ACCEPT]

    verdicts = verifier.verify_pairs([(incidents[i], incidents[j]) for i, j, _ in remaining])
    confirmed = [(i, j, s) for (i, j, s), v in zip(remaining, verdicts) if v["decision"] == "YES"]
    yes_edges = [(i, j, s, v["reason"]) for (i, j, s), v in zip(remaining, verdicts)
                 if v["decision"] == "YES"]

    # union-find
    uf = UnionFind(n)
    for (i, j, _s) in auto:
        uf.union(i, j)
    for (i, j, _) in confirmed:
        uf.union(i, j)
    comps = {}
    for i in range(n):
        comps.setdefault(uf.find(i), []).append(i)
    comps = [sorted(v) for v in comps.values() if len(v) >= 2]

    # oversize guard >20: re-verify weakest 25% of component edges, drop flips
    edge_set = {(min(i, j), max(i, j)) for i, j, _ in auto} | {(min(i, j), max(i, j)) for i, j, _, _ in yes_edges}
    oversize_before = [len(c) for c in comps if len(c) > OVERSIZE_THRESHOLD]
    dropped = 0
    for comp in comps:
        if len(comp) <= OVERSIZE_THRESHOLD:
            continue
        comp_edges = sorted([(i, j, float(sim[i, j])) for (i, j) in edge_set if i in comp and j in comp],
                            key=lambda t: t[2])
        weakest = comp_edges[:max(1, len(comp_edges) // 4)]
        for (i, j, s) in weakest:
            v = verifier._ask_individual(incidents[i], incidents[j])
            if v is None or v[0] == "NO":
                edge_set.discard((min(i, j), max(i, j)))
                dropped += 1
    if dropped:
        uf2 = UnionFind(n)
        for (i, j) in edge_set:
            uf2.union(i, j)
        comps2 = {}
        for i in range(n):
            comps2.setdefault(uf2.find(i), []).append(i)
        comps = [sorted(v) for v in comps2.values() if len(v) >= 2]
    oversize_after = [len(c) for c in comps if len(c) > OVERSIZE_THRESHOLD]

    # drift review on clusters >= 4 (retry-once-then-FLAG, chunked >25)
    drift_log = []
    post_drift = []
    for c in comps:
        if len(c) < 4:
            post_drift.append(c)
            continue
        cohesion = float(np.mean([sim[a, b] for a in c for b in c if b > a]))
        outcome, removed = drift_review(verifier, incidents, c)
        drift_log.append({"size": len(c), "cohesion": round(cohesion, 4),
                          "outcome": outcome, "removed": len(removed)})
        if removed:
            rs = {i for i in c if incidents[i]["id"] in removed}
            keep = [i for i in c if i not in rs]
            if len(keep) >= 2:
                post_drift.append(keep)
        else:
            post_drift.append(c)

    # purity floor + proposals (clusters >= MIN_CLUSTER_SIZE, clean only)
    proposal_blocks = []
    needs_review = []
    for c in post_drift:
        if len(c) < MIN_CLUSTER_SIZE:
            continue
        svc_codes = [incidents[i].get("classification_dict", {}).get("service", "?") for i in c]
        flags_by_idx = compute_member_flags(svc_codes)
        # map member index -> incident id
        member_flags = {incidents[i]["id"]: flags_by_idx[k]
                        for k, i in enumerate(c)}
        n_flagged = sum(1 for f in member_flags.values() if f["needs_review"])
        cohesion = float(np.mean([sim[a, b] for a in c for b in c if b > a]))
        oversized = max_proposal_members is not None and len(c) > max_proposal_members
        # proposal excluded when >=1/3 of members flagged OR cluster floor trips
        # OR the cross-domain member cap trips (auto-NEEDS_REVIEW, regardless of
        # cohesion — W3 guard for OFFERING-000 contamination risk).
        excluded = (n_flagged >= max(1, len(c) // 3)
                    or cohesion < PURITY_MIN_SIM or len(set(svc_codes)) > PURITY_MAX_SERVICES
                    or oversized)
        flags = {"mean_sim": round(cohesion, 4), "n_services": len(set(svc_codes)),
                 "needs_review": excluded, "oversized": oversized, "members": member_flags}
        block = {"members": c, "flags": flags,
                 "reasons": {f"{incidents[i]['id'][:6]}~{incidents[j]['id'][:6]}": r
                             for (i, j, _s, r) in yes_edges
                             if i in c and j in c}}
        if flags["needs_review"]:
            needs_review.append(block)
        else:
            proposal_blocks.append(block)

    labels = label_proposals(verifier, incidents, proposal_blocks)

    proposals = []
    for pblock in proposal_blocks:
        label = labels.get(str(len(proposals)), "")
        prop = store.create_proposal(
            offering_id=offering_id,
            member_ids=[incidents[i]["id"] for i in pblock["members"]],
            mean_sim=pblock["flags"]["mean_sim"],
            verifier_reasons=pblock["reasons"],
            purity_flags=pblock["flags"],
            proposed_label=label,
        )
        proposals.append(prop)

    return {
        "offering_id": offering_id,
        "n_pool": n,
        "candidates": len(candidates),
        "auto_accepted": len(auto),
        "llm_pairs": len(remaining),
        "yes_edges": len(confirmed),
        "unresolved": verifier.unresolved,
        "oversize_before": oversize_before,
        "oversize_after": oversize_after,
        "edges_dropped": dropped,
        "drift": drift_log,
        "proposals": proposals,
        "needs_review_clusters": [{"size": len(b["members"]), "flags": b["flags"]}
                                  for b in needs_review],
        "tickets_covered": sum(len(p["member_ids"]) for p in proposals),
        "timing_s": round(time.perf_counter() - t0, 2),
    }


def run_all_pools(pool_incidents: dict[str, list[dict]], verifier: Verifier | None = None) -> list[dict]:
    """Cluster every offering pool. pool_incidents: {offering_id: [incident, ...]}."""
    verifier = verifier or Verifier()
    reports = []
    for offering_id, incidents in sorted(pool_incidents.items()):
        if len(incidents) < MIN_CLUSTER_SIZE:
            continue
        _log.info("clustering pool %s (%d tickets)", offering_id, len(incidents))
        reports.append(run_pool(offering_id, incidents, verifier))
    return reports
