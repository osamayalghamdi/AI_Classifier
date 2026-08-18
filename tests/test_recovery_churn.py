"""Churn guard for the RECOVERY job — a ticket that fails recovery goes to
the manual review queue and is NEVER re-picked (no infinite retry loop).
The queue replaces the old in-memory `_retried_still_fallback` set: the
guard is now durable (DB table) instead of process-local.
"""

from ai_classification.seams.recovery import recovery_candidates, run_recovery


class FakeStore:
    """Minimal store stand-in: list_incidents + update_classification + queue."""

    def __init__(self, incidents):
        self._incidents = {i["id"]: dict(i) for i in incidents}
        self._queue = []
        self.calls = []

    def list_incidents(self):
        return list(self._incidents.values())

    def update_classification(self, incident_id, classification_json):
        self.calls.append(incident_id)
        import json
        self._incidents[incident_id]["classification_dict"] = json.loads(classification_json)

    def queue_list(self):
        return [{"incident_id": q} for q in self._queue]

    def queue_add(self, incident_id, reason=""):
        self._queue.append(incident_id)


def _failed_inc(i: int) -> dict:
    return {
        "id": f"i{i:04d}",
        "title": "مكة المكرمة" if i == 0 else "2026-2078944",  # content-poor
        "description": "",
        "classification_dict": {
            "service": "General / Unspecified",
            "reasoning": "Classification failed after 2 attempts. Last error: service selection failed",
        },
    }


def _patch_store(monkeypatch, store):
    """recovery.py lazily imports `store` from ai_classification.core.store —
    patch the singleton there."""
    from ai_classification.core import store as store_mod
    monkeypatch.setattr(store_mod, "store", store)


def _patch_classify(monkeypatch, fake_cls):
    """recovery.py lazily imports classify/PROMPT_VERSION from
    ai_classification.core.classifier — patch on the source module."""
    from ai_classification.core import classifier as classifier_mod
    monkeypatch.setattr(classifier_mod, "classify", lambda t, d: fake_cls)
    monkeypatch.setattr(classifier_mod, "PROMPT_VERSION", "p")


def _patch_settings(monkeypatch, llm_model="m"):
    """recovery.py lazily imports settings from ai_classification.config —
    patch on the source module."""
    from ai_classification import config as config_mod
    fake = type("S", (), {"llm_model": llm_model})()
    monkeypatch.setattr(config_mod, "settings", fake)


def test_recovery_skips_queued_tickets(monkeypatch):
    """A ticket already in the manual-review queue is never re-picked."""
    store = FakeStore([_failed_inc(0), _failed_inc(1)])
    _patch_store(monkeypatch, store)
    store._queue.append("i0000")  # exhausted — manual review owns it

    assert [c["id"] for c in recovery_candidates()] == ["i0001"]


def test_recovery_failure_queues_ticket(monkeypatch):
    """A re-classify that fails again goes to the manual queue — churn stops."""
    store = FakeStore([_failed_inc(0)])
    _patch_store(monkeypatch, store)

    class Boom(Exception):
        pass

    from ai_classification.core import classifier as classifier_mod
    monkeypatch.setattr(classifier_mod, "classify", lambda t, d: (_ for _ in ()).throw(Boom("still broken")))
    monkeypatch.setattr(classifier_mod, "PROMPT_VERSION", "p")
    _patch_settings(monkeypatch)

    stats = run_recovery()
    assert stats["failed"] == 1
    assert stats["queued"] == 1
    assert "i0000" in store._queue
    assert recovery_candidates() == []  # churn stopped — no re-pick loop


def test_recovery_success_not_queued(monkeypatch):
    """A ticket that DID recover is NOT queued (and no longer a candidate)."""
    store = FakeStore([_failed_inc(0)])
    _patch_store(monkeypatch, store)

    class FakeCls:
        model_version = "m"
        prompt_version = "p"
        service = "pilgrim groups - Nusuk Masar Haj.Issue Permits"
        canonical_statement = "real issue"

        def model_dump_json(self):
            return '{"service": "pilgrim groups - Nusuk Masar Haj.Issue Permits", "canonical_statement": "real issue"}'

    _patch_classify(monkeypatch, FakeCls())
    _patch_settings(monkeypatch)

    stats = run_recovery()
    assert stats["recovered"] == 1
    assert store._queue == []            # success = clean, never queued
    assert recovery_candidates() == []   # and not a candidate either
