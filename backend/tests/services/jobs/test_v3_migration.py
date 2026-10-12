"""Classifier v3 migration script — up/down/up reversibility, fully isolated.

Runs scripts.migrate_classifier_v3.upgrade()/downgrade() for real, but inside
its OWN Postgres schema with the search_path pointed there, so the migrations
never touch the shared `public.incidents` table that every other DB-backed
suite depends on.

Do NOT point this back at `public`: the module's last test ends in the DOWN
state (v3 columns dropped), and running it against `public` silently broke
later suites — `services/jobs` runs before `services/ocr` and `shared`
alphabetically, so "placed last in this directory" buys nothing.
"""

import psycopg2
import pytest

from scripts.migrate_classifier_v3 import DOWN_SQL, UP_SQL, downgrade, upgrade
from ai_classification.shared.config import settings

from tests.conftest import TEST_PG_DATABASE

_SCHEMA = "v3_migration_test"


def _connect():
    return psycopg2.connect(
        host=settings.pg_host, port=settings.pg_port,
        user=settings.pg_user, password=settings.pg_password,
        dbname=TEST_PG_DATABASE,
    )


def _table_exists(conn, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (name,),
        )
        return cur.fetchone() is not None


def _column_exists(conn, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'incidents' "
            "AND column_name = %s",
            (name,),
        )
        return cur.fetchone() is not None


@pytest.fixture(scope="module")
def db():
    conn = _connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        # Own schema: these migrations must never touch public.incidents.
        cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {_SCHEMA}")
        # `public` stays in the path so pgvector types (installed there) resolve.
        cur.execute(f"SET search_path TO {_SCHEMA}, public")
        # The ALTERs need an incidents table to act on, even standalone.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS incidents "
            "(id TEXT PRIMARY KEY, classification_json TEXT NOT NULL DEFAULT '{}')"
        )
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    conn.close()


class TestV3Migration:
    def test_up_creates_objects(self, db):
        executed = upgrade(db, verbose=False)
        assert len(executed) == len(UP_SQL)
        assert _table_exists(db, "taxonomy_gaps")
        assert _table_exists(db, "classification_log")
        assert _column_exists(db, "ticket_kind")
        assert _column_exists(db, "classification_status")

    def test_up_is_idempotent(self, db):
        upgrade(db, verbose=False)
        upgrade(db, verbose=False)  # must not raise
        assert _table_exists(db, "taxonomy_gaps")
        assert _column_exists(db, "ticket_kind")

    def test_up_down_up_reversible(self, db):
        upgrade(db, verbose=False)   # ensure up state
        downgrade(db, verbose=False)
        assert not _table_exists(db, "taxonomy_gaps")
        assert not _table_exists(db, "classification_log")
        assert not _column_exists(db, "ticket_kind")
        assert not _column_exists(db, "classification_status")
        # re-up — everything comes back
        upgrade(db, verbose=False)
        assert _table_exists(db, "taxonomy_gaps")
        assert _table_exists(db, "classification_log")
        assert _column_exists(db, "ticket_kind")
        assert _column_exists(db, "classification_status")

    def test_down_is_idempotent(self, db):
        downgrade(db, verbose=False)
        downgrade(db, verbose=False)  # must not raise
        assert not _table_exists(db, "taxonomy_gaps")
        assert not _table_exists(db, "classification_log")
        assert not _column_exists(db, "ticket_kind")
        upgrade(db, verbose=False)
        assert _column_exists(db, "ticket_kind")

    def test_down_executes_all_statements(self, db):
        executed = downgrade(db, verbose=False)
        assert len(executed) == len(DOWN_SQL)
