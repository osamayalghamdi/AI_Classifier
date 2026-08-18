"""Integration layer (E1-E9): async ingest, job store, retry worker.

Owns the `ingestion_jobs` table and its state machine:

    pending -> processing -> succeeded
                         \-> retryable -> processing (backoff) -> ... -> flagged

The pipeline (seams/pipeline.py) does the classify/persist work; this
layer only orchestrates, tracks state, and retries. An incident is NEVER
half-written: process_incident is read-only, persist_result is the single
write step, and any failure leaves the job retryable (or flagged after
exhaustion) with the real error recorded.

Failure classification (stable codes):
    LLM_UNAVAILABLE / LLM_TIMEOUT  — classify-phase failures (DNS,
        connection, timeout, empty response)
    EMBEDDING_FAILED               — model-load / embed failures
    DB_FAILURE                     — persist-phase (psycopg2) failures
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg2
from psycopg2.extras import Json

from ai_classification.shared.config import settings
from ai_classification.seams.port import Incident
from ai_classification.seams.pipeline import persist_result, process_incident
from ai_classification.services.jobs.integration.schemas import Err

_log = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_SUCCEEDED = "succeeded"
STATUS_RETRYABLE = "retryable"
STATUS_FLAGGED = "flagged"

# LLM-failure signatures seen in real errors (DNS, connect, timeout).
_LLM_FAIL_MARKERS = (
    "getaddrinfo",
    "name resolution",
    "temporary failure",
    "failed to resolve",
    "connection error",
    "connect error",
    "connection refused",
    "max retries exceeded",
    "timeout",
)


def _connect():
    return psycopg2.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        user=settings.pg_user,
        password=settings.pg_password,
        dbname=settings.pg_database,
    )


def ensure_jobs_table() -> None:
    with _connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingestion_jobs (
                    source_reference  TEXT PRIMARY KEY,
                    status            TEXT NOT NULL DEFAULT 'pending',
                    attempts          INT  NOT NULL DEFAULT 0,
                    next_retry_at     TIMESTAMPTZ,
                    payload           JSONB NOT NULL,
                    result_json       JSONB,
                    error_code        TEXT,
                    error_message     TEXT,
                    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )


def _job_from_row(row) -> dict:
    ref, status, attempts, next_retry, payload, result, ecode, emsg, created, updated = row
    return {
        "source_reference": ref,
        "status": status,
        "attempts": attempts,
        "next_retry_at": next_retry.isoformat() if next_retry else None,
        "payload": payload,
        "result": result,
        "error": {"code": ecode, "message": emsg} if ecode else None,
        "created_at": created.isoformat(),
        "updated_at": updated.isoformat(),
    }


# ── Enqueue / fetch (E1/E2/E7 — idempotent on source_reference) ─────

def enqueue(payload: dict) -> dict | None:
    """Create a job. Replay-safe: same source_reference returns the
    existing job unchanged (no re-processing, no attempt increment)."""
    ensure_jobs_table()
    ref = payload["source_reference"]
    with _connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM ingestion_jobs WHERE source_reference = %s", (ref,))
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO ingestion_jobs (source_reference, payload) VALUES (%s, %s)",
                    (ref, Json(payload)),
                )
    return get_job(ref)


def get_job(reference: str) -> dict | None:
    ensure_jobs_table()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_reference, status, attempts, next_retry_at, payload, "
                "result_json, error_code, error_message, created_at, updated_at "
                "FROM ingestion_jobs WHERE source_reference = %s",
                (reference,),
            )
            row = cur.fetchone()
    return _job_from_row(row) if row else None


def list_jobs(limit: int = 20) -> list[dict]:
    ensure_jobs_table()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_reference, status, attempts, next_retry_at, payload, "
                "result_json, error_code, error_message, created_at, updated_at "
                "FROM ingestion_jobs ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
    return [_job_from_row(r) for r in rows]


# ── Failure classification ───────────────────────────────────────────

def classify_failure(message: str, phase: str) -> str:
    """Map a real failure message to a stable error code."""
    if phase == "persist":
        return Err.DB_FAILURE
    low = (message or "").lower()
    if "embedding" in low or "sentence" in low or "tokenizer" in low or "model" in low:
        return Err.EMBEDDING_FAILED
    if "timeout" in low:
        return Err.LLM_TIMEOUT
    if any(m in low for m in _LLM_FAIL_MARKERS):
        return Err.LLM_UNAVAILABLE
    return Err.LLM_UNAVAILABLE


# ── Worker ───────────────────────────────────────────────────────────

def _incident_from_payload(payload: dict) -> Incident:
    return Incident(
        source_reference=payload["source_reference"],
        title=payload.get("title", ""),
        description=payload.get("description", ""),
        attachments=payload.get("attachments", []),
        status=payload.get("status", "active"),
        created_at=payload.get("created_at"),
        updated_at=payload.get("updated_at"),
    )


def _mark(job_ref: str, status: str, *, attempts: int, ecode: str | None = None,
          emsg: str | None = None, result: dict | None = None,
          next_retry_at: datetime | None = None) -> None:
    with _connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ingestion_jobs SET status = %s, attempts = %s, error_code = %s, "
                "error_message = %s, result_json = %s, next_retry_at = %s, updated_at = now() "
                "WHERE source_reference = %s",
                (status, attempts, ecode, emsg, Json(result) if result is not None else None,
                 next_retry_at, job_ref),
            )


def _process_one(job: dict) -> str:
    """Process a single job. Returns the new status."""
    ref = job["source_reference"]
    payload = job["payload"]
    _mark(ref, STATUS_PROCESSING, attempts=job["attempts"])
    incident = _incident_from_payload(payload)

    # Phase 1 — classify (read-only). Never writes.
    try:
        result = process_incident(incident)
    except Exception as exc:  # noqa: BLE001 — worker must record, not crash
        code = classify_failure(str(exc), phase="process")
        return _retry_or_flag(job, code, str(exc))

    if result.error:
        # classify returned an error result (LLM unavailable/parse).
        code = classify_failure(result.error, phase="process")
        return _retry_or_flag(job, code, result.error)

    # Detect the classifier's SILENT fallback: when the LLM is unreachable
    # the classifier never raises — it degrades to a low-confidence generic
    # result whose reasoning starts with "Classification failed after ...".
    # Persisting that as a success would be a half-written incident, so the
    # worker treats it as LLM_UNAVAILABLE and leaves the job retryable.
    reasoning = (getattr(result.classification, "reasoning", None) or "")
    if getattr(result.classification, "confidence", "") == "low" and reasoning.startswith(
        "Classification failed after"
    ):
        return _retry_or_flag(job, Err.LLM_UNAVAILABLE, reasoning[:400])

    # Phase 2 — persist (the single write step).
    try:
        outcome = persist_result(result)
    except Exception as exc:  # noqa: BLE001 — psycopg2 / embed failures
        code = classify_failure(str(exc), phase="persist")
        return _retry_or_flag(job, code, str(exc))

    result_payload = {
        "source_reference": ref,
        "is_new": result.is_new,
        "incident_id": outcome.get("incident_id") or result.incident_id,
        "title": result.title,
        "classification": _classification_dict(result.classification),
        "similar_tickets": result.similar_tickets,
        "suggestions": result.suggestions,
        "confidence": result.confidence,
        "model_version": result.model_version,
        "prompt_version": result.prompt_version,
        "processed_at": result.processed_at.isoformat(),
        "persist": outcome,
        "write_back": {
            "mode": settings.integration_write_back,
            "applied": False,  # default SAFEST: nothing written to ticket fields
        },
    }
    _mark(ref, STATUS_SUCCEEDED, attempts=job["attempts"], result=result_payload)
    _log.info("Integration job %s -> succeeded (incident %s)", ref, result.incident_id)
    return STATUS_SUCCEEDED


def _retry_or_flag(job: dict, code: str, message: str) -> str:
    ref = job["source_reference"]
    attempts = job["attempts"] + 1
    if attempts >= settings.integration_max_attempts:
        _mark(ref, STATUS_FLAGGED, attempts=attempts, ecode=code, emsg=message)
        _log.error("Integration job %s FLAGGED after %d attempts (%s: %s)",
                   ref, attempts, code, message[:200])
        return STATUS_FLAGGED
    delay = settings.integration_retry_base_s * attempts  # linear backoff
    next_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    _mark(ref, STATUS_RETRYABLE, attempts=attempts, ecode=code, emsg=message,
          next_retry_at=next_at)
    _log.warning("Integration job %s retryable (%s: %s) — retry in %ss (attempt %d/%d)",
                 ref, code, message[:200], delay, attempts, settings.integration_max_attempts)
    return STATUS_RETRYABLE


def _classification_dict(cls: Any) -> dict | None:
    if cls is None:
        return None
    if hasattr(cls, "model_dump"):
        return cls.model_dump()
    if isinstance(cls, dict):
        return cls
    return None


def worker_tick(limit: int = 10) -> int:
    """Process due jobs (pending + retryable-with-expired backoff). Returns count."""
    ensure_jobs_table()
    now = datetime.now(timezone.utc)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_reference, status, attempts, next_retry_at, payload, "
                "result_json, error_code, error_message, created_at, updated_at "
                "FROM ingestion_jobs "
                "WHERE status IN (%s, %s) AND (next_retry_at IS NULL OR next_retry_at <= %s) "
                "ORDER BY created_at LIMIT %s",
                (STATUS_PENDING, STATUS_RETRYABLE, now, limit),
            )
            rows = cur.fetchall()
    processed = 0
    for row in rows:
        job = _job_from_row(row)
        try:
            _process_one(job)
        except Exception as exc:  # noqa: BLE001 — never let one job kill the tick
            _log.error("Integration worker crashed processing %s: %s",
                       job["source_reference"], exc)
        processed += 1
    return processed


def start_integration_worker() -> threading.Thread:
    """Background worker thread (daemon) — polls the job table."""
    def _loop() -> None:
        while True:
            try:
                worker_tick()
            except Exception as exc:  # noqa: BLE001 — keep polling through errors
                _log.error("Integration worker tick failed: %s", exc)
            time.sleep(settings.integration_poll_s)

    thread = threading.Thread(target=_loop, name="integration-worker", daemon=True)
    thread.start()
    _log.info("Integration worker started (poll %.1fs, max attempts %d, write-back=%s)",
              settings.integration_poll_s, settings.integration_max_attempts,
              settings.integration_write_back)
    return thread
