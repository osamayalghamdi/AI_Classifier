"""Shared pytest fixtures.

Integration tests (test_incident_store.py) run against a real Postgres
database — the same `ai_pg` container the dev server uses, but a separate
database (`ai_incidents_test`) so tests never touch dev data. Mocking
psycopg2 was considered and rejected: it's fragile and misses real
integration bugs (SQL errors, pgvector behavior, connection pooling).
"""

import os

# MUST be set before ai_classification.shared.config is imported: Settings is a
# module-level singleton evaluated at import time. Integration API tests
# auth with this token; make the suite green without manual env exports
# (documented convention: INTEGRATION_TOKEN=test-token).
os.environ.setdefault("INTEGRATION_TOKEN", "test-token")
os.environ.setdefault("INTEGRATION_WORKER_ENABLED", "0")
# v2 persistent clustering: Flow A runs as a BACKGROUND thread after every
# classify_and_store — in tests that would fire real LLM calls from the
# integration/classify tests. Forced off here; flow tests drive the engine
# synchronously (production default stays ON: env not set in .env).
os.environ["CLUSTER_ASSIGN_ON_ARRIVAL"] = "0"
# v2 persistent clustering: sweep groups mint straight to ACTIVE in prod
# (user's zero-friction override). Tests exercise the human-gate path by
# default; the auto-activate path is tested via a settings monkeypatch.
os.environ["CLUSTER_AUTO_ACTIVATE"] = "0"
# Self-healing (heal.py): the worker runs reclassify_fallback_incidents
# IMMEDIATELY on startup, and every TestClient lifespan (integration/webhook
# tests) starts one. That background sweep fires REAL LLM calls against the
# shared test DB and races the heal tests' own rows (flaky
# test_heal_llm_down_fails_open). Forced off here — heal tests drive
# reclassify_fallback_incidents synchronously (production default stays ON).
os.environ["RECLASSIFY_ENABLED"] = "0"

# SAFETY GUARD: integration tests operate on settings.pg_database and can
# wipe rows. Never let them run against the production DB (ai_incidents) —
# force the isolated test DB when PG_DATABASE wasn't explicitly set.
if not os.environ.get("PG_DATABASE"):
    os.environ["PG_DATABASE"] = "ai_incidents_test"
elif os.environ["PG_DATABASE"] == "ai_incidents":
    raise SystemExit(
        "REFUSING to run tests against the production database (ai_incidents). "
        "Set PG_DATABASE=ai_incidents_test (or unset PG_DATABASE entirely)."
    )

import psycopg2
import pytest

from ai_classification.shared.config import settings

TEST_PG_DATABASE = "ai_incidents_test"


# Ensure the test database exists on the same Postgres server as dev, once per run
@pytest.fixture(scope="session", autouse=True)
def _ensure_test_database():
    conn = psycopg2.connect(
        host=settings.pg_host, port=settings.pg_port,
        user=settings.pg_user, password=settings.pg_password,
        dbname=settings.pg_database,
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_PG_DATABASE,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE DATABASE {TEST_PG_DATABASE}")
    finally:
        conn.close()
