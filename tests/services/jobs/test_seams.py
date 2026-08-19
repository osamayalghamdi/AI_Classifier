"""Tests for SEAMS — the ticket-source port + ingestion pipeline.

Runs against the real test Postgres (see conftest.py) with mocked
embeddings and a mocked LLM classification. Covers: port contract,
config-driven selection, not-configured stub, pipeline result object
(no writes), idempotency (S6), and provenance (S6).
"""

from datetime import datetime, timedelta, timezone

import pytest

from ai_classification.shared.store import IncidentStore
from ai_classification.seams import (
    Incident,
    LocalFakeTicketSource,
    NotConfiguredError,
    PipelineResult,
    RealTicketingSource,
    get_ticket_source,
    manual_process,
    persist_result,
    process_incident,
)
from ai_classification.seams.pipeline import _existing_classification
from ai_classification.shared.config import settings as base_settings

from tests.conftest import TEST_PG_DATABASE
from tests.shared.test_incident_store import FixedVecModel, _make_result, _truncate


def _make_seams_store(monkeypatch, model=None):
    """IncidentStore (test DB, mocked embeddings) wired into the module
    singletons the pipeline uses via lazy imports."""
    import ai_classification.shared.store as store_mod
    import ai_classification.services.classify.classifier as classifier_mod

    s = _make_store(monkeypatch, model or FixedVecModel())
    # The pipeline lazy-imports `store` from these modules at call time, so
    # patching the module attributes redirects every write to the test DB.
    monkeypatch.setattr(store_mod, "store", s)
    monkeypatch.setattr(classifier_mod, "store", s)
    return s


def _make_store(monkeypatch, model):
    import ai_classification.shared.store as store_mod
    from dataclasses import replace

    monkeypatch.setattr(store_mod, "SentenceTransformer", lambda *a, **_: model)
    test_settings = replace(base_settings, pg_database=TEST_PG_DATABASE)
    monkeypatch.setattr(store_mod, "settings", test_settings)
    s = IncidentStore()
    s.setup()
    _truncate(s)
    return s


# ── S1/S2: port contract + implementations ────────────────────────────


class TestRealSource:
    def test_raises_not_configured_without_token(self):
        src = RealTicketingSource("http://localhost:8002", token="")
        dummy = PipelineResult(
            source_reference="x", title="", description="", is_new=False,
            incident_id=None, classification=None, similar_tickets=[],
            suggestions=[], confidence="", model_version="", prompt_version="",
            processed_at=datetime.now(timezone.utc), status="active",
        )
        for call in (lambda: src.fetch_ticket("x"),
                     lambda: src.fetch_attachments("x"),
                     lambda: src.list_changed(),
                     lambda: src.write_back(dummy)):
            with pytest.raises(NotConfiguredError, match="not configured"):
                call()

    def test_with_token_attempts_network_not_configured_error(self, monkeypatch):
        # With a token the client is live: NotConfiguredError must NOT be
        # raised — the real HTTP attempt is (no server on localhost:8002).
        src = RealTicketingSource("http://localhost:8002", token="tok")
        with pytest.raises(Exception) as exc:
            src.fetch_ticket("x")
        assert not isinstance(exc.value, NotConfiguredError)


class TestSelection:
    def test_default_is_real_stub(self):
        assert isinstance(get_ticket_source(), RealTicketingSource)

    def test_local_selected_by_config(self, monkeypatch):
        import ai_classification.shared.config as config_mod

        class _FakeSettings:
            ticketing_source = "local"

        monkeypatch.setattr(config_mod, "settings", _FakeSettings())
        assert isinstance(get_ticket_source(), LocalFakeTicketSource)


class TestLocalSource:
    def test_fetch_roundtrip(self, monkeypatch):
        s = _make_seams_store(monkeypatch)
        cls = _make_result()
        s.save_incident("loc0000000001", "Seams title", "Seams body", cls,
                        source_ticket_ids=["EXT-77"])
        src = LocalFakeTicketSource(s)
        inc = src.fetch_ticket("EXT-77")
        assert isinstance(inc, Incident)
        assert inc.source_reference == "EXT-77"
        assert inc.title == "Seams title"
        assert inc.description == "Seams body"
        assert inc.id == "loc0000000001"

    def test_list_changed_respects_since(self, monkeypatch):
        s = _make_seams_store(monkeypatch)
        s.save_incident("loc0000000002", "A", "B", _make_result(),
                        source_ticket_ids=["EXT-1"])
        src = LocalFakeTicketSource(s)
        all_incs = src.list_changed(None)
        assert len(all_incs) >= 1
        future = datetime.now(timezone.utc) + timedelta(days=1)
        assert src.list_changed(future) == []


# ── S4/S5: pipeline entry + result object, no writes ──────────────────


class TestPipelineResult:
    def test_result_object_and_no_write(self, monkeypatch):
        s = _make_seams_store(monkeypatch)
        before = len(s.list_incidents())

        def _fake_classify(title, description, *, incident_ref=None, affected_system=None):
            return _make_result(
                canonical_statement=f"CS {title}",
                signature=f"sig {title}",
            )

        monkeypatch.setattr("ai_classification.services.classify.classifier.classify", _fake_classify)
        inc = Incident(source_reference="EXT-99", title="T1", description="D1")
        r = process_incident(inc)
        assert r.is_new is True
        assert r.source_reference == "EXT-99"
        assert r.title == "T1"
        assert r.classification is not None
        assert isinstance(r.similar_tickets, list)
        assert isinstance(r.suggestions, list)
        assert r.confidence == "high"
        assert r.error is None
        # Pipeline alone must not write anything.
        assert len(s.list_incidents()) == before

    def test_seen_returns_existing_without_llm(self, monkeypatch):
        s = _make_seams_store(monkeypatch)
        calls = {"n": 0}

        def _fake_classify(title, description, *, incident_ref=None, affected_system=None):
            calls["n"] += 1
            if calls["n"] > 1:
                raise AssertionError("classify must NOT be called for seen content")
            return _make_result(canonical_statement="CS", signature="sg")

        monkeypatch.setattr("ai_classification.services.classify.classifier.classify", _fake_classify)
        inc = Incident(source_reference="EXT-1", title="T1", description="D1")
        r1 = process_incident(inc)
        persist_result(r1)
        r2 = process_incident(inc)
        assert calls["n"] == 1  # exactly one LLM call across both passes
        assert r2.is_new is False
        assert r2.incident_id is not None


# ── S6: idempotency ───────────────────────────────────────────────────


class TestIdempotency:
    def test_same_source_reference_twice_no_duplicate(self, monkeypatch):
        s = _make_seams_store(monkeypatch)

        def _fake_classify(title, description, *, incident_ref=None, affected_system=None):
            return _make_result(
                canonical_statement=f"CS {title}",
                signature=f"sig {title}",
            )

        monkeypatch.setattr("ai_classification.services.classify.classifier.classify", _fake_classify)
        inc = Incident(source_reference="EXT-42", title="T42", description="D42")

        r1 = process_incident(inc)
        assert r1.is_new is True
        out1 = persist_result(r1)
        assert out1["action"] == "new"

        rows_after_first = len(s.list_incidents())
        assert rows_after_first == 1

        r2 = process_incident(inc)
        assert r2.is_new is False
        assert r2.incident_id == out1["incident_id"]

        out2 = persist_result(r2, dry_run=True)
        assert out2["dry_run"] is True

        assert len(s.list_incidents()) == 1  # no duplicate row

    def test_dry_run_writes_nothing(self, monkeypatch):
        s = _make_seams_store(monkeypatch)
        inc = Incident(source_reference="EXT-DR", title="T", description="D")
        monkeypatch.setattr("ai_classification.services.classify.classifier.classify",
                            lambda t, d, *, incident_ref=None, affected_system=None: _make_result(canonical_statement="CS", signature="sg"))
        r = process_incident(inc)
        out = persist_result(r, dry_run=True)
        assert out["dry_run"] is True
        assert len(s.list_incidents()) == 0


# ── S6: provenance ────────────────────────────────────────────────────


class TestProvenance:
    def test_result_and_persisted_row_carry_model_and_prompt(self, monkeypatch):
        s = _make_seams_store(monkeypatch)

        def _fake_classify(title, description, *, incident_ref=None, affected_system=None):
            return _make_result(canonical_statement="CS", signature="sg")

        monkeypatch.setattr("ai_classification.services.classify.classifier.classify", _fake_classify)
        inc = Incident(source_reference="EXT-P", title="TP", description="DP")
        r = process_incident(inc)
        assert r.model_version == base_settings.llm_model
        assert r.prompt_version != ""
        out = persist_result(r)
        assert out["action"] == "new"
        assert out["incident_id"] is not None
        row = s.get_incident(out["incident_id"])
        assert row is not None
        cls = _existing_classification(row)
        assert cls is not None
        assert cls.model_version == base_settings.llm_model
        assert cls.prompt_version == r.prompt_version


# ── S4: thin callers ──────────────────────────────────────────────────


class TestThinCallers:
    def test_manual_process_no_persist(self, monkeypatch):
        s = _make_seams_store(monkeypatch)
        s.save_incident("loc0000000003", "M", "N", _make_result(),
                        source_ticket_ids=["EXT-M"])
        monkeypatch.setattr("ai_classification.services.classify.classifier.classify",
                            lambda t, d, *, incident_ref=None, affected_system=None: _make_result(canonical_statement="CS", signature="sg"))
        src = LocalFakeTicketSource(s)
        out = manual_process("EXT-M", src, persist=False)
        assert out["result"].source_reference == "EXT-M"
        assert out["persist"]["action"] == "no-persist"
