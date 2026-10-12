"""Pipeline wiring: incident attachments → OCR service → incident record.

Drives the REAL seams pipeline (process_incident + persist_result) against a
live stub OCR endpoint, with the classifier and the DB stubbed out: the
assertion under test is that the OCR text and the attachment names reach
`classify_and_store` — i.e. they land on the incident record.
"""

from __future__ import annotations

import base64
import json
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import ai_classification.services.classify.classifier as classifier_mod
from ai_classification.services.ocr import client as ocr_client
from ai_classification.seams import pipeline
from ai_classification.seams.port import Incident
from ai_classification.shared import config as config_mod
from ai_classification.shared.config import settings
from ai_classification.shared.store import store

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)
STUB_TEXT = "<page 1>\nPUMP FAILURE ON LINE 3 — replace coupling"


def _settings_with(**overrides):
    d = {k: v for k, v in settings.__dict__.items() if not k.startswith("_")}
    d.update(overrides)
    return types.SimpleNamespace(**d)


class _StubOcr(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):  # noqa: N802
        type(self).calls += 1
        self.rfile.read(int(self.headers.get("content-length", 0)))
        body = json.dumps({"engine": "elm", "model": "stub-vl", "text": STUB_TEXT}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _point_ocr_at(monkeypatch, **overrides):
    """Point BOTH settings holders at the (overridden) copy: the client module
    holds its own binding, the pipeline imports the singleton lazily."""
    replaced = _settings_with(**overrides)
    monkeypatch.setattr(ocr_client, "settings", replaced)
    monkeypatch.setattr(config_mod, "settings", replaced)
    return replaced


@pytest.fixture()
def stub_ocr(monkeypatch):
    _StubOcr.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubOcr)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _point_ocr_at(
        monkeypatch,
        ocr_enabled=True,
        ocr_service_url=f"http://127.0.0.1:{server.server_address[1]}",
        ocr_timeout_s=5.0,
    )
    try:
        yield _StubOcr
    finally:
        server.shutdown()
        server.server_close()


class _FakeClassification:
    confidence = "high"
    canonical_statement = "pump failure"
    affected_system = "Operations"
    service = "Facilities"
    incident_type = "Failure"
    category = "Hardware"
    reasoning = "stub"
    model_version = "stub-model"
    prompt_version = "stub-prompt"

    def model_dump(self):
        return {"confidence": self.confidence}

    def model_dump_json(self):
        return json.dumps(self.model_dump())


@pytest.fixture()
def no_db(monkeypatch):
    """The pipeline's store reads and the write call are stubbed — no DB here."""
    monkeypatch.setattr(store, "get_incident_by_hash", lambda h: None)
    monkeypatch.setattr(store, "find_similar", lambda text, classification=None: [])
    monkeypatch.setattr(classifier_mod, "classify", lambda *a, **k: _FakeClassification())
    captured: dict = {}

    def fake_store(title, description, extracted_text="", documents=None, **kwargs):
        captured.update(
            title=title,
            description=description,
            extracted_text=extracted_text,
            documents=documents,
        )
        return type("R", (), {"incident_id": "INC123"})()

    monkeypatch.setattr(classifier_mod, "classify_and_store", fake_store)
    return captured


def _incident(**kw) -> Incident:
    base = dict(source_reference="REF-1", title="Pump failure", description="Line 3 stopped")
    base.update(kw)
    return Incident(**base)


def test_attachment_is_ocrd_and_written_to_the_record(stub_ocr, no_db):
    attachment = {"name": "photo.png", "content_base64": base64.b64encode(PNG_1PX).decode()}
    result = pipeline.process_incident(_incident(attachments=[attachment]))

    assert stub_ocr.calls == 1
    assert STUB_TEXT in result.extracted_text
    assert result.extracted_text.startswith("[photo.png]")
    assert result.documents == ["photo.png"]

    pipeline.persist_result(result)
    assert STUB_TEXT in no_db["extracted_text"]      # ← reaches the incident record
    assert no_db["documents"] == ["photo.png"]


def test_no_attachments_means_no_ocr_call(stub_ocr, no_db):
    result = pipeline.process_incident(_incident())
    assert stub_ocr.calls == 0 and result.extracted_text == "" and result.documents == []


def test_upstream_text_wins_and_is_not_re_ocrd(stub_ocr, no_db):
    result = pipeline.process_incident(
        _incident(extracted_text="already extracted", attachments=[{"name": "a.pdf"}])
    )
    assert stub_ocr.calls == 0
    assert result.extracted_text == "already extracted"
    assert result.documents == ["a.pdf"]


def test_ocr_failure_never_blocks_the_incident(monkeypatch, no_db):
    _point_ocr_at(monkeypatch, ocr_enabled=True, ocr_service_url="http://127.0.0.1:1", ocr_timeout_s=1.0)
    attachment = {"name": "photo.png", "content_base64": base64.b64encode(PNG_1PX).decode()}
    result = pipeline.process_incident(_incident(attachments=[attachment]))

    assert result.error is None                    # classification still ran
    assert result.extracted_text == ""             # nothing invented
    assert result.documents == ["photo.png"]       # the file is still recorded
    outcome = pipeline.persist_result(result)
    assert outcome["action"] == "new"              # the incident still lands


def test_unreadable_attachment_is_recorded_not_fatal(stub_ocr, no_db):
    result = pipeline.process_incident(_incident(attachments=[{"name": "report.pdf", "type": "document"}]))
    assert stub_ocr.calls == 0                     # nothing to send
    assert result.extracted_text == ""
    assert result.documents == ["report.pdf"]
