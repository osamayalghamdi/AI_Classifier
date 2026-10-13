"""Shared fixtures for the SMAX connector tests.

Fake SMAX and fake classifier HTTP servers (http.server.ThreadingHTTPServer
on an ephemeral port) — no real network, no LLM, and no imports from the
classifier app anywhere in this package's tests.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from integrations.smax.config import Settings
from integrations.smax.classifier_client import ClassifierClient
from integrations.smax.smax_client import SmaxClient


# ── Fake SMAX server ──────────────────────────────────────────────────
# Implements the real handover contract (SMAX_INTEGRATION.md):
#   POST /auth/authentication-endpoint/authenticate/token?TENANTID=…
#   GET  /rest/<tenant>/ems/Incident?layout=&filter=&size=&skip=
#   GET  /rest/<tenant>/frs/file-list/<file_id>
# Cookie auth is ENFORCED (401 without it) and `token_expired` forces one 401
# so the client's refresh-and-retry path is exercised rather than assumed.

def _matches(ticket: dict, expression: str) -> bool:
    """Understand the conditions build_filter() produces (Id, LastUpdateTime)."""
    single = re.search(r"Id = '([^']+)'", expression)
    if single and str(ticket.get("Id")) != single.group(1):
        return False
    listed = re.search(r"Id in \(([^)]*)\)", expression)
    if listed:
        ids = {v.strip().strip("'") for v in listed.group(1).split(",")}
        if str(ticket.get("Id")) not in ids:
            return False
    updated = re.search(r"LastUpdateTime >= (\d+)", expression)
    if updated and int(ticket.get("LastUpdateTime", 0)) < int(updated.group(1)):
        return False
    created = re.search(r"EmsCreationTime >= (\d+)", expression)
    if created and int(ticket.get("EmsCreationTime", 0)) < int(created.group(1)):
        return False
    return True


class _FakeSmaxHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence request logging
        pass

    # ── helpers ───────────────────────────────────────────────────────
    def _send(self, data: bytes, code: int = 200, content_type: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code: int = 200):
        self._send(json.dumps(obj).encode("utf-8"), code)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def _authed(self) -> bool:
        if self.server.token_expired:
            # Simulate server-side expiry: 401 once. The client refreshes, the
            # login handler rotates the token in, and the retry succeeds.
            self._json({"error": {"code": "UNAUTHORIZED"}}, 401)
            return False
        cookie = self.headers.get("Cookie", "")
        ok = (f"SMAX_AUTH_TOKEN={self.server.token}" in cookie
              and f"TENANTID={self.server.tenant}" in cookie)
        if not ok:
            self._json({"error": {"code": "UNAUTHORIZED"}}, 401)
        return ok

    # ── POST: the login exchange ──────────────────────────────────────
    def do_POST(self):
        if self.path.startswith("/auth/authentication-endpoint/authenticate/token"):
            body = self._body()
            self.server.auth_calls.append(body)
            if self.server.token_expired:
                self.server.token_expired = False          # one forced refresh
                self.server.token = "refreshed.jwt.token"
            # SMAX answers with a bare JWT as text/plain.
            self._send(self.server.token.encode("utf-8"), content_type="text/plain")
            return
        self._json({"error": {"code": "NOT_FOUND"}}, 404)

    # ── GET: search + file service ────────────────────────────────────
    def do_GET(self):
        if not self._authed():
            return
        if "/ems/Incident" in self.path:
            query = parse_qs(urlsplit(self.path).query)
            expression = (query.get("filter") or [""])[0]
            size = int((query.get("size") or ["250"])[0])
            skip = int((query.get("skip") or ["0"])[0])
            self.server.filters.append(expression)
            self.server.layouts.append((query.get("layout") or [""])[0])
            rows = [t for t in self.server.tickets if _matches(t, expression)]
            page = rows[skip:skip + size]
            self._json({
                "entities": [{"properties": t} for t in page],
                "meta": {"completion_status": "OK", "total": len(rows)},
            })
            return
        if "/frs/file-list/" in self.path:
            file_id = self.path.rsplit("/", 1)[-1]
            self.server.file_requests.append(file_id)
            self._send(self.server.files.get(file_id, b""),
                       content_type="application/octet-stream")
            return
        self._json({"error": {"code": "NOT_FOUND"}}, 404)


@pytest.fixture
def fake_smax():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeSmaxHandler)
    server.tenant = "t1"
    server.token = "header.payload.signature"   # a 3-part JWT, like SMAX
    server.token_expired = False                # set True to force one 401
    server.tickets = []                         # list of SMAX `properties` dicts
    server.files = {}                           # file_id -> bytes
    server.filters = []                         # every filter expression received
    server.layouts = []                         # the `layout=` of every search (field verification)
    server.file_requests = []                   # every file id requested
    server.auth_calls = []                      # every login body received
    server.suggestions = []                     # write-back attempts (path, body)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def smax_client(fake_smax):
    url = f"http://127.0.0.1:{fake_smax.server_address[1]}"
    return SmaxClient(base_url=url, tenant=fake_smax.tenant,
                      username="integration-user", password="integration-pass")


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
        smax_base="http://smax.invalid",
        smax_tenant="t1",
        smax_user="integration-user",
        smax_pass="integration-pass",
        smax_dry_run=True,
        smax_poll_s=0.01,
        smax_sync_stamp_path=str(tmp_path / ".last_sync"),
        smax_write_back="suggestions",
        classifier_api_url="http://classifier.invalid",
        classifier_api_token="classifier-token",
    )
