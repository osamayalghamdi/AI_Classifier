"""Shared fixtures for the SMAX connector tests.

Fake SMAX and fake classifier HTTP servers (http.server.ThreadingHTTPServer
on an ephemeral port) — no real network, no LLM, and no imports from the
classifier app anywhere in this package's tests.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from integrations.smax.config import Settings
from integrations.smax.classifier_client import ClassifierClient
from integrations.smax.smax_client import SmaxClient


# ── Fake SMAX server ──────────────────────────────────────────────────

class _FakeSmaxHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence request logging
        pass

    # ── helpers ───────────────────────────────────────────────────────
    def _json(self, obj, code: int = 200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    @staticmethod
    def _changed_iso(ticket: dict) -> str:
        return ticket.get("updated_at") or ticket.get("created_at") or ""

    # ── GET ───────────────────────────────────────────────────────────
    def do_GET(self):
        if self.path.startswith("/tickets?changed_since="):
            since = self.path.split("=", 1)[1]
            tickets = [t for t in self.server.tickets if self._changed_iso(t) > since]
            self._json({"tickets": tickets})
            return
        if self.path.startswith("/tickets/"):
            ticket_id = self.path.rsplit("/", 1)[-1]
            for t in self.server.tickets:
                if str(t.get("id")) == ticket_id:
                    self._json(t)
                    return
            self._json({"error": "not found"}, 404)
            return
        self._json({"error": "not found"}, 404)

    # ── POST ──────────────────────────────────────────────────────────
    def do_POST(self):
        body = self._body()
        if "/suggestions" in self.path:
            self.server.suggestions.append((self.path, body))
            self._json({"ok": True})
            return
        self._json({"error": "not found"}, 404)


@pytest.fixture
def fake_smax():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeSmaxHandler)
    server.tickets = []
    server.suggestions = []  # list of (path, payload) POSTed
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def smax_client(fake_smax):
    url = f"http://127.0.0.1:{fake_smax.server_address[1]}"
    return SmaxClient(api_url=url, token="smax-token")


# ── Fake classifier API server ────────────────────────────────────────

_CLASSIFIER_RESULT = {
    "source_reference": None,  # filled per job
    "is_new": True,
    "incident_id": "inc-1",
    "title": "t",
    "classification": {
        "affected_system": "SAP ERP",
        "service": "Payments",
        "severity": "high",
    },
    "similar_tickets": [{"id": "T-100"}, {"id": "T-200"}],
    "suggestions": ["Restart the payment gateway service"],
    "confidence": "high",
    "model_version": "test-model",
    "prompt_version": "test-prompt",
    "processed_at": "2025-01-01T12:00:00+00:00",
    "persist": {"action": "new"},
    "write_back": {"mode": "suggestions", "applied": False},
}


def _job_for(ref: str) -> dict:
    result = dict(_CLASSIFIER_RESULT)
    result["source_reference"] = ref
    return {
        "source_reference": ref,
        "status": "succeeded",
        "attempts": 1,
        "next_retry_at": None,
        "payload": {"source_reference": ref, "title": "t", "description": "d"},
        "result": result,
        "error": None,
        "created_at": "2025-01-01T11:00:00+00:00",
        "updated_at": "2025-01-01T12:00:00+00:00",
    }


class _FakeClassifierHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence request logging
        pass

    def _json(self, obj, code: int = 200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _record(self, body):
        self.server.requests.append((self.command, self.path, dict(self.headers), body))

    def do_POST(self):
        body = self._body()
        self._record(body)
        if self.path == "/api/v1/incidents":
            inc = body
            ref = inc["source_reference"]
            self.server.jobs[ref] = _job_for(ref)
            self._json(
                {"reference": ref, "status": "succeeded",
                 "location": f"/api/v1/incidents/{ref}"},
                202,
            )
            return
        if self.path == "/api/v1/backfill":
            batch = body
            refs = []
            for inc in batch["incidents"]:
                refs.append(inc["source_reference"])
                self.server.jobs[inc["source_reference"]] = _job_for(inc["source_reference"])
            self._json(
                {"total": len(refs), "references": refs, "location_prefix": "/api/v1/incidents/"},
                202,
            )
            return
        self._json({"error": {"code": "NOT_FOUND", "message": "unknown path"}}, 404)

    def do_GET(self):
        self._record(None)
        ref = self.path.rsplit("/", 1)[-1]
        if ref in self.server.pending_first:
            self.server.pending_first.remove(ref)
            self._json({"source_reference": ref, "status": "pending", "attempts": 0,
                        "result": None, "error": None})
            return
        if ref in self.server.jobs:
            self._json(self.server.jobs[ref])
            return
        self._json({"error": {"code": "NOT_FOUND", "message": f"no job {ref}"}}, 404)


@pytest.fixture
def fake_classifier():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeClassifierHandler)
    server.jobs = {}
    server.pending_first = set()
    server.requests = []  # list of (method, path, headers, body)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def classifier_client(fake_classifier):
    url = f"http://127.0.0.1:{fake_classifier.server_address[1]}"
    return ClassifierClient(api_url=url, token="classifier-token")


# ── Settings fixture (stamp in tmp_path, never the repo) ──────────────

@pytest.fixture
def connector_settings(tmp_path) -> Settings:
    return Settings(
        smax_api_url="http://smax.invalid",
        smax_api_token="smax-token",
        smax_dry_run=True,
        smax_poll_s=0.01,
        smax_sync_stamp_path=str(tmp_path / ".last_sync"),
        smax_write_back="suggestions",
        classifier_api_url="http://classifier.invalid",
        classifier_api_token="classifier-token",
    )
