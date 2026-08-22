"""Tests for the classifier public-API client (classifier_client.py).

Uses the fake classifier HTTP server from conftest — no real network, no
imports from the classifier app.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from integrations.smax.classifier_client import ClassifierClient, ClassifierError
from integrations.smax.config import NotConfiguredError
from integrations.smax.smax_models import Incident


def _incident(ref: str = "INC-1") -> Incident:
    return Incident(
        source_reference=ref,
        title="Permit portal down",
        description="Users cannot log in",
        status="active",
        created_at=datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc),
        updated_at=datetime(2025, 1, 2, 11, 30, tzinfo=timezone.utc),
    )


class TestSubmit:
    def test_builds_request_and_parses_202(self, classifier_client, fake_classifier):
        ref = classifier_client.submit(_incident())
        assert ref == "INC-1"
        method, path, headers, body = fake_classifier.requests[-1]
        assert method == "POST"
        assert path == "/api/v1/incidents"
        assert headers.get("Authorization") == "Bearer classifier-token"
        assert body["source_reference"] == "INC-1"
        assert body["title"] == "Permit portal down"
        assert body["description"] == "Users cannot log in"
        assert body["status"] == "active"
        assert body["created_at"] == "2025-01-01T10:00:00+00:00"
        assert body["updated_at"] == "2025-01-02T11:30:00+00:00"

    def test_missing_token_raises_not_configured_before_network(self, fake_classifier):
        url = f"http://127.0.0.1:{fake_classifier.server_address[1]}"
        client = ClassifierClient(api_url=url, token="")
        with pytest.raises(NotConfiguredError, match="CLASSIFIER_API_TOKEN"):
            client.submit(_incident())


class TestResult:
    def test_returns_succeeded_job(self, classifier_client, fake_classifier):
        classifier_client.submit(_incident())
        job = classifier_client.result("INC-1", max_attempts=3, poll_interval=0.01)
        assert job["status"] == "succeeded"
        assert job["result"]["classification"]["affected_system"] == "SAP ERP"

    def test_polls_until_terminal(self, classifier_client, fake_classifier):
        classifier_client.submit(_incident())
        fake_classifier.pending_first.add("INC-1")  # first GET -> pending
        job = classifier_client.result("INC-1", max_attempts=5, poll_interval=0.01)
        assert job["status"] == "succeeded"
        # At least two GETs happened: one pending + one succeeded.
        gets = [r for r in fake_classifier.requests if r[0] == "GET"]
        assert len(gets) >= 2

    def test_flagged_raises(self, classifier_client, fake_classifier):
        fake_classifier.jobs["BAD-1"] = {
            "source_reference": "BAD-1",
            "status": "flagged",
            "attempts": 5,
            "result": None,
            "error": {"code": "LLM_UNAVAILABLE", "message": "llm down"},
        }
        with pytest.raises(ClassifierError, match="flagged"):
            classifier_client.result("BAD-1", max_attempts=2, poll_interval=0.01)

    def test_returns_none_when_attempts_exhausted(self, classifier_client, fake_classifier):
        fake_classifier.pending_first.add("SLOW-1")
        fake_classifier.jobs["SLOW-1"] = {
            "source_reference": "SLOW-1",
            "status": "processing",
            "attempts": 1,
            "result": None,
            "error": None,
        }
        assert classifier_client.result("SLOW-1", max_attempts=2, poll_interval=0.01) is None

    def test_missing_token_raises_not_configured(self, fake_classifier):
        url = f"http://127.0.0.1:{fake_classifier.server_address[1]}"
        client = ClassifierClient(api_url=url, token="")
        with pytest.raises(NotConfiguredError):
            client.result("INC-1")


class TestBackfill:
    def test_chunks_and_returns_references(self, classifier_client, fake_classifier):
        incidents = [_incident(ref=f"INC-{i}") for i in range(250)]  # > 200 -> 2 chunks
        refs = classifier_client.backfill(incidents)
        assert len(refs) == 250
        posts = [r for r in fake_classifier.requests if r[0] == "POST" and r[1] == "/api/v1/backfill"]
        assert len(posts) == 2
        assert len(posts[0][3]["incidents"]) == 200
        assert len(posts[1][3]["incidents"]) == 50
        # Each chunk references the right source_references.
        assert posts[0][3]["incidents"][0]["source_reference"] == "INC-0"
        assert posts[1][3]["incidents"][0]["source_reference"] == "INC-200"
