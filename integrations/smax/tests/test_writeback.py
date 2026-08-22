"""Tests for the write-back loop (writeback.py).

Dry-run must LOG and never POST; real mode posts the suggestion payload to
the fake SMAX; mode=none skips posting. No real network, no LLM.
"""

from __future__ import annotations

from types import SimpleNamespace

from integrations.smax.config import Settings
from integrations.smax.smax_models import Incident
from integrations.smax import writeback


class TestDryRun:
    def test_logs_instead_of_posting(self, fake_smax, smax_client,
                                     classifier_client, connector_settings, caplog):
        classifier_client.submit(Incident(source_reference="T-1", title="t", description="d"))
        import logging
        with caplog.at_level(logging.INFO):
            stats = writeback.run_once(
                smax_client, classifier_client, connector_settings, ["T-1"],
                max_attempts=3, poll_interval=0.01,
            )
        assert stats["ready"] == 1
        assert stats["dry_run"] == 1
        assert stats["written"] == 0
        # Nothing reached the fake SMAX.
        assert fake_smax.suggestions == []
        # The would-post payload was logged.
        assert any("DRY-RUN write-back for T-1" in r.message for r in caplog.records)
        assert any("SAP ERP" in r.message for r in caplog.records)  # payload visible


class TestRealMode:
    def test_posts_suggestion(self, fake_smax, smax_client, classifier_client):
        classifier_client.submit(Incident(source_reference="T-2", title="t", description="d"))
        settings = Settings(
            smax_api_url="http://smax.invalid", smax_api_token="tok",
            smax_dry_run=False, smax_poll_s=0.01,
            smax_sync_stamp_path="/tmp/never-used", smax_write_back="suggestions",
            classifier_api_url="http://classifier.invalid", classifier_api_token="tok",
        )
        stats = writeback.run_once(smax_client, classifier_client, settings, ["T-2"],
                                   max_attempts=3, poll_interval=0.01)
        assert stats["written"] == 1
        assert len(fake_smax.suggestions) == 1
        path, payload = fake_smax.suggestions[0]
        assert path == "/tickets/T-2/suggestions"
        assert payload["classification"]["affected_system"] == "SAP ERP"
        assert payload["similar_ticket_ids"] == ["T-100", "T-200"]
        assert payload["suggestions"] == ["Restart the payment gateway service"]
        assert payload["confidence"] == "high"


class TestModeNone:
    def test_never_posts(self, fake_smax, smax_client, classifier_client):
        classifier_client.submit(Incident(source_reference="T-3", title="t", description="d"))
        settings = Settings(
            smax_api_url="http://smax.invalid", smax_api_token="tok",
            smax_dry_run=False, smax_poll_s=0.01,
            smax_sync_stamp_path="/tmp/never-used", smax_write_back="none",
            classifier_api_url="http://classifier.invalid", classifier_api_token="tok",
        )
        stats = writeback.run_once(smax_client, classifier_client, settings, ["T-3"],
                                   max_attempts=3, poll_interval=0.01)
        assert stats["ready"] == 1
        assert stats["skipped"] == 1
        assert fake_smax.suggestions == []


class TestResultFromJob:
    def test_wraps_classification_for_attribute_access(self):
        """End-to-end BUG-1: the API returns classification as a dict; the
        write-back wraps it so to_smax_suggestion's getattr works."""
        job = {
            "source_reference": "T-9",
            "status": "succeeded",
            "result": {
                "classification": {
                    "affected_system": "SAP ERP", "service": "Payments", "severity": "high",
                },
                "similar_tickets": [{"id": "T-1"}],
                "suggestions": ["s"],
                "confidence": "high",
                "model_version": "m",
                "prompt_version": "p",
                "processed_at": "2025-01-01T12:00:00+00:00",
            },
        }
        result = writeback.result_from_job(job)
        assert isinstance(result.classification, SimpleNamespace)
        payload = writeback.to_smax_suggestion(result)
        assert payload["classification"]["affected_system"] == "SAP ERP"
        assert payload["similar_ticket_ids"] == ["T-1"]
        assert payload["processed_at"] == "2025-01-01T12:00:00+00:00"

    def test_no_result_dict_degrades_gracefully(self):
        job = {"source_reference": "T-0", "status": "succeeded", "result": None}
        result = writeback.result_from_job(job)
        assert result.classification is None
        payload = writeback.to_smax_suggestion(result)
        assert payload["classification"]["affected_system"] is None
        assert payload["similar_ticket_ids"] == []
