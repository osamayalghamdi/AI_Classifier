"""OCR client tests — the app side of the external OCR service.

No DB, no LLM, no docker: a real (threaded) HTTP server stands in for the
OCR service, so the multipart upload, the JSON parsing and the failure
handling are exercised end to end.

`Settings` is a frozen dataclass, so overrides go through a copy patched
onto the CONSUMING module (`client.settings`) — the same convention as
tests/services/classify/test_cascade.py.
"""

from __future__ import annotations

import base64
import json
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ai_classification.services.ocr import client as ocr_client
from ai_classification.shared.config import settings

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


def _settings_with(**overrides):
    """A copy of Settings with OCR knobs overridden (frozen dataclass)."""
    d = {k: v for k, v in settings.__dict__.items() if not k.startswith("_")}
    d.update(overrides)
    return types.SimpleNamespace(**d)


class _StubOcr(BaseHTTPRequestHandler):
    """Minimal `POST /ocr` — records what it received."""

    reply = {"engine": "elm", "model": "stub-vl", "text": "<page 1>\nSTUB TEXT", "words": []}
    status = 200
    received: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length)
        type(self).received.append(
            {"path": self.path, "ctype": self.headers.get("content-type", ""), "body": body}
        )
        payload = json.dumps(self.reply).encode()
        self.send_response(self.status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silent
        pass


@pytest.fixture()
def stub_ocr(monkeypatch):
    """A live stub OCR endpoint; the client module points at it."""
    _StubOcr.received = []
    _StubOcr.status = 200
    _StubOcr.reply = {"engine": "elm", "model": "stub-vl", "text": "<page 1>\nSTUB TEXT", "words": []}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubOcr)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/ocr"
    monkeypatch.setattr(
        ocr_client, "settings",
        _settings_with(ocr_enabled=True, ocr_service_url=url, ocr_timeout_s=5.0),
    )
    try:
        yield _StubOcr
    finally:
        server.shutdown()
        server.server_close()


# ── attachment field extraction ───────────────────────────────────────

def test_name_and_mime_defaults():
    assert ocr_client.attachment_name({"filename": "scan.PDF"}) == "scan.PDF"
    assert ocr_client.attachment_name({"file_name": "/tmp/x/y.png"}) == "y.png"
    assert ocr_client.attachment_name({}) == "attachment"
    assert ocr_client.attachment_mime({}, "a.pdf") == "application/pdf"
    assert ocr_client.attachment_mime({"type": "image/png"}, "a") == "image/png"


def test_bytes_from_inline_base64_and_data_uri():
    raw, mime, name = ocr_client.attachment_bytes(
        {"name": "a.png", "content_base64": base64.b64encode(PNG_1PX).decode()}
    )
    assert raw == PNG_1PX and mime == "image/png" and name == "a.png"
    data_uri = f"data:image/png;base64,{base64.b64encode(PNG_1PX).decode()}"
    raw2, _, _ = ocr_client.attachment_bytes({"name": "a.png", "content": data_uri})
    assert raw2 == PNG_1PX


def test_name_only_attachment_is_rejected_cleanly():
    with pytest.raises(ValueError, match="no content_base64/content/url"):
        ocr_client.attachment_bytes({"name": "report.pdf", "type": "document"})


def test_endpoint_normalization(monkeypatch):
    monkeypatch.setattr(ocr_client, "settings", _settings_with(ocr_service_url="http://ocr:8001"))
    assert ocr_client.ocr_endpoint() == "http://ocr:8001/ocr"
    monkeypatch.setattr(ocr_client, "settings", _settings_with(ocr_service_url="http://ocr:8001/ocr"))
    assert ocr_client.ocr_endpoint() == "http://ocr:8001/ocr"
    monkeypatch.setattr(ocr_client, "settings", _settings_with(ocr_service_url="https://ocr.company.local/api/"))
    assert ocr_client.ocr_endpoint() == "https://ocr.company.local/api/ocr"


# ── the wire ──────────────────────────────────────────────────────────

def test_transcribe_posts_multipart_and_parses_reply(stub_ocr):
    outcome = ocr_client.transcribe({"name": "scan.png", "content_base64": base64.b64encode(PNG_1PX).decode()})
    assert outcome.ok and outcome.text.startswith("<page 1>")
    assert (outcome.engine, outcome.model, outcome.name) == ("elm", "stub-vl", "scan.png")
    sent = stub_ocr.received[-1]
    assert sent["path"] == "/ocr"
    assert sent["ctype"].startswith("multipart/form-data; boundary=")
    assert b'name="file"' in sent["body"] and b'filename="scan.png"' in sent["body"]
    assert PNG_1PX in sent["body"]


def test_http_error_becomes_an_outcome_not_an_exception(stub_ocr):
    stub_ocr.status = 503
    outcome = ocr_client.transcribe({"name": "scan.png", "content_base64": base64.b64encode(PNG_1PX).decode()})
    assert not outcome.ok and "503" in (outcome.error or "")


def test_unreachable_endpoint_is_recorded(monkeypatch):
    monkeypatch.setattr(
        ocr_client, "settings",
        _settings_with(ocr_enabled=True, ocr_service_url="http://127.0.0.1:1", ocr_timeout_s=1.0),
    )
    outcome = ocr_client.transcribe({"name": "a.png", "content_base64": base64.b64encode(PNG_1PX).decode()})
    assert not outcome.ok and outcome.error


def test_oversize_attachment_is_refused(monkeypatch):
    monkeypatch.setattr(ocr_client, "settings", _settings_with(ocr_max_attachment_mb=0.00001))
    with pytest.raises(ValueError, match="MB cap"):
        ocr_client.attachment_bytes({"name": "big.png", "content_base64": base64.b64encode(PNG_1PX).decode()})


# ── report semantics ──────────────────────────────────────────────────

def test_report_best_effort_and_text_shape(stub_ocr):
    report = ocr_client.transcribe_attachments([
        {"name": "good.png", "content_base64": base64.b64encode(PNG_1PX).decode()},
        {"name": "name-only.pdf"},  # unreadable → recorded, not raised
    ])
    assert [o.name for o in report.outcomes] == ["good.png", "name-only.pdf"]
    assert len(report.succeeded) == 1 and len(report.failed) == 1
    assert report.documents == ["good.png", "name-only.pdf"]
    assert report.text().startswith("[good.png]\n<page 1>")
    assert report.text(limit=5) == "[good"


def test_disabled_ocr_makes_no_request(stub_ocr, monkeypatch):
    monkeypatch.setattr(ocr_client, "settings", _settings_with(ocr_enabled=False, ocr_max_text_chars=20000))
    report = ocr_client.transcribe_attachments(
        [{"name": "a.png", "content_base64": base64.b64encode(PNG_1PX).decode()}]
    )
    assert report.outcomes == [] and stub_ocr.received == []
