"""Guards for the connector's payload contract.

These tests exist because the ingest payload used to be built twice (once in
`submit`, once in `backfill`) and the two copies had already drifted: backfill
silently dropped `affected_system` and `attachments`. Anything that would let
that happen again fails here.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from integrations.smax.classifier_client import incident_payload
from integrations.smax.smax_models import Incident

_WHEN = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

_BASE = Incident(
    source_reference="T-1",
    title="Pump leak",
    description="Coolant under pump P-203",
    status="active",
    created_at=_WHEN,
    updated_at=_WHEN,
)


def _incident(**overrides) -> Incident:
    return replace(_BASE, **overrides)


def test_payload_carries_the_optional_fields_when_present():
    payload = incident_payload(
        _incident(
            affected_system="Operations",
            attachments=[{"name": "photo.png", "url": "https://smax/x.png"}],
        )
    )
    assert payload["affected_system"] == "Operations"
    assert payload["attachments"] == [{"name": "photo.png", "url": "https://smax/x.png"}]


def test_payload_omits_them_when_empty():
    payload = incident_payload(_incident())
    assert "affected_system" not in payload
    assert "attachments" not in payload


def test_payload_datetimes_are_iso_or_none():
    payload = incident_payload(_incident(created_at=None, updated_at=None))
    assert payload["created_at"] is None and payload["updated_at"] is None
    assert incident_payload(_incident())["created_at"].startswith("2025-01-02T03:04:05")


def test_submit_and_backfill_send_identical_bodies(classifier_client, fake_classifier):
    """The regression this refactor removed: one incident, two paths, same body."""
    incident = _incident(
        affected_system="Operations",
        attachments=[{"name": "photo.png", "content_base64": "aGk="}],
    )

    classifier_client.submit(incident)
    classifier_client.backfill([incident])

    single = next(body for method, path, _h, body in fake_classifier.requests
                  if method == "POST" and path == "/api/v1/incidents")
    batched = next(body for method, path, _h, body in fake_classifier.requests
                   if method == "POST" and path == "/api/v1/backfill")["incidents"][0]
    assert batched == single


def test_only_contract_fields_are_sent(classifier_client, fake_classifier):
    """The API rejects unknown fields (422) — never invent one here."""
    allowed = {
        "source_reference", "title", "description", "status",
        "created_at", "updated_at", "affected_system", "attachments",
    }
    classifier_client.submit(_incident(attachments=[{"name": "a.png", "url": "u"}]))
    body = next(b for m, p, _h, b in fake_classifier.requests
                if m == "POST" and p == "/api/v1/incidents")
    assert set(body) <= allowed
