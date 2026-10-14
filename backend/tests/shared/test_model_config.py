"""The model-configuration report, and hosting without the models.

The promise these tests hold down: a host with NO model configured still serves
— and says, in one place, which value is missing, what to set, and what
degrades meanwhile. Refusing to start on a missing LLM_MODEL is the behaviour
that turned "set one env var" into a restart loop.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_classification.shared.config import ModelEntry
from ai_classification.shared.model_config import (
    classifier_blocking,
    classifier_issues,
    embedding_issues,
    inspect_model_config,
    ocr_issues,
    report_lines,
    summary,
)


def _entry(**over) -> ModelEntry:
    """The LOCAL Ollama classifier — the default shape of this project."""
    base = dict(name="LOCAL_LLM", role="classifier", provider="ollama",
                enabled=True, model_id="ollama/classifier-qwen",
                api_base="http://192.168.1.50:11434", api_key="")
    base.update(over)
    return ModelEntry(**base)


def _settings(**over) -> SimpleNamespace:
    """The settings surface the report reads — nothing else."""
    base = dict(
        llm_model="ollama/classifier-qwen",
        llm_api_base="http://192.168.1.50:11434",
        llm_api_key=None,
        active_classifier_model=_entry(),
        embedding_provider="external",
        embedding_api_url="https://embed.example/v1",
        embedding_model_name="BAAI/bge-m3",
        ocr_enabled=True,
        ocr_service_url="http://ocr.internal:8001",
        attachments_enabled=False,
        attachment_reader_configured=lambda: True,
    )
    base.update(over)
    return SimpleNamespace(**base)


class TestClassifierReport:
    def test_no_enabled_model_names_the_variables_that_fix_it(self):
        issues = classifier_issues(_settings(active_classifier_model=None, llm_model="",
                                             llm_api_base=None, llm_api_key=None))
        assert [i.capability for i in issues] == ["classifier"]
        issue = issues[0]
        assert issue.blocking is True
        assert "LLM_MODEL" in issue.env and "MODEL_<NAME>_ENABLED" in issue.env
        assert "LLM_MODEL=" in issue.fix            # an example, not a shrug
        assert "LLM_UNAVAILABLE" in issue.effect    # what the caller will see
        assert "still" in issue.effect or "keep working" in issue.effect

    def test_an_explicitly_disabled_local_entry_is_named(self):
        """`ollama/...` enables LOCAL_LLM by itself, so this only happens when
        someone switched it off — and that reads as "my Ollama is up, why no
        classification?" unless the report says which switch to flip."""
        issues = classifier_issues(_settings(active_classifier_model=None,
                                             llm_model="ollama/qwen2.5:7b"))
        assert len(issues) == 2
        local = issues[1]
        assert "local entry is switched OFF" in local.summary
        assert "MODEL_LOCAL_LLM_ENABLED" in local.env

    def test_an_enabled_entry_without_a_key_is_reported_with_its_variable(self):
        remote = _entry(name="QWEN3_6", provider="elm", model_id="openai/qwen3.6",
                        api_base="https://llms.elm.sa/v1", api_key="")
        settings = _settings(active_classifier_model=remote, llm_model="openai/qwen3.6",
                             llm_api_base="https://llms.elm.sa/v1")
        issues = classifier_issues(settings)
        assert len(issues) == 1
        assert "no API key" in issues[0].summary
        assert "UNIVERSAL_API_KEY" in issues[0].env
        assert classifier_blocking(settings) is not None

    def test_a_local_ollama_entry_needs_no_key(self):
        """The local default is unauthenticated — reporting it as unconfigured
        would make a perfectly working local setup look broken."""
        assert classifier_issues(_settings()) == []
        assert classifier_blocking(_settings()) is None

    def test_an_elm_entry_needs_a_base_url(self):
        issues = classifier_issues(_settings(
            active_classifier_model=_entry(name="QWEN3_6", provider="elm",
                                           model_id="openai/qwen3.6", api_base="", api_key="k"),
            llm_model="openai/qwen3.6", llm_api_base=None))
        assert len(issues) == 1
        assert "no API base URL" in issues[0].summary
        assert "BASE_URL" in issues[0].env

    def test_a_fully_wired_entry_reports_nothing(self):
        assert classifier_issues(_settings()) == []
        assert classifier_blocking(_settings()) is None

    def test_the_probe_and_the_cascade_disagreeing_is_advisory_not_blocking(self):
        """/ready probes LLM_MODEL while the cascade calls the registry entry: a
        mismatch must be visible without making the host look broken."""
        issues = classifier_issues(_settings(active_classifier_model=_entry(
            model_id="ollama/qwen2.5:7b")))
        assert [i.capability for i in issues] == ["classifier-probe"]
        assert issues[0].blocking is False
        assert "disagree on the model" in issues[0].summary
        assert classifier_blocking(_settings(active_classifier_model=_entry(
            model_id="ollama/qwen2.5:7b"))) is None


class TestEndpointReports:
    def test_external_embeddings_without_a_url(self):
        issues = embedding_issues(_settings(embedding_provider="external", embedding_api_url=""))
        assert len(issues) == 1
        assert "EMBEDDING_API_URL is unset" in issues[0].summary
        assert "EMBEDDING_API_URL" in issues[0].env

    def test_local_embeddings_without_the_stack(self, monkeypatch):
        import importlib.util
        monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)
        issues = embedding_issues(_settings(embedding_provider="local"))
        assert len(issues) == 1
        assert "not installed" in issues[0].summary
        assert "local-models" in issues[0].fix

    def test_ocr_pointing_at_a_cluster_name_is_flagged_but_advisory(self):
        issues = ocr_issues(_settings(ocr_service_url="http://ocr:8001"))
        assert len(issues) == 1
        assert "cluster-internal name" in issues[0].summary
        assert issues[0].blocking is False
        assert ocr_issues(_settings(ocr_service_url="http://ocr.haj.local:8001")) == []

    def test_ocr_disabled_is_not_an_issue(self):
        assert ocr_issues(_settings(ocr_enabled=False, ocr_service_url="")) == []

    def test_attachments_enabled_without_the_reader_is_flagged(self):
        issues = inspect_model_config(_settings(
            attachments_enabled=True, attachment_reader_configured=lambda: False))
        assert [i.capability for i in issues] == ["attachment-reader"]
        assert "ATTACHMENT_VISION_API_URL" in issues[0].env

    def test_the_report_tolerates_a_settings_object_missing_attributes(self):
        """A report must never be the thing that crashes a startup path."""
        issues = inspect_model_config(SimpleNamespace())
        assert len(issues) >= 1                      # the classifier is unknown → reported
        assert all(i.capability for i in issues)

    def test_summary_exposes_blocking_and_advisory_separately(self):
        body = summary(_settings(active_classifier_model=None, llm_model="",
                                 attachments_enabled=True,
                                 attachment_reader_configured=lambda: False))
        assert body["configured"] is False
        assert [i["capability"] for i in body["blocking"]] == ["classifier"]
        assert "attachment-reader" in [i["capability"] for i in body["issues"]]

    def test_report_lines_carry_the_fix_and_the_effect(self):
        lines = report_lines(inspect_model_config(_settings(
            active_classifier_model=None, llm_model="")))
        text = "\n".join(lines)
        assert "set:" in text and "meanwhile:" in text
        assert "LLM_MODEL" in text
        assert report_lines([]) == []


# ── Hosting: the app starts and serves without any model ──────────────

@pytest.fixture
def unconfigured(monkeypatch):
    """Every settings reader in the app pointed at a host with no classifier."""
    from ai_classification.shared.config import settings as real
    import ai_classification.api.integration as integration_mod
    import ai_classification.app as app_mod

    stub = SimpleNamespace(**{
        **real.__dict__,
        "llm_model": "",
        "llm_api_base": None,
        "llm_api_key": None,
        "active_classifier_model": None,
        "attachment_reader_configured": real.attachment_reader_configured,
    })
    monkeypatch.setattr(app_mod, "settings", stub)
    monkeypatch.setattr(integration_mod, "settings", stub)
    return stub


@pytest.fixture
def isolated_store():
    """Same isolated-instance swap the other lifespan tests use."""
    import ai_classification.shared.store as store_mod
    from ai_classification.shared.store import IncidentStore

    original = store_mod.store
    isolated = IncidentStore()
    store_mod.store = isolated
    try:
        yield isolated
    finally:
        store_mod.store = original


def test_the_app_hosts_and_names_the_missing_model(unconfigured, isolated_store):
    """The acceptance test: no classifier model configured, app up anyway."""
    from fastapi.testclient import TestClient
    from ai_classification.app import app

    with TestClient(app) as client:               # used to raise at startup
        assert client.get("/health").status_code == 200

        ready = client.get("/ready", params={"llm": "true"})
        body = ready.json()

        # The host is ready for traffic: the DB and embeddings are fine, and an
        # unconfigured model is reported rather than fatal.
        assert ready.status_code == 200, body
        assert body["checks"]["db"] == "ok"
        assert body["checks"]["llm"].startswith("not configured:")
        assert "LLM_MODEL" in body["checks"]["llm"]

        models = body["models"]
        assert models["configured"] is False
        classifier = [i for i in models["issues"] if i["capability"] == "classifier"]
        assert classifier and classifier[0]["blocking"] is True
        assert "LLM_MODEL" in classifier[0]["env"]
        assert classifier[0]["fix"]

        # The dashboard's own surface still answers, and the report is there too.
        assert client.get("/status").status_code in (200, 503)


def test_ready_still_probes_a_configured_model(unconfigured, isolated_store, monkeypatch):
    """The unconfigured short-circuit must not swallow a real probe failure."""
    import litellm
    from fastapi.testclient import TestClient
    from ai_classification.app import app

    unconfigured.active_classifier_model = _entry()
    unconfigured.llm_model = "ollama/classifier-qwen"

    def boom(**kwargs):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(litellm, "completion", boom)
    with TestClient(app) as client:
        checks = client.get("/ready", params={"llm": "true"}).json()["checks"]
    assert checks["llm"] == "unreachable: ConnectionError"


class TestLocalModelWiring:
    """The default setup is a local Ollama: one LLM_MODEL line must be enough."""

    def test_an_ollama_llm_model_enables_the_local_entry(self, monkeypatch):
        import ai_classification.shared.config as cfg
        monkeypatch.setenv("LLM_MODEL", "ollama/qwen2.5:7b")
        monkeypatch.delenv("MODEL_LOCAL_LLM_ENABLED", raising=False)
        monkeypatch.delenv("MODEL_LOCAL_LLM_ID", raising=False)
        monkeypatch.setenv("LLM_API_BASE", "http://192.168.1.50:11434")
        reg = cfg._build_model_registry()
        assert reg["LOCAL_LLM"].enabled is True
        assert reg["LOCAL_LLM"].model_id == "ollama/qwen2.5:7b"      # LLM_MODEL wins
        assert reg["LOCAL_LLM"].api_base == "http://192.168.1.50:11434"
        assert reg["LOCAL_LLM"].api_key == ""                        # no key needed

    def test_the_local_entry_falls_back_to_the_loopback_base(self, monkeypatch):
        import ai_classification.shared.config as cfg
        monkeypatch.setenv("LLM_MODEL", "ollama/classifier-qwen")
        monkeypatch.delenv("LLM_API_BASE", raising=False)
        monkeypatch.delenv("MODEL_LOCAL_LLM_API_BASE", raising=False)
        assert cfg._build_model_registry()["LOCAL_LLM"].api_base == "http://localhost:11434"

    def test_nothing_in_the_catalog_is_openrouter(self):
        """Dropped on purpose: the project runs locally (Ollama) or on the
        company endpoint. A leftover entry would resurface as a "fix" in the
        report and in docs, which is how it came back the first time."""
        import ai_classification.shared.config as cfg
        providers = {entry[1] for entry in cfg.MODEL_CATALOG.values()}
        assert "openrouter" not in providers
        assert providers == {"ollama", "elm"}

    def test_a_loopback_ollama_base_is_advisory_inside_a_cluster(self):
        """`localhost` is the right answer on the host and the wrong one in a
        pod: say so without calling a working local setup broken."""
        issues = classifier_issues(_settings(
            active_classifier_model=_entry(api_base="http://localhost:11434"),
            llm_api_base="http://localhost:11434"))
        assert [i.capability for i in issues] == ["classifier"]
        assert issues[0].blocking is False
        assert "the container itself" in issues[0].summary
        assert "LLM_API_BASE" in issues[0].env
