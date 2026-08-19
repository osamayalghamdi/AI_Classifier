"""Shared clustering helpers — the survivors of the v2 LLM-first refactor.

The stateless rebuild (Phase-1 offering exact-match, Phase-2 embedding
graph clustering, volume-adaptive sensitivity, verdict/name fingerprint
caches, the 5-minute rebuild loop) was DELETED in the v2 refactor
(feat/llm-first-clustering): clusters are now persistent DB rows decided
by the LLM (services/cluster/persistent.py — Flows A/B/C/D).

What stays here:
  * subsystem rollup        — a different question ("is this subsystem
    having a bad day"), pure SQL-free GROUP BY over active incidents
  * severity / label / canonical-statement helpers — shared by the
    persistent report builder and the audit flows

Pipeline position: 30_cluster — helpers + subsystem rollup."""

import logging
from collections import defaultdict

_log = logging.getLogger(__name__)

# ── v3 ticket-kind filter ──────────────────────────────────────────────
# Only incident / service_request tickets feed the subsystem rollup and
# cluster assignment. Everything else (administrative, inquiry,
# feature_request, test, content_thin) is classified for routing only and
# must never pollute rollups or clusters. The `ticket_kind` COLUMN wins
# when present; fall back to the value inside classification_dict
# (pre-column rows); absent entirely (legacy rows / test fixtures) →
# treat as incident, the pre-v3 behavior.
_CLUSTER_TICKET_KINDS = ("incident", "service_request")


def _kind_of(inc: dict) -> str:
    kind = inc.get("ticket_kind")
    if not kind:
        kind = (inc.get("classification_dict") or {}).get("ticket_kind")
    return kind or "incident"


# ── Subsystem rollup ──────────────────────────────────────────────────────


# Count active incidents per (affected_system, service) — no embeddings, no LLM.
# Complements the persistent clusters; doesn't replace them.
def _subsystem_rollup(active_incidents: list[dict]) -> list[dict]:
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for inc in active_incidents:
        if _kind_of(inc) not in _CLUSTER_TICKET_KINDS:
            continue
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


# ── Incident helpers ──────────────────────────────────────────────────────


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
