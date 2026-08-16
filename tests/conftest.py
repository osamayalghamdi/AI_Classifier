"""Shared pytest fixtures.

Integration tests (test_incident_store.py) run against a real Postgres
database — the same `ai_pg` container the dev server uses, but a separate
database (`ai_incidents_test`) so tests never touch dev data. Mocking
psycopg2 was considered and rejected: it's fragile and misses real
integration bugs (SQL errors, pgvector behavior, connection pooling).
"""

import os

# MUST be set before ai_classification.config is imported: Settings is a
# module-level singleton evaluated at import time. Integration API tests
# auth with this token; make the suite green without manual env exports
# (documented convention: INTEGRATION_TOKEN=test-token).
os.environ.setdefault("INTEGRATION_TOKEN", "test-token")
os.environ.setdefault("INTEGRATION_WORKER_ENABLED", "0")

import psycopg2
import pytest

from ai_classification.config import settings

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
