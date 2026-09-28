"""Provenance model id — the model that ACTUALLY served the call.

Regression (persistence.py L45 / L195): model_version and classification_log
recorded settings.llm_model (the legacy id) while call_llm serves the
request with settings.active_classifier_model.model_id — provenance could
name a different (even disabled) model than the one that produced the
classification, which makes drift undebuggable.
"""

import types

import pytest

import ai_classification.services.classify.classifier as classifier_mod
from ai_classification.domain.models import ClassificationResult
from ai_classification.domain.taxonomy import AffectedSystem, Category
from ai_classification.services.classify import persistence as persist_mod
from ai_classification.shared.config import ModelEntry, settings


ACTIVE = ModelEntry(name="QWEN3_6", role="classifier", provider="elm", enabled=True,
                    model_id="openai/qwen3.6", api_base="http://llms.elm.sa/v1",
                    api_key="k")


def _settings(**overrides):
    d = {k: v for k, v in settings.__dict__.items() if not k.startswith("_")}
    d.update(overrides)
    return types.SimpleNamespace(**d)


class FakeStore:
    """Minimal store double (only the methods persistence calls)."""

    def __init__(self):
        self.logged = []
        self.saved = {}

    def log_classification(self, incident_ref, stage, prompt_version, model,
                           raw_verdict, extra=None):
        self.logged.append({"incident_ref": incident_ref, "stage": stage,
                            "prompt_version": prompt_version, "model": model,
                            "raw": raw_verdict, "extra": extra})

    def generate_id(self):
        return "incident-da45h"

    def get_incident_by_source_ticket_id(self, ticket_id):
        return None

    def get_incident_by_hash(self, h):
        return None

    def find_similar(self, embed_text, extracted_text="", classification=None):
        return []

    def save_incident(self, incident_id, title, description, classification,
                      extracted_text="", **kwargs):
        self.saved = {"id": incident_id, "classification": classification, **kwargs}
        return incident_id


@pytest.fixture
def diverged(monkeypatch):
    """llm_model and the served (active) model deliberately DIFFER."""
    monkeypatch.setattr(classifier_mod, "settings",
                        _settings(llm_model="legacy/old-id", active_classifier_model=ACTIVE))
    store = FakeStore()
    monkeypatch.setattr(classifier_mod, "store", store)
    return store


def test_prefers_active_classifier_model():
    s = _settings(llm_model="legacy/old-id", active_classifier_model=ACTIVE)
    assert persist_mod._provenance_model(s) == "openai/qwen3.6"


def test_falls_back_to_llm_model_when_none_enabled():
    s = _settings(llm_model="legacy/old-id", active_classifier_model=None)
    assert persist_mod._provenance_model(s) == "legacy/old-id"


def test_log_classification_records_the_served_model(diverged):
    persist_mod._log_classification("ref-1", "stage2", '{"service": "x"}', extra={"a": 1})
    assert diverged.logged[0]["model"] == "openai/qwen3.6"


def test_classify_and_store_stamps_the_served_model(diverged, monkeypatch):
    result = ClassificationResult(
        affected_system=AffectedSystem.other, service="General / Unspecified",
        category=Category.other, confidence="low", signature="s", reasoning="r",
        canonical_statement="c",
    )
    monkeypatch.setattr(classifier_mod, "classify", lambda *a, **k: result)
    monkeypatch.setattr(classifier_mod, "settings",
                        _settings(llm_model="legacy/old-id", active_classifier_model=ACTIVE,
                                  cluster_assign_on_arrival=False))
    resp = persist_mod.classify_and_store("Email down", "Cannot send emails")
    assert resp.classification.model_version == "openai/qwen3.6"
    assert diverged.saved["classification"].model_version == "openai/qwen3.6"
