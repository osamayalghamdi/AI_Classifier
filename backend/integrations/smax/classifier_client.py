"""Classifier API surface (E1-E9) — how this connector talks to the app.

The connector only ever uses the app's public HTTP contract, never its Python
internals. Endpoints used:

- POST /api/v1/incidents        — async ingest, returns 202 + reference.
- GET  /api/v1/incidents/{ref}  — fetch a job's structured result.
- POST /api/v1/backfill         — batch ingest (<=200 incidents per call).
- POST /classify                — OPTIONAL synchronous classify (small /
                                   manual runs); not part of E1-E9.

`incident_payload()` is THE mapping from a connector Incident to the ingest
body: both single ingest and backfill go through it, so a new field is added
in exactly one place and cannot drift between the two paths.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import quote

from .http_client import JsonHttpClient
from .smax_models import Incident

_log = logging.getLogger(__name__)

# Terminal job statuses from the E2 fetch contract. "done"/"failed" are
# accepted as aliases for robustness against older API revisions.
_TERMINAL_SUCCESS = {"succeeded", "done"}
_TERMINAL_FAILURE = {"failed", "flagged"}

MAX_BACKFILL_BATCH = 200


class ClassifierError(RuntimeError):
    """A terminal failure reported by the classifier API (flagged/failed)."""


def incident_payload(incident: Incident) -> dict:
    """Connector Incident → the app's ingest body (ONE definition).

    Used by both `submit` and `backfill` — that is what keeps the two paths
    identical. The API rejects unknown fields (422), so keep this inside the
    documented contract: backend/docs/INTEGRATION_GUIDE.md.
    """
    payload = {
        "source_reference": incident.source_reference,
        "title": incident.title,
        "description": incident.description,
        "status": incident.status,
        "created_at": incident.created_at.isoformat() if incident.created_at else None,
        "updated_at": incident.updated_at.isoformat() if incident.updated_at else None,
    }
    if incident.affected_system:
        # The ticketing system's own system pick — the classifier validates
        # and pins it (LLM resolution only when this is absent).
        payload["affected_system"] = incident.affected_system
    if incident.attachments:
        # Entries take a url or inline content_base64 (+ name): the app OCRs
        # each one and stores the text on the incident record.
        payload["attachments"] = incident.attachments
    return payload


class ClassifierClient(JsonHttpClient):
    """Thin HTTP client for the classifier's public integration API.

    Configured by env (CLASSIFIER_API_URL / CLASSIFIER_API_TOKEN), never by code.
    """

    label = "Classifier API"
    subject = "Classifier API"
    credential_env = "CLASSIFIER_API_TOKEN"
    url_env = "CLASSIFIER_API_URL"

    # ── E1: async ingest ──────────────────────────────────────────────
    def submit(self, incident: Incident) -> str:
        """POST /api/v1/incidents — one incident, returns its reference.

        The API replies 202 + {"reference": ...} immediately; processing
        happens asynchronously server-side (poll with `result`)."""
        body = self.post_json("/api/v1/incidents", incident_payload(incident))
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
            job = self.get_json(f"/api/v1/incidents/{quote(ref)}")
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

        Each item uses the same body as `submit` (see `incident_payload`), so
        a backfilled incident carries exactly what a live one would.
        Returns the list of submitted references."""
        references: list[str] = []
        for start in range(0, len(incidents), MAX_BACKFILL_BATCH):
            chunk = incidents[start:start + MAX_BACKFILL_BATCH]
            body = self.post_json(
                "/api/v1/backfill",
                {"incidents": [incident_payload(inc) for inc in chunk]},
            )
            references.extend(body.get("references", []))
            _log.info("Backfill chunk of %d accepted (%d total so far)",
                      len(chunk), len(references))
        return references

    # ── Optional sync mode (small/manual runs) ────────────────────────
    def classify_sync(self, incident: Incident) -> dict:
        """POST /classify — synchronous, stores the incident, returns the full
        ClassifyResponse. Legacy sync endpoint, not the async E1-E9 path; use
        it only for small/manual runs."""
        payload = {
            "title": incident.title,
            "description": incident.description,
            "source_ticket_id": incident.source_reference,
        }
        if incident.affected_system:
            payload["affected_system"] = incident.affected_system
        return self.post_json("/classify", payload)
