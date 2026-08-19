"""SEAMS pipeline: ONE entry function, normalized incident in, result out.

The pipeline NEVER writes to the store itself — persistence is the
separate, skippable, dry-run-capable step `persist_result`. Webhook,
polling, batch and manual ingestion are thin callers of process_incident.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .port import Incident, PipelineResult

_log = logging.getLogger(__name__)


def process_incident(incident: Incident) -> PipelineResult:
    """Classify one normalized incident and return a result OBJECT.

    Read-only: dedupe lookups + LLM classification + similarity search.
    No writes. Errors are captured in result.error, never raised.
    """
    # Lazy imports — classifier imports store and store imports sync, so a
    # top-level import here would be circular.
    from ai_classification.services.classify.classifier import PROMPT_VERSION, classify, content_hash
    from ai_classification.shared.store import store
    from ai_classification.shared.config import settings

    processed_at = datetime.now(timezone.utc)
    title = incident.title or ""
    description = incident.description or ""
    h = content_hash(title, description)

    # ── Idempotency gate (read-only): same content → not new ──
    existing = store.get_incident_by_hash(h)
    if existing is not None:
        cls = _existing_classification(existing)
        confidence = getattr(cls, "confidence", "") if cls is not None else ""
        return PipelineResult(
            source_reference=incident.source_reference,
            title=title,
            description=description,
            is_new=False,
            incident_id=existing.get("id"),
            classification=cls,
            similar_tickets=[],
            suggestions=[],
            confidence=confidence,
            model_version=getattr(cls, "model_version", "") if cls is not None else "",
            prompt_version=getattr(cls, "prompt_version", "") if cls is not None else "",
            processed_at=processed_at,
            status=incident.status,
        )

    # ── New content → classify (pure, read-only) ──
    try:
        cls = classify(title, description, affected_system=incident.affected_system or None)
    except Exception as exc:  # noqa: BLE001 — pipeline must return, not raise
        _log.error("Seams: classify failed for %s: %s", incident.source_reference, exc)
        return PipelineResult(
            source_reference=incident.source_reference,
            title=title,
            description=description,
            is_new=False,
            incident_id=None,
            classification=None,
            similar_tickets=[],
            suggestions=[],
            confidence="",
            model_version=settings.llm_model,
            prompt_version="",
            processed_at=processed_at,
            status=incident.status,
            error=str(exc),
        )

    # Provenance — model identity + prompt version ride on the result.
    cls.model_version = settings.llm_model
    cls.prompt_version = PROMPT_VERSION

    embed_text = cls.canonical_statement or f"{title} {description}"
    try:
        matches = store.find_similar(embed_text, classification=cls)
    except Exception:  # noqa: BLE001 — similarity is best-effort
        matches = []
    similar_tickets = [
        {"id": m.id, "title": m.title, "similarity": round(m.similarity, 4)}
        for m in matches
    ]
    return PipelineResult(
        source_reference=incident.source_reference,
        title=title,
        description=description,
        is_new=True,
        incident_id=None,
        classification=cls,
        similar_tickets=similar_tickets,
        suggestions=[m["title"] for m in similar_tickets[:3]],
        confidence=cls.confidence,
        model_version=cls.model_version,
        prompt_version=cls.prompt_version,
        processed_at=processed_at,
        status=incident.status,
    )


def persist_result(result: PipelineResult, *, dry_run: bool = False) -> dict:
    """Separate write-back step: persist a pipeline result to the store.

    Skippable (callers may drop it), dry-run capable. Mirrors the previous
    sync behavior exactly:
      - new   → classify_and_store (content-hash gate stays race-safe)
      - seen  → increment_occurrence + status propagation (no LLM call)
    """
    from ai_classification.services.classify.classifier import classify_and_store
    from ai_classification.shared.store import store

    if result.error:
        return {"action": "skipped", "reason": result.error}
    if dry_run:
        return {
            "dry_run": True,
            "action": "new" if result.is_new else "seen",
            "incident_id": result.incident_id,
            "source_reference": result.source_reference,
        }

    if result.is_new:
        resp = classify_and_store(
            result.title,
            result.description,
            precomputed=result.classification,
        )
        return {
            "action": "new",
            "incident_id": resp.incident_id,
            "source_reference": result.source_reference,
        }

    # Seen → dedupe semantics: +1 occurrence, status-only propagation.
    if result.incident_id:
        store.increment_occurrence(result.incident_id)
        # Status propagation mirrors the legacy sync mapping verbatim.
        local_status = (
            "active"
            if result.status in ("open", "in_progress", "third_party")
            else "resolved"
        )
        current = store.get_incident(result.incident_id)
        if current and current.get("status") != local_status:
            store.set_status(result.incident_id, result.status)
    return {
        "action": "seen",
        "incident_id": result.incident_id,
        "source_reference": result.source_reference,
    }


def process_batch(incidents: list[Incident]) -> list[PipelineResult]:
    """Thin batch caller — one normalized incident at a time."""
    return [process_incident(inc) for inc in incidents]


def manual_process(ref: str, source, *, persist: bool = True, dry_run: bool = False) -> dict:
    """Thin manual caller: fetch one ticket by reference → process → persist."""
    incident = source.fetch_ticket(ref)
    result = process_incident(incident)
    outcome = persist_result(result, dry_run=dry_run) if persist else {"action": "no-persist"}
    return {"result": result, "persist": outcome}


def _existing_classification(existing: dict):
    """Rebuild a ClassificationResult from a stored row (best-effort)."""
    from pydantic import TypeAdapter

    from ai_classification.services.classify.classifier import ClassificationResult

    cls_data = existing.get("classification_dict")
    if not cls_data:
        return None
    try:
        return TypeAdapter(ClassificationResult).validate_python(cls_data)
    except Exception:  # noqa: BLE001 — stored data may predate schema
        return None
