"""HTTP client for the classifier's public API (E1-E9 integration contract).

The connector talks to the classifier ONLY through this HTTP surface —
never through its Python internals. Endpoints used:

- POST /api/v1/incidents        — async ingest, returns 202 + reference.
- GET  /api/v1/incidents/{ref}  — fetch a job's structured result.
- POST /api/v1/backfill         — batch ingest (<=200 incidents per call).
- POST /classify                — OPTIONAL synchronous classify (small /
                                   manual runs); not part of E1-E9, but
                                   handy for one-off checks.

Transport mirrors smax_client.py: urllib, Bearer auth, timeouts, retry on
429/5xx, and a loud NotConfiguredError when the token is missing.
"""

from __future__ import annotations

import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import NotConfiguredError
from .smax_models import Incident

_log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 10.0
MAX_RETRIES = 3

# Terminal job statuses from the E2 fetch contract. "done"/"failed" are
# accepted as aliases for robustness against older API revisions.
_TERMINAL_SUCCESS = {"succeeded", "done"}
_TERMINAL_FAILURE = {"failed", "flagged"}

MAX_BACKFILL_BATCH = 200


class ClassifierError(RuntimeError):
    """A terminal failure reported by the classifier API (flagged/failed)."""


class ClassifierClient:
    """Thin HTTP client for the classifier's public integration API.

    Configured by env (CLASSIFIER_API_URL / CLASSIFIER_API_TOKEN), never
    by code.
    """

    def __init__(self, api_url: str, token: str, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._api_url = api_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    # ── transport ─────────────────────────────────────────────────────
    def _request(self, method: str, path: str, body: bytes | None = None) -> bytes:
        self._require_configured()
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = Request(
            f"{self._api_url}{path}",
            data=body,
            method=method,
            headers=headers,
        )
        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                with urlopen(req, timeout=self._timeout) as resp:
                    return resp.read()
            except HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                    _log.warning("Classifier API %s on %s — retry %d/%d",
                                 exc.code, path, attempt, MAX_RETRIES)
                    continue
                raise
            except URLError as exc:
                last_err = exc
                if attempt < MAX_RETRIES:
                    _log.warning("Classifier API unreachable on %s — retry %d/%d",
                                 path, attempt, MAX_RETRIES)
                    continue
        raise RuntimeError(
            f"Classifier API unreachable after {MAX_RETRIES} attempts: {last_err}"
        ) from last_err

    # ── E1: async ingest ──────────────────────────────────────────────
    def submit(self, incident: Incident) -> str:
        """POST /api/v1/incidents — one incident, returns its reference.

        The API replies 202 + {"reference": ...} immediately; processing
        happens asynchronously server-side (poll with `result`)."""
        payload = {
            "source_reference": incident.source_reference,
            "title": incident.title,
            "description": incident.description,
            "status": incident.status,
            "created_at": incident.created_at.isoformat() if incident.created_at else None,
            "updated_at": incident.updated_at.isoformat() if incident.updated_at else None,
        }
        if incident.attachments:
            payload["attachments"] = incident.attachments
        raw = self._request("POST", "/api/v1/incidents", body=_dumps(payload))
        body = _json(raw)
        reference = body.get("reference")
        if not reference:
            raise ClassifierError(
                f"POST /api/v1/incidents returned no reference: {body!r}"
            )
        _log.info("Submitted incident %s (status=%s)", reference, body.get("status"))
        return reference

    # ── E2: fetch result by reference (with polling) ──────────────────
    def result(self, ref: str, *, max_attempts: int = 20, poll_interval: float = 2.0) -> dict | None:
        """GET /api/v1/incidents/{ref} — poll until terminal or attempts out.

        Returns the job dict ({"status": "succeeded", "result": {...}, ...})
        on success. Raises ClassifierError on a terminal failure
        (flagged/failed). Returns None when attempts are exhausted while the
        job is still pending/processing/retryable."""
        for attempt in range(1, max_attempts + 1):
            job = _json(self._request("GET", f"/api/v1/incidents/{quote(ref)}"))
            status = job.get("status", "")
            if status in _TERMINAL_SUCCESS:
                return job
            if status in _TERMINAL_FAILURE:
                error = job.get("error") or {}
                raise ClassifierError(
                    f"Incident {ref} ended {status}: {error.get('message', '')}"
                )
            _log.debug("Incident %s still %s (attempt %d/%d)",
                       ref, status or "unknown", attempt, max_attempts)
            if attempt < max_attempts:
                time.sleep(poll_interval)
        return None

    # ── E3: batch / backfill ──────────────────────────────────────────
    def backfill(self, incidents: list[Incident]) -> list[str]:
        """POST /api/v1/backfill — up to 200 incidents per call.

        Returns the list of submitted references."""
        references: list[str] = []
        for start in range(0, len(incidents), MAX_BACKFILL_BATCH):
            chunk = incidents[start:start + MAX_BACKFILL_BATCH]
            payload = {
                "incidents": [
                    {
                        "source_reference": inc.source_reference,
                        "title": inc.title,
                        "description": inc.description,
                        "status": inc.status,
                        "created_at": inc.created_at.isoformat() if inc.created_at else None,
                        "updated_at": inc.updated_at.isoformat() if inc.updated_at else None,
                    }
                    for inc in chunk
                ]
            }
            raw = self._request("POST", "/api/v1/backfill", body=_dumps(payload))
            body = _json(raw)
            references.extend(body.get("references", []))
            _log.info("Backfill chunk of %d accepted (%d total so far)",
                      len(chunk), len(references))
        return references

    # ── Optional sync mode (small/manual runs) ────────────────────────
    def classify_sync(self, incident: Incident) -> dict:
        """POST /classify — synchronous, stores the incident, returns the
        full ClassifyResponse. Use only for small/manual runs: this is the
        legacy sync endpoint, not the async E1-E9 path."""
        payload = {
            "title": incident.title,
            "description": incident.description,
            "source_ticket_id": incident.source_reference,
        }
        if incident.affected_system:
            payload["affected_system"] = incident.affected_system
        raw = self._request("POST", "/classify", body=_dumps(payload))
        return _json(raw)

    def _require_configured(self) -> None:
        if not self._token:
            raise NotConfiguredError(
                "Classifier API is not configured: set CLASSIFIER_API_TOKEN "
                f"(CLASSIFIER_API_URL={self._api_url!r} present but no credentials)"
            )


def _json(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"))


def _dumps(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")
