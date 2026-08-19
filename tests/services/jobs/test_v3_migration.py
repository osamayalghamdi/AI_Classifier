"""Classifier v3 migration script — up/down/up reversibility on the test DB.

Runs scripts.migrate_classifier_v3.upgrade()/downgrade() against the real
Postgres test database (same convention as the store integration tests) and
verifies the schema state via information_schema. Placed last in this
directory so dropping the v3 columns can't disturb other DB-backed suites.
"""

import psycopg2
import pytest

from scripts.migrate_classifier_v3 import DOWN_SQL, UP_SQL, downgrade, upgrade
from ai_classification.shared.config import settings

from tests.conftest import TEST_PG_DATABASE


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
            "WHERE table_schema = 'public' AND table_name = %s",
            (name,),
        )
        return cur.fetchone() is not None


def _column_exists(conn, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'incidents' "
            "AND column_name = %s",
            (name,),
        )
        return cur.fetchone() is not None


@pytest.fixture(scope="module")
def db():
    conn = _connect()
    conn.autocommit = True
    # The ALTERs need the incidents table to exist even when this module
    # runs standalone (no store.setup() in this session yet).
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS incidents "
            "(id TEXT PRIMARY KEY, classification_json TEXT NOT NULL DEFAULT '{}')"
        )
    yield conn
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
        # leave the DB back in up state for any suite that follows
        upgrade(db, verbose=False)
        assert _column_exists(db, "ticket_kind")

    def test_down_executes_all_statements(self, db):
        executed = downgrade(db, verbose=False)
        assert len(executed) == len(DOWN_SQL)
