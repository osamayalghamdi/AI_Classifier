"""Standalone schema migration for classifier v3 (no app imports, psycopg2 only).

`up`   — the exact v3 DDL (idempotent, safe to run twice): adds
          ticket_kind / classification_status columns to incidents and
          creates taxonomy_gaps + classification_log.
`down` — drops the v3 objects (tables + columns). Reversible:
          up;down;up must all succeed.

DB connection reads PG_* env vars (same defaults as app config):
PG_HOST=localhost PG_PORT=5432 PG_USER=aiuser PG_PASSWORD=aipass PG_DATABASE=ai_incidents

Usage:
    python scripts/migrate_classifier_v3.py             # up (default)
    python scripts/migrate_classifier_v3.py --down      # drop v3 objects
    python scripts/migrate_classifier_v3.py --db other  # target another database
"""
import argparse
import os

import psycopg2

UP_SQL = [
    "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS ticket_kind TEXT NOT NULL DEFAULT 'incident'",
    "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS classification_status TEXT NOT NULL DEFAULT 'ok'",
    """
    CREATE TABLE IF NOT EXISTS taxonomy_gaps (
      id TEXT PRIMARY KEY,
      service TEXT NOT NULL,
      suggested_offering TEXT NOT NULL,
      incident_refs JSONB NOT NULL DEFAULT '[]',
      count INTEGER NOT NULL DEFAULT 1,
      first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      UNIQUE (service, suggested_offering)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS classification_log (
      id BIGSERIAL PRIMARY KEY,
      incident_ref TEXT NOT NULL,
      stage TEXT NOT NULL,
      prompt_version TEXT NOT NULL DEFAULT '',
      model TEXT NOT NULL DEFAULT '',
      raw_verdict TEXT NOT NULL,
      extra JSONB NOT NULL DEFAULT '{}',
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_classification_log_ref ON classification_log (incident_ref, stage)",
]

DOWN_SQL = [
    "DROP TABLE IF EXISTS taxonomy_gaps",
    "DROP TABLE IF EXISTS classification_log",
    "ALTER TABLE incidents DROP COLUMN IF EXISTS ticket_kind",
    "ALTER TABLE incidents DROP COLUMN IF EXISTS classification_status",
]


def _connect(dbname: str | None = None):
    return psycopg2.connect(
        host=os.environ.get("PG_HOST", "localhost"),
        port=int(os.environ.get("PG_PORT", "5432")),
        user=os.environ.get("PG_USER", "aiuser"),
        password=os.environ.get("PG_PASSWORD", "aipass"),
        dbname=dbname or os.environ.get("PG_DATABASE", "ai_incidents"),
    )


def upgrade(conn, *, verbose: bool = True) -> list[str]:
    """Apply the v3 DDL (idempotent). Returns the executed statements."""
    executed = []
    with conn.cursor() as cur:
        for sql in UP_SQL:
            cur.execute(sql)
            executed.append(sql)
    conn.commit()
    if verbose:
        for sql in executed:
            print(f"up: {sql.strip().splitlines()[0]}")
    return executed


def downgrade(conn, *, verbose: bool = True) -> list[str]:
    """Drop the v3 objects (tables + columns). Returns the executed statements."""
    executed = []
    with conn.cursor() as cur:
        for sql in DOWN_SQL:
            cur.execute(sql)
            executed.append(sql)
    conn.commit()
    if verbose:
        for sql in executed:
            print(f"down: {sql}")
    return executed


def main() -> None:
    parser = argparse.ArgumentParser(description="Classifier v3 schema migration")
    parser.add_argument("--up", action="store_true", help="apply the v3 DDL (default)")
    parser.add_argument("--down", action="store_true", help="drop the v3 tables/columns")
    parser.add_argument("--db", default=None,
                        help="database name (default: PG_DATABASE env or ai_incidents)")
    args = parser.parse_args()

    conn = _connect(args.db)
    try:
        if args.down:
            downgrade(conn)
            print("migrate_classifier_v3: down complete")
        else:
            upgrade(conn)
            print("migrate_classifier_v3: up complete")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
