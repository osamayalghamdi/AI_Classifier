"""Two different tickets with the same digit-blanked text must both survive.

Regression for the ticket-ID loss: the content-hash gate in `persistence.py`
documented itself as "runs when there's NO source_ticket_id" but never checked
it, and the seams pipeline gated on the hash unconditionally — so a second
SMAX ticket whose text only differed in numbers was merged into the first and
its reference was never recorded. Later status updates for that ticket (the
webhook looks incidents up by source_ticket_id) then matched nothing.

Both tests use the real store/DB (isolated `ai_incidents_test`); the LLM is
stubbed — the subject here is dedupe, not classification.
"""

from __future__ import annotations

import pytest

import ai_classification.services.classify.classifier as classifier
from ai_classification.domain.models import ClassificationResult
from ai_classification.domain.taxonomy import AffectedSystem, Category, IncidentType, Severity, Urgency
from ai_classification.seams import pipeline
from ai_classification.seams.port import Incident
from ai_classification.shared.store import store

REF_A = "RAWDHA-1"
REF_B = "RAWDHA-2"
TITLE = "RAWDHA ERROR"
# Same text once digits are blanked → identical content hash on purpose.
DESC_A = "sensor alert 10% on the reading"
DESC_B = "sensor alert 20% on the reading"


def _result(**overrides) -> ClassificationResult:
    defaults = dict(
        affected_system=AffectedSystem.nusuk_masar_haj,
        service="System/Application - Nusuk Masar Haj",
        incident_type=IncidentType.degradation,
        severity=Severity.major,
        urgency=Urgency.high,
        category=Category.software,
        confidence="high",
        reasoning="test",
        canonical_statement="RAWDHA reading deviates from the expected value.",
        signature="operators cannot trust the RAWDHA reading because it deviates",
    )
    defaults.update(overrides)
    return ClassificationResult(**defaults)


@pytest.fixture()
def stubbed_llm(monkeypatch):
    monkeypatch.setattr(classifier, "classify", lambda *a, **k: _result())
    store.setup()
    created: list[str] = []

    def _ingest(ref: str, description: str) -> dict:
        outcome = pipeline.persist_result(
            pipeline.process_incident(
                Incident(source_reference=ref, title=TITLE, description=description)
            )
        )
        if outcome.get("incident_id"):
            created.append(outcome["incident_id"])
        return outcome

    yield _ingest

    # leave the shared test DB as we found it
    with store._getconn() as conn, conn.cursor() as cur:
        for incident_id in set(created):
            cur.execute("DELETE FROM incidents WHERE id = %s", (incident_id,))
        conn.commit()


def test_second_ticket_with_same_text_keeps_its_own_id(stubbed_llm):
    """Different ticket IDs + identical (digit-blanked) text → BOTH stored."""
    first = stubbed_llm(REF_A, DESC_A)
    assert first["action"] == "new"

    second = stubbed_llm(REF_B, DESC_B)
    assert second["action"] == "new", (
        "the second ticket was merged into the first: its reference would be lost"
    )
    assert second["incident_id"] != first["incident_id"]

    # This is the consequence that matters: SMAX status updates arrive by
    # ticket reference and must find the incident.
    assert store.get_incident_by_source_ticket_id(REF_A) is not None
    assert store.get_incident_by_source_ticket_id(REF_B) is not None


def test_same_ticket_twice_is_still_idempotent(stubbed_llm):
    """The same reference re-delivered stays a no-op (no duplicate row)."""
    first = stubbed_llm(REF_A, DESC_A)
    again = stubbed_llm(REF_A, DESC_A)

    assert again["action"] == "seen"
    assert again["incident_id"] == first["incident_id"]
