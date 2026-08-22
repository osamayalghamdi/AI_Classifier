"""PostgreSQL + pgvector incident store, plus app lifecycle and store-facing
service calls.

Uses pgvector for indexed cosine similarity. Thread-safe via a connection pool.

Pipeline position: 40_store — Postgres/pgvector persistence."""

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import psycopg2
import psycopg2.extras
import psycopg2.pool
from fastapi import FastAPI
from sentence_transformers import SentenceTransformer

from ai_classification.shared.config import settings
from ai_classification.domain.models import ClassificationResult
from ai_classification.services.jobs.sync import start_sync_worker

from ai_classification.shared.db import DBBase, VECTOR_DIM
from ai_classification.shared.store_incidents import IncidentsMixin
from ai_classification.shared.store_clusters import ClustersMixin
from ai_classification.shared.store_logs import LogsMixin

_log = logging.getLogger(__name__)

# Column names for the common incident SELECT (17 cols, 0-indexed).
# Used by _row_to_incident to map DB rows → dicts.
_INCIDENT_COLS: tuple[str, ...] = (
    "id", "title", "description", "extracted_text", "classification_json",
    "status", "created_at", "documents", "assign_group", "assignee", "priority",
    "notes", "discussion_history", "escalation_info", "completion_code",
    "ticket_kind", "classification_status",
)


@dataclass
class SimilarMatch:
    id: str
    title: str
    similarity: float
    classification: ClassificationResult


class IncidentStore(DBBase, IncidentsMixin, ClustersMixin, LogsMixin):
    """PostgreSQL-backed store with pgvector cosine similarity."""


# ── Module-level singleton + app lifecycle ──────────────────────────────

store = IncidentStore()


# Start/stop app: init store, begin background sync
@asynccontextmanager
async def lifespan(app: FastAPI):
    # D2: resolved LLM/DB/embedding config as the FIRST log line — explicit,
    # no implicit defaults. (load_dotenv is CWD-relative: env must be set
    # deliberately by the caller — compose .env, systemd, or export.)
    _log.info(
        "Starting app — model=%s, api_base=%s, db=%s:%s/%s, embedding_model=%s",
        settings.llm_model,
        settings.llm_api_base or "(provider default)",
        settings.pg_host,
        settings.pg_port,
        settings.pg_database,
        settings.embedding_model_name,
    )
    # D3: fail loud on missing/invalid LLM config — never a silent fallback
    # to the ollama default in config.py.
    if not os.environ.get("LLM_MODEL"):
        raise RuntimeError(
            "LLM_MODEL is not set. Export LLM_MODEL explicitly (e.g. "
            "LLM_MODEL=openrouter/qwen/qwen3.6-35b-a3b) — refusing to start "
            "with an implicit default model."
        )
    if settings.llm_model.startswith("openrouter/") and not (settings.llm_api_key or "").strip():
        raise RuntimeError(
            "LLM_API_KEY is required when LLM_MODEL starts with 'openrouter/' — "
            "export LLM_API_KEY. Refusing to start with an unauthenticated LLM config."
        )
    store.setup()
    if store.ready:
        _log.info("Store ready")
    else:
        _log.warning("Store FAILED (embeddings disabled)")

    start_sync_worker(store)

    # v2 persistent clustering: Flow B pool sweep (every REPOOL_INTERVAL,
    # 900s) + Flow C nightly audit. Replaces the legacy 5-min stateless
    # rebuild loop AND the sub-offering repool worker (Flow B supersedes
    # repool's phase logic; the sub-offering engine stays dormant).
    from ai_classification.services.cluster.persistent import start_sweep_worker
    start_sweep_worker()

    # Self-healing: re-classify fallback-marked incidents once the LLM is
    # reachable again (gated on RECLASSIFY_ENABLED; the worker idles when off).
    from ai_classification.services.jobs.heal import start_heal_worker
    start_heal_worker()

    # Service status monitor — loud logging when any service (esp. the LLM
    # endpoint) is unreachable; state exposed via GET /status.
    from ai_classification.services.ingest.status_monitor import monitor
    monitor.start()

    # E1-E9 integration worker (async ingest queue) — gated so tests can
    # drive the queue synchronously (INTEGRATION_WORKER_ENABLED=0).
    if settings.integration_worker_enabled:
        from ai_classification.services.jobs.integration import start_integration_worker
        start_integration_worker()

    yield
    _log.info("Shutting down store")
    store.close()


# Return service health status
def get_health() -> dict:
    return {"status": "ok", "model": settings.llm_model, "store_ready": store.ready}


# Mark an incident as resolved
def resolve_incident(incident_id: str) -> bool:
    ok = store.resolve_incident(incident_id)
    if ok:
        _log.info("Incident %s resolved", incident_id)
    else:
        _log.warning("Resolve failed — incident %s not found", incident_id)
    return ok


# Get a single incident by ID
def get_incident(incident_id: str) -> dict | None:
    inc = store.get_incident(incident_id)
    if inc is None:
        _log.debug("Incident %s not found", incident_id)
    return inc


# Delete all incidents
def delete_all_incidents() -> int:
    count = store.delete_all()
    _log.warning("All incidents deleted — count=%d", count)
    return count


# List all incidents, optional ?status= filter
def list_incidents(status: str | None = None) -> list[dict]:
    items = store.list_incidents(status)
    _log.debug("Listed %d incidents (status=%s)", len(items), status or "all")
    return items
