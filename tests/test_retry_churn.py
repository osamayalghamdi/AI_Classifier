"""Churn guard for the retry worker — content-poor tickets must not be
re-picked every sweep."""

from ai_classification.seams import retry as retry_mod
from ai_classification.seams.retry import (
    retry_candidates, retry_unassigned, _retried_still_fallback,
    _FALLBACK_SERVICES,
)


class FakeStore:
    """Minimal store stand-in: list_incidents + update_classification."""

    def __init__(self, incidents):
        self._incidents = {i["id"]: dict(i) for i in incidents}
        self.calls = []

    def list_incidents(self):
        return list(self._incidents.values())

    def update_classification(self, incident_id, classification_json):
        self.calls.append(incident_id)
        import json
        self._incidents[incident_id]["classification_dict"] = json.loads(classification_json)


def _generic_inc(i: int) -> dict:
    return {
        "id": f"i{i:04d}",
        "title": "مكة المكرمة" if i == 0 else "2026-2078944",  # content-poor
        "description": "",
        "classification_dict": {"service": "General / Unspecified", "reasoning": ""},
    }


def _patch_store(monkeypatch, store):
    """retry.py lazily imports `store` from ai_classification.core.store —
    patch the singleton there."""
    from ai_classification.core import store as store_mod
    monkeypatch.setattr(store_mod, "store", store)


def _patch_classify(monkeypatch, fake_cls):
    """retry.py lazily imports classify/PROMPT_VERSION from
    ai_classification.core.classifier — patch on the source module."""
    from ai_classification.core import classifier as classifier_mod
    monkeypatch.setattr(classifier_mod, "classify", lambda t, d: fake_cls)


def _patch_settings(monkeypatch, llm_model="m"):
    """retry.py lazily imports settings from ai_classification.config —
    patch on the source module."""
    from ai_classification import config as config_mod
    fake = type("S", (), {"llm_model": llm_model})()
    monkeypatch.setattr(config_mod, "settings", fake)


def test_churn_guard_skips_already_retried_fallbacks(monkeypatch):
    """A ticket that was retried and STILL fell back must not appear in the
    next sweep's candidates (no infinite re-pick loop)."""
    store = FakeStore([_generic_inc(0), _generic_inc(1)])
    _patch_store(monkeypatch, store)
    _retried_still_fallback.clear()

    # First sweep: both are candidates.
    first = retry_candidates()
    assert len(first) == 2

    # Simulate one retry that still lands in the fallback.
    _retried_still_fallback.update(i["id"] for i in first)

    # Second sweep: none re-picked.
    second = retry_candidates()
    assert second == []


def test_retry_unassigned_marks_still_fallback(monkeypatch):
    """retry_unassigned must add still-fallback tickets to the guard set."""
    store = FakeStore([_generic_inc(0)])
    _patch_store(monkeypatch, store)
    _retried_still_fallback.clear()

    class FakeCls:
        model_version = "m"
        prompt_version = "p"
        service = "General / Unspecified"
        canonical_statement = ""

        def model_dump_json(self):
            return '{"service": "General / Unspecified", "canonical_statement": ""}'

    _patch_classify(monkeypatch, FakeCls())
    _patch_settings(monkeypatch)

    stats = retry_unassigned()
    assert stats["reclassified"] == 1
    assert len(_retried_still_fallback) == 1  # guarded now

    # Next sweep: candidate list empty (churn stopped).
    assert retry_candidates() == []


def test_retry_marks_successful_reclassifications_as_clean(monkeypatch):
    """A ticket that DID get a real service is NOT added to the guard —
    it's no longer a candidate anyway (real offering = assigned)."""
    store = FakeStore([_generic_inc(0)])
    _patch_store(monkeypatch, store)
    _retried_still_fallback.clear()

    class FakeCls:
        model_version = "m"
        prompt_version = "p"
        service = "pilgrim groups - Nusuk Masar Haj.Issue Permits"
        canonical_statement = "real issue"

        def model_dump_json(self):
            return '{"service": "pilgrim groups - Nusuk Masar Haj.Issue Permits"}'

    _patch_classify(monkeypatch, FakeCls())
    _patch_settings(monkeypatch)

    retry_unassigned()
    assert _retried_still_fallback == set()  # nothing guarded
    assert retry_candidates() == []          # and not a candidate either
