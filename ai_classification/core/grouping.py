"""Graph-based grouping over incident embeddings, validated by an LLM.

Used by the /api/reports endpoint to propose clusters from similarity.
The math (cosine similarity + graph communities) proposes candidate groups;
the LLM validator confirms coherence and prunes outliers — embeddings
propose, the LLM disposes.

Single similarity threshold, wide enough for recall on this embedding model's
compressed similarity range (bge-m3 tops out ~60% on this data — a "tight"
0.65 pass finds nothing). Precision is enforced downstream by the LLM
validator, not a second threshold. To keep that safe: any candidate group
larger than MAX_VALIDATOR_GROUP_SIZE is dropped outright rather than sent to
the LLM or trusted as-is — a graph component that big is almost certainly a
false merge of several real incidents, and a validator call over that many
tickets risks truncating mid-response.
"""

import json
import logging
from collections import defaultdict

import networkx as nx
import numpy as np
from litellm import completion

from ..config import settings
from .store import store

_log = logging.getLogger(__name__)

# ── Tunable parameters ──────────────────────────────────────────────────

SIMILARITY_THRESHOLD = 0.55        # intra-bucket threshold
MIN_CLUSTER_SIZE = 3               # smallest group to return
MIN_DENSITY = 0.4                  # chain filter
MAX_VALIDATOR_GROUP_SIZE = 15      # candidates larger than this are not validated

# ── Public API ──────────────────────────────────────────────────────────


# Load active incidents, build similarity graph, return dense clusters as JSON
def build_clusters(period: str = "daily") -> dict:
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

    # Step 1: Pre-group by canonical prefix (text before ":")
    # The LLM already normalizes this ("Payment checkout:", "File upload:", etc.)
    prefix_buckets: dict[str, list[int]] = {}
    for i, inc in enumerate(incidents):
        cs = _extract_canonical_statement(inc)
        prefix = cs.split(":")[0].strip() if ":" in cs else cs[:20]
        prefix_buckets.setdefault(prefix, []).append(i)

    _log.debug("Prefix buckets: %s", {k: len(v) for k, v in sorted(prefix_buckets.items())})

    # Step 2: Cluster within each bucket
    all_clusters: list[dict] = []
    used: set[int] = set()

    for prefix, indices in prefix_buckets.items():
        if len(indices) < MIN_CLUSTER_SIZE:
            continue  # too few to cluster, leave as singletons
        bucket_clusters, bucket_used = _cluster_pass(
            incidents, sim, SIMILARITY_THRESHOLD, indices
        )
        all_clusters.extend(bucket_clusters)
        used.update(bucket_used)

    # Step 3: Cross-bucket pass on leftovers (same root cause may span prefixes)
    leftover = [i for i in range(n) if i not in used]
    if len(leftover) >= MIN_CLUSTER_SIZE:
        cross_clusters, cross_used = _cluster_pass(
            incidents, sim, SIMILARITY_THRESHOLD + 0.05, leftover  # tighter for cross-bucket
        )
        all_clusters.extend(cross_clusters)
        used.update(cross_used)

    all_clusters.sort(key=lambda c: c["count"], reverse=True)
    _log.info("build_clusters — %d active incidents, %d clusters found", n, len(all_clusters))
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

        members = sorted(comp)
        cluster_incidents = [incidents[m] for m in members]

        # A candidate this large is almost certainly several real incidents
        # merged by the wide threshold, not one big incident. Don't send it
        # to the validator (risks truncating mid-response) and don't trust
        # it unvalidated either — drop it rather than fail open.
        if len(cluster_incidents) > MAX_VALIDATOR_GROUP_SIZE:
            _log.warning(
                "Candidate group too large to validate (%d > %d), dropping",
                len(cluster_incidents), MAX_VALIDATOR_GROUP_SIZE,
            )
            continue

        # LLM validation — math proposed this group, the LLM confirms
        # coherence and prunes outliers. Falls back to the math proposal
        # untouched if the call fails, is malformed, or the pruning floor
        # is exceeded (see validate_group's safeguards below).
        pruned: list[dict] = []
        verdict_name = None
        verdict_description = None

        validator_input = [
            {"id": inc["id"], "canonical_statement": _extract_canonical_statement(inc)}
            for inc in cluster_incidents
        ]
        verdict = validate_group(validator_input)

        if verdict is not None and not verdict.get("is_coherent", True):
            # LLM examined this group and found no real common issue — drop it,
            # don't surface a false pattern. Members stay unused (available to
            # a later, looser pass).
            _log.info("Cluster rejected by validator as incoherent (%d incidents)",
                      len(cluster_incidents))
            continue
        elif verdict is not None:
            keep_ids = set(verdict.get("keep", []))
            remove_reasons = {r["id"]: r["reason"] for r in verdict.get("remove", [])}
            pruned = [
                {"id": inc["id"], "title": inc["title"], "reason": remove_reasons[inc["id"]]}
                for inc in cluster_incidents if inc["id"] in remove_reasons
            ]
            cluster_incidents = [inc for inc in cluster_incidents if inc["id"] in keep_ids]
            # Rebuild members to match the pruned cluster
            cluster_ids = {inc["id"] for inc in cluster_incidents}
            members = sorted([m for m in members if incidents[m]["id"] in cluster_ids])
            verdict_name = verdict.get("name") or None
            verdict_description = verdict.get("description") or None

        if len(cluster_incidents) < MIN_CLUSTER_SIZE:
            continue

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
                        float(np.mean([sim[m, o] for o in members if o != m])) * 100, 1
                    ),
                    "description": inc["description"][:200],
                }
                for inc, m in zip(cluster_incidents, members)
            ],
        })

        used.update(members)

    return clusters, used


# ── Subsystem rollup ──────────────────────────────────────────────────────


# Count active incidents per (affected_system, service) — no embeddings, no LLM.
# Complements the duplicate-detection clusters above; doesn't replace them.
def _subsystem_rollup(active_incidents: list[dict]) -> list[dict]:
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for inc in active_incidents:
        try:
            data = json.loads(inc.get("classification", "{}"))
        except (json.JSONDecodeError, TypeError):
            data = {}
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
    try:
        data = json.loads(inc.get("classification", "{}"))
        return data.get("severity", "Minor")
    except (json.JSONDecodeError, TypeError):
        return "Minor"


# Parse canonical_statement from an incident's classification JSON, fall back to title
def _extract_canonical_statement(inc: dict) -> str:
    try:
        data = json.loads(inc.get("classification", "{}"))
        return data.get("canonical_statement") or inc.get("title", "")
    except (json.JSONDecodeError, TypeError):
        return inc.get("title", "")


# Return the highest severity across a list of incidents
def _worst_severity(incidents: list[dict]) -> str:
    ranks = [SEVERITY_RANK.get(_extract_severity(i), 0) for i in incidents]
    return SEVERITY_NAMES.get(max(ranks), "Minor")


# Find the most common affected_system and service in a cluster
def _dominant_labels(incidents: list[dict]) -> tuple[str, str]:
    systems: dict[str, int] = defaultdict(int)
    services: dict[str, int] = defaultdict(int)
    for inc in incidents:
        try:
            data = json.loads(inc.get("classification", "{}"))
            systems[data.get("affected_system", "Unknown")] += 1
            services[data.get("service", "Unknown")] += 1
        except (json.JSONDecodeError, TypeError):
            pass
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
- name: a short label (max 8 words) for the shared issue (omit if is_coherent is false).
- description: one-line description of what this incident is (omit if is_coherent is false).

Return format:
{
  "is_coherent": true/false,
  "keep": ["id1", "id2"],
  "remove": [
    {"id": "id3", "reason": "This is a login failure, the rest are checkout timeouts."}
  ],
  "name": "Short group name (max 8 words)",
  "description": "One-line description of what this incident is."
}"""


# Call the LLM via LiteLLM, same provider plumbing as core/classifier.py
def _call_llm(messages: list[dict]) -> str:
    kwargs: dict = dict(
        model=settings.llm_model,
        temperature=0.1,
        max_tokens=1500,
        messages=messages,
    )
    if settings.llm_api_base:
        kwargs["api_base"] = settings.llm_api_base
    if settings.llm_api_key:
        kwargs["api_key"] = settings.llm_api_key

    # Qwen3 thinks by default — disable for structured JSON output
    if "qwen3" in settings.llm_model.lower():
        kwargs["extra_body"] = {"reasoning": {"enabled": False}}

    resp = completion(**kwargs)
    content = resp.choices[0].message.content
    if not content or not content.strip():
        raise ValueError("Empty LLM response")
    return content


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
        content = _call_llm([
            {"role": "system", "content": VALIDATOR_PROMPT},
            {"role": "user", "content": user_msg},
        ])

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
    text = raw.strip()
    if text.startswith("```"):
        if text.startswith("```json"):
            text = text[7:]
        else:
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
    return json.loads(text.strip())
