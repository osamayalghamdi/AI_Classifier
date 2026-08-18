"""Two-phase incident grouping: FM exact-match (Phase 1) then embedding + LLM (Phase 2).

Used by the /api/reports endpoint to propose clusters from active incidents.

Phase 1 — FM exact-match: Incidents whose LLM classification shares the same
failure_mode code (other than FM-000) form guaranteed clusters. No similarity
threshold or LLM validation needed — the classification already determined they
are the same root cause.

Phase 2 — Embedding + LLM: The remaining FM-000 (unclassified) incidents are
clustered by cosine similarity over bge-m3 embeddings, using a single wide
threshold (0.50) for recall. Graph-connected components above MIN_DENSITY are
sent to an LLM validator that confirms coherence, prunes outliers, and assigns
a human-readable name. Any candidate group larger than MAX_VALIDATOR_GROUP_SIZE
is dropped outright — a graph component that big is almost certainly a false
merge of several real incidents.

Pipeline position: 30_cluster — FM exact-match + embedding clustering."""

import hashlib
import json
import logging
import time
from collections import defaultdict

import networkx as nx
import numpy as np

from ..config import settings
from .store import store
from .llm import call_llm, strip_json_fences

_log = logging.getLogger(__name__)

# ── Tunable parameters ──────────────────────────────────────────────────

SIMILARITY_THRESHOLD = 0.50        # intra-bucket clustering threshold (deliberately wider than
                                   # store.settings.similarity_threshold (0.80) — the grouping pass
                                   # casts a wider net for recall; fine-grained dedupe is stricter).
MIN_CLUSTER_SIZE = 3               # smallest group to return
MIN_DENSITY = 0.4                  # chain filter
MAX_VALIDATOR_GROUP_SIZE = 15      # candidates larger than this are not validated

# ── Arabic display labels for FM-named clusters ───────────────────────────
# Display-only (dashboard/NOC labels). The frozen taxonomy (FAILURE_MODES)
# is untouched — classification and embeddings keep the upstream English
# names; only the report's failure_mode_desc is localized.
FM_AR_LABELS: dict[str, str] = {
    "FM-000": "غير مصنف",
    "FM-005": "فشل إيداع المعاملات المالية في البنك أو المحفظة",
    "FM-007": "اختفاء أيقونة تقييم الشركات من الواجهة",
    "FM-008": "عدم تحديث حالة البلاغ المعالج إلى مغلق",
    "FM-010": "تعذر إدخال أرقام الحجاج أثناء التسجيل",
    "FM-011": "تعذر الوصول لتحديث بيانات الفوترة الضريبية",
    "FM-014": "تعذر الرد على البلاغات أو إغلاقها",
    "FM-015": "فشل اعتماد طلبات التنقل بين المدن قبل المغادرة",
    "FM-018": "فشل إصدار تصريح زيارة الروضة عند اختيار التاريخ",
    "FM-020": "فشل تأكيد الوصول الفعلي لعقد السكن",
    "FM-021": "فشل تقديم طلب الاعتراض بسبب خطأ في البيانات",
    "FM-022": "تعذر تقديم طلب التظلم على المخالفات",
    "FM-004": "عطل تشغيلي في نظام CRM",
}

# ── Verdict cache ──────────────────────────────────────────────────────────
# Key = sorted, joined member IDs (fingerprint). Value = verdict dict.
# Persists across rebuild cycles so stable groups don't get re-validated.
_verdict_cache: dict[str, dict] = {}
_VERDICT_CACHE_TTL = 3600 * 24  # 24 hours


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
    _snapshot.clear()
    _log.info("Full cluster cache invalidated")


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
    if len(pairs) < MIN_CLUSTER_SIZE:
        return {"total_incidents": len(pairs), "clusters": [], "subsystem_summary": subsystem_summary}

    incidents, embeddings = zip(*pairs)
    emb_matrix = np.stack(embeddings)

    # Cosine similarity
    sim = emb_matrix @ emb_matrix.T
    sim = np.clip(sim, 0.0, 1.0)

    n = len(incidents)

    # Phase 1: Exact-match grouping by failure_mode code
    # Tickets sharing an FM code (not FM-000) form guaranteed clusters.
    # This bypasses embedding similarity entirely for classified tickets.
    fm_buckets: dict[str, list[int]] = defaultdict(list)
    for i, inc in enumerate(incidents):
        c = inc.get("classification_dict", {})
        fm = c.get("failure_mode", "FM-000") or "FM-000"
        fm_buckets[fm].append(i)

    _log.debug("FM buckets: %s", {k: len(v) for k, v in sorted(fm_buckets.items()) if k != "FM-000"})

    # Load FM descriptions
    from ai_classification.core.failure_modes import FAILURE_MODES as _FM

    all_clusters: list[dict] = []
    used: set[int] = set()

    for fm, indices in fm_buckets.items():
        if fm == "FM-000" or len(indices) < MIN_CLUSTER_SIZE:
            continue
        members = [incidents[i] for i in indices]
        worst_sev = _worst_severity(members)
        top_sys, top_svc = _dominant_labels(members)
        cid = hashlib.md5(fm.encode()).hexdigest()[:12]
        fm_entry = _FM.get(fm)
        fm_name = FM_AR_LABELS.get(fm) or (fm_entry[0] if fm_entry else fm)
        cluster_incidents = []
        for idx, inc in zip(indices, members):
            # Compute similarity to cluster centroid (not self-similarity = 100%)
            centroid_sim = 100.0
            if len(indices) > 1:
                member_sims = [float(sim[idx, other]) for other in indices if other != idx]
                if member_sims:
                    centroid_sim = round(sum(member_sims) / len(member_sims) * 100, 1)
            di = {
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
            }
            cluster_incidents.append(di)
        cluster = {
            "cluster_id": cid,
            "name": fm,
            "failure_mode_desc": fm_name,
            "affected_system": top_sys,
            "affected_service": top_svc,
            "worst_severity": worst_sev,
            "count": len(members),
            "summary": f"{len(members)} tickets sharing failure mode {fm}",
            "pruned": [],
            "coherence": cluster_coherence(members, sim, indices),
            "incidents": cluster_incidents,
        }
        all_clusters.append(cluster)
        used.update(indices)
        _log.info("FM cluster: %s — %d tickets, %s", fm, len(members), worst_sev)

    # Phase 2: Embedding-based clustering for FM-000 (unclassified) leftovers
    leftover = [i for i in range(n) if i not in used]
    if len(leftover) >= MIN_CLUSTER_SIZE:
        prefix_buckets: dict[str, list[int]] = {}
        for i in leftover:
            cs = _extract_canonical_statement(incidents[i])
            prefix = cs.split(":")[0].strip() if ":" in cs else cs[:20]
            prefix_buckets.setdefault(prefix, []).append(i)
        _log.debug("FM-000 prefix buckets: %s", {k: len(v) for k, v in sorted(prefix_buckets.items())})
        for px, bx in prefix_buckets.items():
            if len(bx) < MIN_CLUSTER_SIZE:
                continue
            bx_clusters, bx_used = _cluster_pass(
                incidents, sim, SIMILARITY_THRESHOLD, bx
            )
            all_clusters.extend(bx_clusters)
            used.update(bx_used)
        # Cross-bucket pass on remaining leftovers
        still_left = [i for i in leftover if i not in used]
        if len(still_left) >= MIN_CLUSTER_SIZE:
            cross_clusters, cross_used = _cluster_pass(
                incidents, sim, SIMILARITY_THRESHOLD + 0.05, still_left
            )
            all_clusters.extend(cross_clusters)
            used.update(cross_used)

    all_clusters.sort(key=lambda c: c["count"], reverse=True)
    _log.info("build_clusters — %d active incidents, %d clusters found (phase1=%d, phase2=%s)",
              n, len(all_clusters), sum(1 for c in all_clusters if (c["name"] or "").startswith("FM-") and c["name"] != "FM-000"),
              "active" if len(leftover) >= MIN_CLUSTER_SIZE else "skipped")
    return {"total_incidents": n, "clusters": all_clusters, "subsystem_summary": subsystem_summary}


# ── Clustering pass ───────────────────────────────────────────────────────


# Build a similarity graph over `candidate_indices` at `threshold`, validate
# each dense component with the LLM, return accepted clusters + used indices
def _cluster_pass(
    incidents: tuple, sim: np.ndarray, threshold: float, candidate_indices: list[int],
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
        if len(comp) < MIN_CLUSTER_SIZE:
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

        if len(member_pairs) < MIN_CLUSTER_SIZE:
            continue

        # ── Emission floor ── reject clusters whose internal coherence is too low
        # Short signatures produce tighter vectors; a mean below 0.70 means the
        # group is held together by shared filler (e.g. all starting "Error")
        # rather than real semantic similarity.
        member_indices = sorted(m for m, _ in member_pairs)
        intra_pairs = 0
        intra_sum = 0.0
        for a in range(len(member_indices)):
            for b in range(a + 1, len(member_indices)):
                val = sim[member_indices[a], member_indices[b]]
                if val >= SIMILARITY_THRESHOLD:
                    intra_pairs += 1
                    intra_sum += val
        mean_intra = intra_sum / intra_pairs if intra_pairs > 0 else 0.0
        if mean_intra < 0.70:
            _log.info("Cluster rejected by emission floor (mean_intra=%.3f < 0.70) — %d tickets",
                      mean_intra, len(member_pairs))
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

