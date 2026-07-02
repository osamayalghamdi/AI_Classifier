"""Tests for service — orchestration between classifier and store."""

from dataclasses import replace

import pytest

import ai_classification.service as service
from ai_classification.models import ClassificationResult, RerankItem
from ai_classification.schemas import AffectedSystem, IncidentType, Severity, Urgency, Category


def _make_result(**overrides) -> ClassificationResult:
    defaults = dict(
        affected_system=AffectedSystem.crm,
        service="Customer Portal",
        incident_type=IncidentType.degradation,
        severity=Severity.major,
        urgency=Urgency.high,
        category=Category.software,
        confidence="high",
        reasoning="test",
    )
    defaults.update(overrides)
    return ClassificationResult(**defaults)


def _make_candidate(result: ClassificationResult, id_="abc123"):
    return {
        "id": id_,
        "title": "Similar incident",
        "description": "d",
        "classification_json": result.model_dump_json(),
        "cosine_score": 0.5,
    }


# ── _llm_matches ─────────────────────────────────────────────────────


class TestLlmMatches:
    def test_no_candidates_returns_empty_without_calling_llm(self, monkeypatch):
        monkeypatch.setattr(service.store, "find_candidates", lambda *a, **k: [])
        called = []
        monkeypatch.setattr(service, "llm_rerank_similar", lambda *a, **k: called.append(1) or [])
        result = service._llm_matches("title", "desc", "", _make_result())
        assert result == []
        assert called == []

    def test_maps_ranked_items_to_similar_match(self, monkeypatch):
        result_cls = _make_result()
        candidates = [_make_candidate(result_cls)]
        monkeypatch.setattr(service.store, "find_candidates", lambda *a, **k: candidates)
        monkeypatch.setattr(
            service, "llm_rerank_similar",
            lambda *a, **k: [RerankItem(id="abc123", similarity=87, reasoning="same cause")],
        )
        matches = service._llm_matches("title", "desc", "", result_cls)
        assert len(matches) == 1
        assert matches[0].id == "abc123"
        assert matches[0].title == "Similar incident"
        assert matches[0].similarity == pytest.approx(0.87)
        assert matches[0].reasoning == "same cause"

    def test_llm_failure_returns_empty_list(self, monkeypatch):
        result_cls = _make_result()
        monkeypatch.setattr(service.store, "find_candidates", lambda *a, **k: [_make_candidate(result_cls)])

        def boom(*a, **k):
            raise ValueError("LLM exploded")

        monkeypatch.setattr(service, "llm_rerank_similar", boom)
        matches = service._llm_matches("title", "desc", "", result_cls)
        assert matches == []

    def test_ignores_ranked_item_with_unknown_id(self, monkeypatch):
        result_cls = _make_result()
        monkeypatch.setattr(service.store, "find_candidates", lambda *a, **k: [_make_candidate(result_cls)])
        monkeypatch.setattr(
            service, "llm_rerank_similar",
            lambda *a, **k: [RerankItem(id="does-not-exist", similarity=90)],
        )
        matches = service._llm_matches("title", "desc", "", result_cls)
        assert matches == []


# ── find_matches routing ────────────────────────────────────────────


class TestFindMatches:
    def test_routes_to_cosine_when_flag_off(self, monkeypatch):
        monkeypatch.setattr(service, "settings", replace(service.settings, use_llm_reranking=False))
        monkeypatch.setattr(service, "_cosine_matches", lambda *a, **k: "cosine")
        monkeypatch.setattr(service, "_llm_matches", lambda *a, **k: "llm")
        assert service.find_matches("t", "d", "", _make_result()) == "cosine"

    def test_routes_to_llm_when_flag_on(self, monkeypatch):
        monkeypatch.setattr(service, "settings", replace(service.settings, use_llm_reranking=True))
        monkeypatch.setattr(service, "_cosine_matches", lambda *a, **k: "cosine")
        monkeypatch.setattr(service, "_llm_matches", lambda *a, **k: "llm")
        assert service.find_matches("t", "d", "", _make_result()) == "llm"
