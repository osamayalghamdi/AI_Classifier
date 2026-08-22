"""Classify-and-persist orchestration: classification_log / taxonomy-gap
store hooks, content hash, classify_and_store, classify_batch.
"""

import json
import logging
import re

from datetime import datetime, timezone

from pydantic import TypeAdapter

from ai_classification.api.schemas import ClassifyBatchResponse, ClassifyResponse
from ai_classification.domain.models import ClassificationResult, SimilarOpenIncident


_log = logging.getLogger(__name__)


# ── classification_log / taxonomy-gap store hooks ─────────────────────
# Worker B implements the store methods in parallel (signatures fixed in the
# v3 contract). getattr-style calls keep this worktree importable until the
# merge; failures are logged, never raised.


def _log_classification(
    incident_ref: str | None,
    stage: str,
    raw: str,
    extra: dict | None = None,
) -> None:
    """Record an LLM decision to classification_log (best-effort)."""
    from ai_classification.services.classify import classifier as classifier_mod

    store = classifier_mod.store
    settings = classifier_mod.settings
    PROMPT_VERSION = classifier_mod.PROMPT_VERSION
    try:
        fn = getattr(store, "log_classification", None)
        if fn is None:
            _log.debug("store.log_classification not available — skipping log (stage=%s)", stage)
            return
        fn(incident_ref, stage, PROMPT_VERSION, settings.llm_model, raw, extra=extra)
    except Exception as e:
        _log.warning("log_classification failed (stage=%s): %s", stage, e)


def _record_taxonomy_gap(
    service: str,
    suggested_offering: str,
    incident_ref: str | None,
) -> None:
    """Record a taxonomy-gap row for a NONE_OF_THE_ABOVE abstention (best-effort)."""
    from ai_classification.services.classify import classifier as classifier_mod

    store = classifier_mod.store
    try:
        fn = getattr(store, "record_taxonomy_gap", None)
        if fn is None:
            _log.debug("store.record_taxonomy_gap not available — skipping gap record")
            return
        fn(service=service, suggested_offering=suggested_offering, incident_ref=incident_ref)
    except Exception as e:
        _log.warning("record_taxonomy_gap failed: %s", e)


# ── Content hash for analytics (not used for dedupe) ─────────────────────
# Stored on each incident for analytical queries but never gates creation.
# Digit blanking catches alerts that differ only by percentage/threshold value.


def content_hash(title: str, description: str) -> str:
    import hashlib
    text = f"{title} {description}".lower()
    text = re.sub(r"<[^>]+>", " ", text)       # strip HTML
    text = re.sub(r"\d+(\.\d+)?", "#", text)    # digits → placeholder
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ── Classify-and-persist orchestration ──────────────────────────────────


# Classify a single incident and save to store


def classify_and_store(
    title: str,
    description: str,
    extracted_text: str = "",
    documents: list[str] | None = None,
    assign_group: str = "",
    assignee: str = "",
    priority: str | None = None,
    status: str = "active",
    notes: str | None = None,
    discussion_history: list[dict] | None = None,
    escalation_info: str | None = None,
    completion_code: str | None = None,
    source_ticket_id: str = "",
    precomputed: ClassificationResult | None = None,
    affected_system: str | None = None,
) -> ClassifyResponse:
    from ai_classification.services.classify import classifier as classifier_mod

    store = classifier_mod.store
    settings = classifier_mod.settings
    classify = classifier_mod.classify
    content_hash = classifier_mod.content_hash
    PROMPT_VERSION = classifier_mod.PROMPT_VERSION

    _log.info("Classifying incident — title='%s', group='%s', priority=%s, ticket_id='%s'",
              title[:60], assign_group, priority, source_ticket_id)

    # ── ID-based dedupe ──
    # The ONLY deduplication mechanism: exact match on the originating
    # ticket ID. If this ticket ID was already processed, return the
    # existing incident unchanged (idempotent). Text similarity is
    # surfaced as informational only (similar_open_incidents) and must
    # never suppress creation of a new incident.
    if source_ticket_id:
        existing = store.get_incident_by_source_ticket_id(source_ticket_id)
        if existing is not None:
            _log.info("Ticket ID '%s' already processed → idempotent return of incident %s",
                      source_ticket_id, existing["id"][:8])
            cls_data = existing.get("classification_dict", {})
            result = TypeAdapter(ClassificationResult).validate_python(cls_data)
            embed_text = result.canonical_statement or f"{title} {description}"
            matches = store.find_similar(embed_text, extracted_text=extracted_text, classification=result)
            return ClassifyResponse(
                incident_id=existing["id"],
                incident_title=title,
                classification=result,
                similar_open_incidents=[
                    SimilarOpenIncident(
                        id=m.id,
                        title=m.title,
                        similarity=round(m.similarity, 4),
                        classification=m.classification,
                        canonical_statement=m.classification.canonical_statement,
                    )
                    for m in matches
                ],
            )

    # ── Content-hash dedupe gate ──
    # Runs when there's NO source_ticket_id (external feed: title +
    # description only). Exact duplicates (digit-blanked) within the recency
    # window increment occurrence_count and return the existing incident
    # instead of creating a new one. ID-based dedupe above takes priority
    # when a stable ticket ID IS present.
    h = content_hash(title, description)
    existing = store.get_incident_by_hash(h)
    if existing and existing.get("last_seen"):
        age = (datetime.now(timezone.utc) - existing["last_seen"]).total_seconds()
        if age < 86400 * 7:  # 7-day window
            _log.info("Content hash match → incrementing occurrence_count for %s (seen %d×)",
                      existing["id"][:8], existing["occurrence_count"])
            store.increment_occurrence(existing["id"])
            # Return the existing classification
            cls_data = json.loads(existing["classification_json"]) if isinstance(existing["classification_json"], str) else existing["classification_json"]
            result = TypeAdapter(ClassificationResult).validate_python(cls_data)
            embed_text = result.canonical_statement or f"{title} {description}"
            matches = store.find_similar(embed_text, extracted_text=extracted_text, classification=result)
            return ClassifyResponse(
                incident_id=existing["id"],
                incident_title=title,
                classification=result,
                similar_open_incidents=[
                    SimilarOpenIncident(
                        id=m.id,
                        title=m.title,
                        similarity=round(m.similarity, 4),
                        classification=m.classification,
                        canonical_statement=m.classification.canonical_statement,
                    )
                    for m in matches
                ],
            )

    # ── v3: the incident id is generated BEFORE classifying so every
    # classification_log entry (triage, stages, verification) carries it.
    incident_id = store.generate_id()

    result = precomputed if precomputed is not None else classify(
        title, description, incident_ref=incident_id, affected_system=affected_system
    )

    # ── Provenance (report fix 2026-08-19): every stored classification
    # carries which model + prompt version produced it. The seams pipeline
    # sets these; the direct API path must too, or drift can't be debugged.
    if not result.model_version:
        result.model_version = settings.llm_model
    if not result.prompt_version:
        result.prompt_version = PROMPT_VERSION

    # ── Severity→priority mapping (only when the caller did not supply one) ──
    _priority_map = {"Critical": "critical", "Major": "high", "Minor": "medium", "Cosmetic": "low"}
    if not priority:
        sev = result.severity.value if hasattr(result.severity, "value") else result.severity
        priority = _priority_map.get(sev, "medium")

    embed_text = result.canonical_statement or f"{title} {description}"

    matches = store.find_similar(embed_text, extracted_text=extracted_text, classification=result)

    _log.info("Classify result — id=%s, system=%s, service=%s, severity=%s, confidence=%s, dupes=%d",
              incident_id, result.affected_system, result.service,
              result.severity, result.confidence, len(matches))
    _log.debug("Canonical: %s", result.canonical_statement[:120] if result.canonical_statement else "(none)")
    if matches:
        _log.info("Similar open incidents found — %d related incidents", len(matches))
        for m in matches:
            _log.debug("  Similar: %s — %.1f%% — %s", m.id, m.similarity * 100, m.title[:60])

    _save_kwargs: dict = dict(
        documents=documents or [],
        assign_group=assign_group,
        assignee=assignee,
        priority=priority,
        status=status,
        notes=notes,
        discussion_history=discussion_history or [],
        escalation_info=escalation_info,
        completion_code=completion_code,
        content_hash=h,
        source_ticket_ids=[source_ticket_id] if source_ticket_id else [incident_id],
    )
    # v3: persist ticket_kind/classification_status to the dedicated columns.
    _save_kwargs["ticket_kind"] = result.ticket_kind.value
    _save_kwargs["classification_status"] = result.classification_status
    store.save_incident(
        incident_id, title, description, result, extracted_text, **_save_kwargs
    )

    _log.info("Incident %s classified — system=%s, severity=%s, confidence=%s, dupes=%d",
              incident_id, result.affected_system, result.severity, result.confidence, len(matches))

    # ── Flow A (v2 persistent clustering): LLM-decided assignment to an
    # existing active cluster, in a BACKGROUND task — slow inference must
    # never delay the classify response (ingestion stays synchronous).
    if settings.cluster_assign_on_arrival:
        try:
            from ai_classification.services.cluster.persistent import assign_in_background
            assign_in_background(incident_id)
        except Exception as exc:  # noqa: BLE001 — clustering must not break ingestion
            _log.warning("Flow A background assignment failed to start: %s", exc)

    return ClassifyResponse(
        incident_title=title,
        classification=result,
        incident_id=incident_id,
        similar_open_incidents=[
            SimilarOpenIncident(
                id=m.id,
                title=m.title,
                similarity=round(m.similarity, 4),
                classification=m.classification,
                canonical_statement=m.classification.canonical_statement,
            )
            for m in matches
        ],
    )
# Classify multiple incidents in batch


def classify_batch(incidents: list[dict]) -> ClassifyBatchResponse:
    results = []
    failed = 0
    for inc in incidents:
        try:
            r = classify_and_store(
                inc.get("title", ""),
                inc.get("description", ""),
                inc.get("extracted_text", ""),
                documents=inc.get("documents"),
                assign_group=inc.get("assign_group", ""),
                assignee=inc.get("assignee", ""),
                priority=inc.get("priority"),
                status=inc.get("status", "active"),
                notes=inc.get("notes"),
                discussion_history=inc.get("discussion_history"),
                escalation_info=inc.get("escalation_info"),
                completion_code=inc.get("completion_code"),
                affected_system=inc.get("affected_system"),
            )
            results.append(r)
        except Exception as e:
            _log.error("Batch classify failed for '%s': %s", inc.get("title", "")[:40], e)
            failed += 1
    _log.info("Batch classify — %d/%d succeeded", len(results), len(incidents))
    return ClassifyBatchResponse(results=results, total=len(incidents), failed=failed)
