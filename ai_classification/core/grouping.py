"""Two-phase incident grouping: offering exact-match (Phase 1) then embedding + LLM (Phase 2).

Used by the /api/reports endpoint to propose clusters from active incidents.

Phase 1 — Offering exact-match: Incidents whose LLM classification shares the
same offering (first segment of the service string, e.g. "System/Application
- Nusuk Masar Haj") form guaranteed clusters. No similarity threshold or LLM
validation needed — the classification already determined they are the same
root cause. Tickets without a resolvable offering (OFFERING-000) fall to
Phase 2. (The legacy failure_mode code is NOT a grouping key anymore.)

Phase 2 — Embedding + LLM: The remaining OFFERING-000 incidents are
clustered by cosine similarity over bge-m3 embeddings, using an
volume-adaptive threshold for recall. Graph-connected components above
MIN_DENSITY are sent to an LLM validator that confirms coherence, prunes
outliers, and assigns a human-readable name. Any candidate group larger
than MAX_VALIDATOR_GROUP_SIZE is dropped outright — a graph component that
big is almost certainly a false merge of several real incidents.

Pipeline position: 30_cluster — offering exact-match + embedding clustering."""

import hashlib
import json
import logging
import time
from collections import defaultdict

import networkx as nx
import numpy as np

from .store import store
from .llm import call_llm, strip_json_fences
from .suboffering import (
    MATCH_THRESHOLD,
    embed_pure,
    match_against_exemplars,
    offering_of,
    OFFERING_000,
)

_log = logging.getLogger(__name__)

# ── Tunable parameters ──────────────────────────────────────────────────

# Phase-2 (embedding) clustering base values. These are the *middle-of-the-
# road* settings; the rebuild loop adapts them to the current incident
# volume via _sensitivity_params() — see below.
SIMILARITY_THRESHOLD = 0.50        # intra-bucket clustering threshold (deliberately wider than
                                   # store.settings.similarity_threshold (0.80) — the grouping pass
                                   # casts a wider net for recall; fine-grained dedupe is stricter).
MIN_CLUSTER_SIZE = 3               # smallest group to return
MIN_DENSITY = 0.4                  # chain filter
MAX_VALIDATOR_GROUP_SIZE = 15      # candidates larger than this are not validated

# ── Volume-adaptive sensitivity ─────────────────────────────────────────
# Incident volume is NOT constant: some periods have a handful of tickets,
# others a flood (e.g. Hajj season). Fixed thresholds behave badly at both
# extremes:
#   * Few incidents  → thresholds too strict → related tickets never group,
#     operators see 15 single-ticket "clusters" instead of 2 real problems.
#   * Many incidents → thresholds too loose  → unrelated tickets merge into
#     giant meaningless clusters.
# So the sensitivity (similarity threshold + min cluster size) is a smooth
# function of the ACTIVE incident count, with floor/ceiling bounds:
#   count <= LOOSE_AT   → LOOSE regime:  lower threshold, min size 2
#   count >= TIGHT_AT   → TIGHT regime:  higher threshold, min size 4
#   in between          → linear interpolation on the threshold
# Deterministic (pure function of the count — same data, same grouping,
# no randomness; the LLM validator still uses seed 42 / temperature 0).
LOOSE_AT = 20                        # at or below this many active incidents
TIGHT_AT = 150                       # at or above this many active incidents
LOOSE_THRESHOLD = 0.40               # few incidents: cast a wide net
TIGHT_THRESHOLD = 0.60               # flood: precision over recall
LOOSE_MIN_CLUSTER = 2                # few incidents: pairs can be a real group
TIGHT_MIN_CLUSTER = 4                # flood: require strong evidence for a group


def _sensitivity_params(active_count: int) -> tuple[float, int]:
    """Volume-adaptive (similarity_threshold, min_cluster_size).

    Pure function of the active incident count — deterministic, no state,
    no randomness. Bounded between the LOOSE and TIGHT regimes.
    """
    if active_count <= LOOSE_AT:
        return LOOSE_THRESHOLD, LOOSE_MIN_CLUSTER
    if active_count >= TIGHT_AT:
        return TIGHT_THRESHOLD, TIGHT_MIN_CLUSTER
    # Linear interpolation between the two regimes.
    frac = (active_count - LOOSE_AT) / (TIGHT_AT - LOOSE_AT)
    threshold = LOOSE_THRESHOLD + frac * (TIGHT_THRESHOLD - LOOSE_THRESHOLD)
    min_size = round(LOOSE_MIN_CLUSTER + frac * (TIGHT_MIN_CLUSTER - LOOSE_MIN_CLUSTER))
    return round(threshold, 3), min_size

# ── Verdict cache ──────────────────────────────────────────────────────────
# Key = sorted, joined member IDs (fingerprint). Value = verdict dict.
# Persists across rebuild cycles so stable groups don't get re-validated.
_verdict_cache: dict[str, dict] = {}
_VERDICT_CACHE_TTL = 3600 * 24  # 24 hours

# ── Arabic cluster-name cache ────────────────────────────────────────────
# Phase-1 offering clusters are named by the LLM in Arabic (the NOC
# dashboard is bilingual; cluster names should read naturally). Keyed by
# member-ID fingerprint — one offering emits MULTIPLE clusters (sub-offering
# splits + residual), each with a DIFFERENT name, so the cache key must be
# the members, never the offering. TTL keeps the 5-min rebuild from
# hammering the LLM; invalidate_incident() evicts entries when a ticket
# changes clusters.
_ar_name_cache: dict[str, dict] = {}  # fingerprint -> {"name", "_cached_at"}
_AR_NAME_TTL = 3600 * 24  # 24 hours

# Naming prompt (NEW — not one of the frozen classifier/canary prompts).
# The LLM READS the actual member tickets (titles + descriptions) and must
# produce a short, simple Arabic title for the cluster — a description of
# the real shared problem, NOT a translation of an offering/service name.
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

# Cap on how many member tickets go into the naming prompt (titles +
# descriptions, truncated). Enough to cover any real cluster without
# blowing the context window.
_AR_NAME_MAX_TICKETS = 15


def _arabic_cluster_name(member_incidents: list[dict]) -> str:
    """LLM-generated Arabic title for a cluster, derived from READING the
    member tickets. Cached per member-ID fingerprint (same members →
    same name, no repeat LLM calls across rebuilds). Falls back to the
    English label on failure — the rebuild must never break."""
    fp = _make_fingerprint(member_incidents)
    cached = _ar_name_cache.get(fp)
    if cached:
        age = time.time() - cached.get("_cached_at", 0)
        if age > _AR_NAME_TTL:
            del _ar_name_cache[fp]
        else:
            return cached["name"]
    name = member_incidents[0].get("title", "") if member_incidents else "Cluster"
    try:
        tickets = []
        for inc in member_incidents[:_AR_NAME_MAX_TICKETS]:
            t = (inc.get("title") or "").strip()[:120]
            d = (inc.get("description") or "").strip()[:200]
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
        # Guard: require at least one Arabic-script character, else keep English.
        if any("\u0600" <= ch <= "\u06FF" for ch in label) and len(label) <= 60:
            name = label
        else:
            _log.warning("Arabic title rejected (no Arabic script or too long): %r", label[:40])
    except Exception as exc:  # noqa: BLE001 — naming is best-effort
        _log.warning("Arabic title generation failed: %s", exc)
    _ar_name_cache[fp] = {"name": name, "_cached_at": time.time()}
    _log.info("Cluster title: %s (%d tickets)", name, len(member_incidents))
    return name


def _make_fingerprint(incidents: list[dict]) -> str:
    """Create a stable fingerprint from sorted incident IDs."""
    ids = sorted(inc["id"] for inc in incidents)
    return ",".join(ids)


def _get_cached_verdict(incidents: list[dict]) -> dict | None:
    """Return cached verdict if fingerprint matches and TTL hasn't expired."""
    fp = _make_fingerprint(incidents)
    cached = _verdict_cache.get(fp)
    if cached is None:
        return None
    age = time.time() - cached.get("_cached_at", 0)
    if age > _VERDICT_CACHE_TTL:
        del _verdict_cache[fp]
        return None
    _log.info("Verdict cache HIT for %d-incident group (age=%.0fs)", len(incidents), age)
    return cached


def _cache_verdict(incidents: list[dict], verdict: dict):
    """Store a validated verdict in the cache."""
    fp = _make_fingerprint(incidents)
    verdict = dict(verdict)  # copy
    verdict["_cached_at"] = time.time()
    _verdict_cache[fp] = verdict
    _log.debug("Verdict cached for %d-incident group", len(incidents))


def invalidate_cache():
    """Clear both verdict cache and cluster snapshot — call after data mutations."""
    _verdict_cache.clear()
    _ar_name_cache.clear()
    _snapshot.clear()
    _log.info("Full cluster cache invalidated")


def invalidate_incident(incident_id: str) -> None:
    """Drop cached names/verdicts for EVERY cluster whose member set contains
    this incident — the old cluster it LEFT and the new one it JOINED both
    carry the ticket's ID in their fingerprint. Membership changed → labels
    must be regenerated, not served stale. Snapshot cleared so the next
    dashboard read rebuilds with the new memberships."""
    dropped = 0
    for cache in (_ar_name_cache, _verdict_cache):
        for fp in [k for k in cache if incident_id in k.split(",")]:
            del cache[fp]
            dropped += 1
    if dropped:
        _snapshot.clear()
        _log.info("invalidate_incident(%s): dropped %d cached cluster entries",
                  incident_id, dropped)


def request_rebuild():
    """Trigger an immediate async rebuild in the background thread."""
    import threading
    thread = threading.Thread(target=_build_and_cache, daemon=True)
    thread.start()
    _log.info("Requested immediate cluster rebuild")


# Background thread rebuilds every N seconds. Dashboard reads the latest snapshot.
_snapshot: dict[str, dict] = {}  # period -> latest cluster result
_last_build: float = 0           # timestamp of last build
_BUILD_INTERVAL = 300            # rebuild every 5 minutes


def _build_and_cache():
    """Run the full clustering pipeline and store the snapshot."""
    global _last_build
    _log.info("Background cluster rebuild starting...")
    for period in ("daily", "weekly"):
        try:
            result = _build_clusters(period)
            result["last_build"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
            result["last_build_ts"] = time.time()
            _snapshot[period] = result
            _log.info("  %s: %d clusters from %d incidents",
                      period,
                      len(_snapshot[period].get("clusters", [])),
                      _snapshot[period].get("total_incidents", 0))
        except Exception as e:
            _log.error("Failed to build clusters for %s: %s", period, e)
    _last_build = time.time()


def start_rebuild_loop():
    """Start a daemon thread that rebuilds clusters every BUILD_INTERVAL seconds."""
    import threading
    def _first_build():
        _build_and_cache()
        # now enter periodic loop
        while True:
            time.sleep(_BUILD_INTERVAL)
            _build_and_cache()
    thread = threading.Thread(target=_first_build, daemon=True)
    thread.start()
    _log.info("Cluster rebuild loop started — every %ds", _BUILD_INTERVAL)


# Public API — returns latest snapshot instantly, no LLM calls
def build_clusters(period: str = "daily") -> dict:
    result = _snapshot.get(period)
    if result is not None:
        return result
    # First run — serve empty while background thread builds
    return {"total_incidents": 0, "clusters": [], "subsystem_summary": [], "status": "building"}

# ── Public API ──────────────────────────────────────────────────────────


# Load active incidents, build similarity graph, return dense clusters as JSON
def _build_clusters(period: str = "daily") -> dict:
    """Load active incidents, cluster by embedding similarity, return report JSON."""
    # Subsystem rollup — separate from duplicate-detection clustering below.
    # Plain GROUP BY affected_system+service over active incidents, no
    # embeddings or LLM involved. Answers "is this subsystem having a bad
    # day" (many different symptoms, same system) as opposed to the
    # clusters below, which answer "is this the same incident reported
    # twice" (same symptom). Both are real questions; conflating them by
    # normalizing canonical statements toward the subsystem would break
    # duplicate detection to serve the rollup — so they stay separate.
    subsystem_summary = _subsystem_rollup(store.list_incidents(status="active"))

    raw = store.list_incidents_with_embeddings()
    if not raw:
        return {"total_incidents": 0, "clusters": [], "subsystem_summary": subsystem_summary}

    # Filter to incidents that actually have embeddings
    pairs = [(inc, emb) for inc, emb in raw if emb is not None]
    if len(pairs) < LOOSE_MIN_CLUSTER:
        return {"total_incidents": len(pairs), "clusters": [], "subsystem_summary": subsystem_summary}

    incidents, embeddings = zip(*pairs)
    emb_matrix = np.stack(embeddings)

    # Volume-adaptive sensitivity — one threshold + min-size for this
    # rebuild, derived from how many active incidents we have.
    adaptive_threshold, adaptive_min_size = _sensitivity_params(len(incidents))
    _log.info(
        "Volume-adaptive sensitivity: %d active incidents → threshold=%.3f, min_cluster_size=%d",
        len(incidents), adaptive_threshold, adaptive_min_size,
    )

    # Cosine similarity
    sim = emb_matrix @ emb_matrix.T
    sim = np.clip(sim, 0.0, 1.0)

    n = len(incidents)

    # Phase 1: Exact-match grouping by OFFERING (coarse service bucket)
    # Tickets sharing an offering (service first segment, e.g. "System/
    # Application - Nusuk Masar Haj") form guaranteed clusters. This is the
    # product taxonomy (offerings/sub-offerings) — the legacy failure_mode
    # code is NOT used as a grouping key anymore. Tickets without a
    # resolvable offering (OFFERING-000) fall through to Phase 2.
    offering_buckets: dict[str, list[int]] = defaultdict(list)
    for i, inc in enumerate(incidents):
        c = inc.get("classification_dict", {})
        offering = offering_of(c.get("service")) or OFFERING_000
        offering_buckets[offering].append(i)

    _log.debug("Offering buckets: %s",
               {k: len(v) for k, v in sorted(offering_buckets.items()) if k != OFFERING_000})

    all_clusters: list[dict] = []
    used: set[int] = set()

    # Sub-offering membership (READ-ONLY): ACTIVE sub-offerings + their
    # exemplars, so phase-1 emits FM-equivalent sub-clusters (named by
    # sub-offering) whenever the engine has minted them.
    _subs = store.list_sub_offerings(status="active")
    _subs_by_offering: dict[str, list[dict]] = defaultdict(list)
    _exemplars_by_offering: dict[str, list[dict]] = defaultdict(list)
    _sub_name: dict[str, str] = {}
    for _s in _subs:
        _subs_by_offering[_s["offering_id"]].append(_s)
        _sub_name[_s["id"]] = _s["name"]
        _exemplars_by_offering[_s["offering_id"]].extend(store.list_exemplars(_s["id"]))

    def _emit(name: str, m_indices: list[int], offering_key: str,
              arabic: bool = False) -> dict | None:
        """Build one cluster dict for the given member indices (read-only).

        arabic=True → the cluster label is LLM-generated in Arabic from the
        current English name (offering-level, residual, AND sub-offering
        clusters — the NOC dashboard is bilingual, all labels should read
        naturally in Arabic)."""
        m = [incidents[i] for i in m_indices]
        if not m:
            return None
        worst_sev = _worst_severity(m)
        top_sys, top_svc = _dominant_labels(m)
        if arabic:
            name = _arabic_cluster_name(list(m))
        cid = hashlib.md5(f"{offering_key}|{name}".encode()).hexdigest()[:12]
        cluster_incidents = []
        for idx, inc in zip(m_indices, m):
            centroid_sim = 100.0
            if len(m_indices) > 1:
                member_sims = [float(sim[idx, other]) for other in m_indices if other != idx]
                if member_sims:
                    centroid_sim = round(sum(member_sims) / len(member_sims) * 100, 1)
            cluster_incidents.append({
                "id": inc["id"],
                "title": inc.get("title", ""),
                "severity": _extract_severity(inc),
                "canonical_statement": _extract_canonical_statement(inc)[:200],
                "similarity_pct": centroid_sim,
                "description": inc.get("description", "")[:200],
                "affected_system": top_sys,
                "service": top_svc,
                "status": inc.get("status", "active"),
                "created_at": inc.get("created_at", ""),
            })
        return {
            "cluster_id": cid,
            "name": name,
            "failure_mode_desc": name,
            "affected_system": top_sys,
            "affected_service": top_svc,
            "worst_severity": worst_sev,
            "count": len(m_indices),
            "summary": f"{len(m_indices)} tickets — {name}",
            "pruned": [],
            "coherence": cluster_coherence(m, sim, m_indices),
            "incidents": cluster_incidents,
        }

    for offering, indices in offering_buckets.items():
        if offering == OFFERING_000 or len(indices) < adaptive_min_size:
            continue
        # 1) Sub-offering split — FM-equivalent granularity via read-only
        #    exemplar matching. Unmatched members fall through to the
        #    offering-level residual cluster.
        sub_groups: dict[str, list[int]] = {}
        exemplars = _exemplars_by_offering.get(offering, [])
        for idx in indices:
            inc = incidents[idx]
            emb = embed_pure(inc.get("title", ""), inc.get("description", ""))
            if emb is None:
                continue
            sub_id, sub_sim = match_against_exemplars(emb, exemplars)
            if sub_id and sub_sim >= MATCH_THRESHOLD:
                sub_groups.setdefault(sub_id, []).append(idx)
        matched_ids = {i for g in sub_groups.values() for i in g}
        for sub_id, sidx in sub_groups.items():
            if len(sidx) < 2:
                continue
            c = _emit(_sub_name.get(sub_id, offering), sidx, offering, arabic=True)
            if c:
                all_clusters.append(c)
        # 2) Residual offering cluster (members that matched no sub-offering)
        residual = [i for i in indices if i not in matched_ids]
        if len(residual) >= adaptive_min_size:
            c = _emit(offering, residual, offering, arabic=True)
            if c:
                all_clusters.append(c)
        used.update(indices)
        _log.info("Offering cluster: %s — %d tickets", offering, len(indices))

    # Phase 2: Embedding-based clustering for OFFERING-000 (unclassified) leftovers
    leftover = [i for i in range(n) if i not in used]
    if len(leftover) >= adaptive_min_size:
        prefix_buckets: dict[str, list[int]] = {}
        for i in leftover:
            cs = _extract_canonical_statement(incidents[i])
            prefix = cs.split(":")[0].strip() if ":" in cs else cs[:20]
            prefix_buckets.setdefault(prefix, []).append(i)
        _log.debug("OFFERING-000 prefix buckets: %s",
                   {k: len(v) for k, v in sorted(prefix_buckets.items())})
        for px, bx in prefix_buckets.items():
            if len(bx) < adaptive_min_size:
                continue
            bx_clusters, bx_used = _cluster_pass(
                incidents, sim, adaptive_threshold, bx, adaptive_min_size
            )
            all_clusters.extend(bx_clusters)
            used.update(bx_used)
        # Cross-bucket pass on remaining leftovers
        still_left = [i for i in leftover if i not in used]
        if len(still_left) >= adaptive_min_size:
            cross_clusters, cross_used = _cluster_pass(
                incidents, sim, adaptive_threshold + 0.05, still_left, adaptive_min_size
            )
            all_clusters.extend(cross_clusters)
            used.update(cross_used)

    all_clusters.sort(key=lambda c: c["count"], reverse=True)
    _log.info("build_clusters — %d active incidents, %d clusters found (phase1=%d, phase2=%s)",
              n, len(all_clusters), sum(1 for c in all_clusters if (c["name"] or "").startswith("OFFERING") and c["name"] != OFFERING_000),
              "active" if len(leftover) >= adaptive_min_size else "skipped")
    return {"total_incidents": n, "clusters": all_clusters, "subsystem_summary": subsystem_summary}


# ── Clustering pass ───────────────────────────────────────────────────────


# Build a similarity graph over `candidate_indices` at `threshold`, validate
# each dense component with the LLM, return accepted clusters + used indices
def _cluster_pass(
    incidents: tuple, sim: np.ndarray, threshold: float, candidate_indices: list[int],
    min_cluster_size: int = MIN_CLUSTER_SIZE,
) -> tuple[list[dict], set[int]]:
    G = nx.Graph()
    G.add_nodes_from(candidate_indices)

    for a, i in enumerate(candidate_indices):
        for j in candidate_indices[a + 1:]:
            if sim[i, j] >= threshold:
                G.add_edge(i, j, weight=sim[i, j])

    components = list(nx.connected_components(G))
    clusters: list[dict] = []
    used: set[int] = set()

    for comp in components:
        if len(comp) < min_cluster_size:
            continue
        sub = G.subgraph(comp)
        n_nodes = len(comp)
        n_edges = sub.number_of_edges()
        max_edges = n_nodes * (n_nodes - 1) / 2
        density = n_edges / max_edges if max_edges > 0 else 0
        if density < MIN_DENSITY:
            continue

        member_pairs = [(m, incidents[m]) for m in sorted(comp)]  # (index, dict)

        # A candidate this large is almost certainly several real incidents
        # merged by the wide threshold, not one big incident. Don't send it
        # to the validator (risks truncating mid-response) and don't trust
        # it unvalidated either — drop it rather than fail open.
        if len(member_pairs) > MAX_VALIDATOR_GROUP_SIZE:
            _log.warning(
                "Candidate group too large to validate (%d > %d), dropping",
                len(member_pairs), MAX_VALIDATOR_GROUP_SIZE,
            )
            continue

        _log.info("Cluster candidate — %d incidents, density=%.2f, sending to LLM validator",
                  len(member_pairs), density)

        cluster_incidents = [inc for _, inc in member_pairs]

        # ── Verdict cache check ──
        cached_verdict = _get_cached_verdict(cluster_incidents)
        if cached_verdict is not None:
            verdict = cached_verdict
            _log.info("  Using cached verdict (name='%s')", verdict.get("name", ""))
        else:
            # LLM validation — math proposed this group, the LLM confirms
            # coherence and prunes outliers. Falls back to the math proposal
            # untouched if the call fails, is malformed, or the pruning floor
            # is exceeded (see validate_group's safeguards below).
            validator_input = [
                {"id": inc["id"], "canonical_statement": _extract_canonical_statement(inc)}
                for inc in cluster_incidents
            ]
            verdict = validate_group(validator_input)
            if verdict is not None:
                _cache_verdict(cluster_incidents, verdict)

        pruned: list[dict] = []
        verdict_name = None
        verdict_description = None

        if verdict is not None and not verdict.get("is_coherent", True):
            # LLM examined this group and found no real common issue — drop it,
            # don't surface a false pattern. Members stay unused (available to
            # a later, looser pass).
            _log.info("Cluster rejected by validator as incoherent (%d incidents)",
                      len(member_pairs))
            continue
        elif verdict is not None:
            keep_ids = set(verdict.get("keep", []))
            remove_reasons = {r["id"]: r["reason"] for r in verdict.get("remove", [])}
            pruned = [
                {"id": inc["id"], "title": inc["title"], "reason": remove_reasons[inc["id"]]}
                for _, inc in member_pairs if inc["id"] in remove_reasons
            ]
            # Single filter pass — keeps (index, dict) in sync
            member_pairs = [(m, inc) for m, inc in member_pairs if inc["id"] in keep_ids]
            verdict_name = verdict.get("name") or None
            verdict_description = verdict.get("description") or None

        if len(member_pairs) < min_cluster_size:
            continue

        # ── Emission floor ── reject clusters whose internal coherence is too low
        # Short signatures produce tighter vectors; a mean below the floor means
        # the group is held together by shared filler (e.g. all starting "Error")
        # rather than real semantic similarity. The floor scales with the
        # adaptive threshold:
        #   loose (0.40) → floor 0.50  (pairs at 0.55 DO group — the point of
        #                                the loose regime; the old flat 0.70
        #                                killed every weak-but-real pair)
        #   mid   (0.50) → floor 0.70  (EXACTLY the previous behavior)
        #   tight (0.60) → floor 0.80  (flood: require strong agreement)
        if threshold <= 0.45:
            emission_floor = threshold + 0.10
        else:
            emission_floor = min(0.80, threshold + 0.20)
        member_indices = sorted(m for m, _ in member_pairs)
        intra_pairs = 0
        intra_sum = 0.0
        for a in range(len(member_indices)):
            for b in range(a + 1, len(member_indices)):
                val = sim[member_indices[a], member_indices[b]]
                if val >= threshold:
                    intra_pairs += 1
                    intra_sum += val
        mean_intra = intra_sum / intra_pairs if intra_pairs > 0 else 0.0
        if mean_intra < emission_floor - 1e-9:   # float-safe comparison
            _log.info("Cluster rejected by emission floor (mean_intra=%.3f < %.2f) — %d tickets",
                      mean_intra, emission_floor, len(member_pairs))
            continue

        cluster_incidents = [inc for _, inc in member_pairs]
        worst_severity = _worst_severity(cluster_incidents)
        top_system, top_service = _dominant_labels(cluster_incidents)
        cid = "".join(inc["id"][:4] for inc in cluster_incidents[:3])

        clusters.append({
            "cluster_id": cid,
            "name": verdict_name,
            "affected_system": top_system,
            "affected_service": top_service,
            "worst_severity": worst_severity,
            "count": len(cluster_incidents),
            "summary": verdict_description or _auto_summary(
                top_system, top_service, worst_severity, len(cluster_incidents)),
            "pruned": pruned,
            "incidents": [
                {
                    "id": inc["id"],
                    "title": inc["title"],
                    "severity": _extract_severity(inc),
                    "canonical_statement": _extract_canonical_statement(inc),
                    "similarity_pct": round(
                        float(np.mean([sim[m, o] for o in member_indices if o != m])) * 100, 1
                    ),
                    "description": inc["description"][:500],
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
                }
                for m, inc in member_pairs
            ],
        })
        _log.info("Cluster accepted — name='%s', system=%s, count=%d, pruned=%d",
                  verdict_name or "(unnamed)", top_system, len(cluster_incidents), len(pruned))
        if pruned:
            for p in pruned:
                _log.debug("  Pruned: %s — %s", p["id"], p.get("reason", "no reason"))

        used.update(m for m, _ in member_pairs)

    return clusters, used


# ── Subsystem rollup ──────────────────────────────────────────────────────


# Count active incidents per (affected_system, service) — no embeddings, no LLM.
# Complements the duplicate-detection clusters above; doesn't replace them.
def _subsystem_rollup(active_incidents: list[dict]) -> list[dict]:
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for inc in active_incidents:
        data = inc.get("classification_dict", {})
        system = data.get("affected_system") or "Unknown"
        service = data.get("service") or "Unknown"
        buckets[(system, service)].append(inc)

    rollup = [
        {
            "affected_system": system,
            "affected_service": service,
            "count": len(incs),
            "worst_severity": _worst_severity(incs),
            "incident_ids": [inc["id"] for inc in incs],
        }
        for (system, service), incs in buckets.items()
        if len(incs) >= 2  # only surface subsystems with more than one open ticket
    ]
    rollup.sort(key=lambda r: r["count"], reverse=True)
    return rollup


# ── Clustering helpers ─────────────────────────────────────────────────────


SEVERITY_RANK = {"Critical": 4, "Major": 3, "Minor": 2, "Cosmetic": 1, "": 0}
SEVERITY_NAMES = {4: "Critical", 3: "Major", 2: "Minor", 1: "Cosmetic"}


# Parse severity from an incident's classification JSON
def _extract_severity(inc: dict) -> str:
    data = inc.get("classification_dict", {})
    return data.get("severity", "Minor")


# Parse full classification JSON (safe)
def _parse_classification(inc: dict) -> dict:
    return inc.get("classification_dict", {})


# Extract a single field from an incident's classification JSON
def _class_field(inc: dict, field: str) -> str:
    data = inc.get("classification_dict", {})
    return data.get(field, "")


# Parse canonical_statement from an incident's classification JSON, fall back to title
def _extract_canonical_statement(inc: dict) -> str:
    data = inc.get("classification_dict", {})
    return data.get("canonical_statement") or inc.get("title", "")


# Return the highest severity across a list of incidents
def _worst_severity(incidents: list[dict]) -> str:
    ranks = [SEVERITY_RANK.get(_extract_severity(i), 0) for i in incidents]
    return SEVERITY_NAMES.get(max(ranks), "Minor")


# Find the most common affected_system and service in a cluster
def _dominant_labels(incidents: list[dict]) -> tuple[str, str]:
    systems: dict[str, int] = defaultdict(int)
    services: dict[str, int] = defaultdict(int)
    for inc in incidents:
        data = inc.get("classification_dict", {})
        if data:
            systems[data.get("affected_system", "Unknown")] += 1
            services[data.get("service", "Unknown")] += 1
    top_sys = max(systems, key=lambda k: systems[k]) if systems else "Unknown"
    top_svc = max(services, key=lambda k: services[k]) if services else "Unknown"
    return top_sys, top_svc


def _auto_summary(system: str, service: str, severity: str, count: int) -> str:
    severity_desc = {
        "Critical": "a critical issue",
        "Major": "a significant issue",
        "Minor": "a minor issue",
        "Cosmetic": "a cosmetic issue",
    }.get(severity, "an issue")
    return (
        f"This cluster contains {count} related incidents affecting "
        f"{system} / {service}. "
        f"The system identified this as {severity_desc} based on embedding "
        f"similarity across {count} tickets."
    )


# ── LLM validator ─────────────────────────────────────────────────────────
#
# The math proposes groups at a wide similarity threshold. The LLM then
# validates each group: confirms coherence, removes outliers, and names
# what remains.
#
# Safeguards:
#   1. ID reconciliation — the verdict's keep+remove ids must exactly match
#      the input ids, or the verdict is discarded entirely.
#   2. Pruning floor (60%) — a verdict that would remove more than 60% of a
#      coherent group isn't trusted; discarded, math proposal kept.
#   3. Outliers preserved — pruned members are returned in the response,
#      never silently dropped; the caller decides what to do with them.
#   4. Verdict caching — not implemented yet.

VALIDATOR_PROMPT = """You are validating an incident group for a NOC shift dashboard.

Here are N incident tickets proposed as one underlying problem by an embedding
similarity search. That search is a wide net — it often includes one or two
tickets that don't actually belong. Your job is to confirm the real core issue
and prune whatever doesn't match it.

Rules:
- Return ONLY valid JSON with no extra text.
- Every input ID must appear in exactly one of "keep" or "remove".
- is_coherent = true whenever there IS a real shared issue among the tickets —
  even if that means removing one or two outliers to get there. Put the
  tickets that genuinely share the issue in "keep", and the outliers in
  "remove" with a one-sentence reason each naming what makes them different.
- is_coherent = false ONLY if there is no real shared issue at all — the
  tickets are simply unrelated, not "mostly related with an outlier". In this
  case put every ID in "remove" with a reason.
- Never invent an ID that wasn't given to you. Never omit one.
- name: a short label (max 8 words) for the shared issue — MUST be written in
  ARABIC (العربية), since the dashboard is bilingual and most tickets are in
  Arabic (omit if is_coherent is false).
- description: one-line description of what this incident is (omit if is_coherent is false).

Return format:
{
  "is_coherent": true/false,
  "keep": ["id1", "id2"],
  "remove": [
    {"id": "id3", "reason": "This is a login failure, the rest are checkout timeouts."}
  ],
  "name": "اسم مختصر للمشكلة (بحد أقصى 8 كلمات بالعربية)",
  "description": "One-line description of what this incident is."
}"""
# ── Coherence metric ─────────────────────────────────────────────────────
# Computes intra-cluster similarity and flags outliers. Used post-grouping
# to audit cluster quality and identify prunable tickets.


def cluster_coherence(
    incident_dicts: list[dict], sim_matrix: np.ndarray,
    indices: list[int],
) -> dict:
    """Return {mean, min, health} for a cluster, and prunable ticket IDs."""
    if len(indices) < 2:
        return {"mean": None, "min": None, "health": "insufficient"}
    pairs = []
    for a in range(len(indices)):
        for b in range(a + 1, len(indices)):
            pairs.append(float(sim_matrix[indices[a], indices[b]]))
    mean_sim = float(np.mean(pairs)) if pairs else 0.0
    min_sim = float(np.min(pairs)) if pairs else 0.0

    # Centroid-based outlier detection
    vecs = np.array([sim_matrix[idx] for idx in indices])  # use sim row as proxy
    centroid = vecs.mean(axis=0)
    centroid_norm = np.linalg.norm(centroid)
    if centroid_norm > 0:
        centroid = centroid / centroid_norm
    prunable = []
    for i, idx in enumerate(indices):
        dist = float(sim_matrix[idx] @ centroid)
        if dist < 0.60:
            prunable.append({
                "idx": idx,
                "coherence": round(dist, 3),
            })

    if mean_sim >= 0.75:
        health = "healthy"
    elif mean_sim >= 0.60:
        health = "review"
    else:
        health = "unhealthy"

    return {
        "mean": round(mean_sim, 3),
        "min": round(min_sim, 3),
        "health": health,
        "prunable": prunable,
    }



# Call the LLM via LiteLLM, same provider plumbing as core/classifier.py
def validate_group(incidents: list[dict]) -> dict | None:
    """Send a candidate group to the LLM for validation.

    Args:
        incidents: list of dicts with 'id' and 'canonical_statement' keys.

    Returns:
        dict with is_coherent, keep, remove, name, description
        or None if the verdict can't be trusted (LLM/parse failure, id
        mismatch, pruning floor exceeded) — caller should keep math's proposal.
    """
    if len(incidents) < 2:
        return None
    if len(incidents) > MAX_VALIDATOR_GROUP_SIZE:
        _log.warning("validate_group called with %d incidents (> %d max), refusing",
                     len(incidents), MAX_VALIDATOR_GROUP_SIZE)
        return None

    # Build the ticket list for the prompt
    ticket_lines = []
    for inc in incidents:
        cs = inc.get("canonical_statement", "") or inc.get("title", "")
        ticket_lines.append(f"  [{inc['id']}] {cs[:200]}")
    ticket_block = "\n".join(ticket_lines)

    user_msg = f"Here are {len(incidents)} tickets:\n{ticket_block}\n\nReturn JSON verdict."

    try:
        content = call_llm([
            {"role": "system", "content": VALIDATOR_PROMPT},
            {"role": "user", "content": user_msg},
        ], max_tokens=1500, temperature=0.1)

        # Parse JSON
        result = _parse_json(content)

        # ── Safeguard 1: ID reconciliation ──
        input_ids = {inc["id"] for inc in incidents}
        keep_ids = set(result.get("keep", []))
        remove_ids = {r["id"] for r in result.get("remove", [])}
        returned_ids = keep_ids | remove_ids

        if returned_ids != input_ids:
            _log.warning("Validator ID mismatch — input=%d, returned=%d. Keeping math proposal.",
                         len(input_ids), len(returned_ids))
            return None

        # ── Safeguard 2: Pruning floor (60%) ──
        # Only guards against over-pruning a group the LLM says IS coherent.
        # An explicit is_coherent=false is a real "reject this group" verdict,
        # not a pruning decision — it must pass through, not get swallowed here.
        if result.get("is_coherent"):
            removal_ratio = len(remove_ids) / len(input_ids)
            if removal_ratio > 0.6:
                _log.warning("Validator wants to remove %.0f%% (>60%%). Keeping math proposal, flagging for human.",
                             removal_ratio * 100)
                return None

        # ── Safeguard 3: Outliers preserved (returned in response, not dropped) ──
        # Already handled — removed IDs are in the response, caller returns them to pool.

        result["keep"] = sorted(keep_ids)
        result["remove"] = sorted(
            [r for r in result.get("remove", []) if r["id"] in remove_ids],
            key=lambda x: x["id"],
        )
        _log.info("Validator result: coherent=%s, keep=%d, remove=%d, name='%s'",
                  result.get("is_coherent"), len(keep_ids), len(remove_ids),
                  result.get("name", ""))
        return result

    except Exception as exc:
        _log.warning("Validator LLM call failed: %s. Keeping math proposal.", exc)
        return None


def _parse_json(raw: str) -> dict:
    """Extract JSON from LLM response, stripping markdown fences."""
    return json.loads(strip_json_fences(raw))

