"""PostgreSQL connection pool + schema setup and embedding primitives for the
incident store.

Extracted from store.py (refactor C-1): lifecycle (pool + DDL), embedding
helpers, connection helpers and id generation. store.py composes this DBBase
with the IncidentsMixin / ClustersMixin / LogsMixin into IncidentStore.

Pipeline position: 40_store — Postgres/pgvector persistence."""

import logging
import uuid

import numpy as np
import psycopg2
import psycopg2.pool

from ai_classification.domain.models import ClassificationResult

_log = logging.getLogger(__name__)

VECTOR_DIM = 1024  # BAAI/bge-m3 output dim


class DBBase:
    """Connection pool + embedding primitives shared by the store mixins."""

    def __init__(self) -> None:
        self._model: SentenceTransformer | None = None
        self._pool: psycopg2.pool.ThreadedConnectionPool | None = None
        self._ready = False

    # ── Lifecycle ──────────────────────────────────────────────────

    def setup(self) -> None:
        # Resolve `SentenceTransformer` and `settings` through the store module
        # at call time: tests monkeypatch `store_mod.SentenceTransformer` /
        # `store_mod.settings` on the store module (see
        # tests/shared/test_incident_store.py), and a module-level import here
        # would bind the unpatched originals.
        import ai_classification.shared.store as store_mod
        SentenceTransformer = store_mod.SentenceTransformer
        settings = store_mod.settings
        if self._ready:
            return

        try:
            self._model = SentenceTransformer(settings.embedding_model_name, device="cpu")
        except Exception as exc:
            _log.warning("Failed to load embedding model '%s': %s. Similarity search disabled.",
                         settings.embedding_model_name, exc)
            self._model = None

        try:
            self._pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1, maxconn=25,
                host=settings.pg_host,
                port=settings.pg_port,
                user=settings.pg_user,
                password=settings.pg_password,
                dbname=settings.pg_database,
            )
            # Create schema
            conn = self._pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS incidents (
                            id TEXT PRIMARY KEY,
                            title TEXT NOT NULL,
                            description TEXT NOT NULL DEFAULT '',
                            extracted_text TEXT NOT NULL DEFAULT '',
                            embedding vector(%d),
                            classification_json TEXT NOT NULL DEFAULT '{}',
                            status TEXT NOT NULL DEFAULT 'active',
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            documents JSONB NOT NULL DEFAULT '[]',
                            assign_group TEXT NOT NULL DEFAULT '',
                            assignee TEXT NOT NULL DEFAULT '',
                            priority TEXT NOT NULL DEFAULT 'medium',
                            notes TEXT,
                            discussion_history JSONB NOT NULL DEFAULT '[]',
                            escalation_info TEXT,
                            completion_code TEXT
                        )
                    """ % VECTOR_DIM)
                    # HNSW index for fast ANN search on active incidents
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_incidents_embedding_active
                        ON incidents USING hnsw (embedding vector_cosine_ops)
                        WHERE status = 'active' AND embedding IS NOT NULL
                    """)
                    # Schema migrations for databases created before these columns existed
                    for col_sql in [
                        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS documents JSONB NOT NULL DEFAULT '[]'",
                        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS assign_group TEXT NOT NULL DEFAULT ''",
                        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS assignee TEXT NOT NULL DEFAULT ''",
                        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS priority TEXT NOT NULL DEFAULT 'medium'",
                        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS notes TEXT",
                        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS discussion_history JSONB NOT NULL DEFAULT '[]'",
                        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS escalation_info TEXT",
                        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS completion_code TEXT",
                        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS content_hash TEXT",
                        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS occurrence_count INTEGER NOT NULL DEFAULT 1",
                        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS first_seen TIMESTAMPTZ",
                        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ",
                        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS source_ticket_ids JSONB NOT NULL DEFAULT '[]'",
                        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS ticket_kind TEXT NOT NULL DEFAULT 'incident'",
                        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS classification_status TEXT NOT NULL DEFAULT 'ok'",
                    ]:
                        try:
                            cur.execute(col_sql)
                        except Exception:
                            pass

                    # ── Classifier v3 tables ───────────────────────────────
                    # taxonomy_gaps: abstentions made visible — the classifier
                    # found no offering for a service and recorded a suggested
                    # name; aggregated per (service, suggested_offering).
                    cur.execute("""
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
                    """)
                    # classification_log: append-only audit trail of every LLM
                    # decision (triage / cascade stages / verification).
                    cur.execute("""
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
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_classification_log_ref
                        ON classification_log (incident_ref, stage)
                    """)

                    # ── Sub-offering engine tables (Phase 2) ────────────────
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS sub_offerings (
                            id TEXT PRIMARY KEY,
                            offering_id TEXT NOT NULL,
                            name TEXT NOT NULL,
                            status TEXT NOT NULL DEFAULT 'proposed',
                            created_from_cluster_id TEXT NOT NULL DEFAULT '',
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_sub_offerings_offering_status
                        ON sub_offerings (offering_id, status)
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS sub_offering_exemplars (
                            id TEXT PRIMARY KEY,
                            sub_offering_id TEXT NOT NULL,
                            incident_id TEXT NOT NULL,
                            title TEXT NOT NULL,
                            description TEXT NOT NULL DEFAULT '',
                            embedding vector(%d),
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                    """ % VECTOR_DIM)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_exemplars_sub_offering
                        ON sub_offering_exemplars (sub_offering_id)
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS unmatched_pool (
                            offering_id TEXT NOT NULL,
                            incident_id TEXT NOT NULL,
                            added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            cooldown_until TIMESTAMPTZ,
                            PRIMARY KEY (offering_id, incident_id)
                        )
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS cluster_proposals (
                            id TEXT PRIMARY KEY,
                            offering_id TEXT NOT NULL,
                            member_ids JSONB NOT NULL DEFAULT '[]',
                            mean_sim DOUBLE PRECISION NOT NULL DEFAULT 0,
                            verifier_reasons JSONB NOT NULL DEFAULT '{}',
                            purity_flags JSONB NOT NULL DEFAULT '{}',
                            proposed_label TEXT NOT NULL DEFAULT '',
                            status TEXT NOT NULL DEFAULT 'pending',
                            decision TEXT NOT NULL DEFAULT '',
                            target_sub_offering_id TEXT NOT NULL DEFAULT '',
                            decision_note TEXT NOT NULL DEFAULT '',
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            decided_at TIMESTAMPTZ
                        )
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_proposals_offering_status
                        ON cluster_proposals (offering_id, status)
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS manual_review_queue (
                            incident_id TEXT PRIMARY KEY,
                            reason TEXT NOT NULL DEFAULT '',
                            attempts INTEGER NOT NULL DEFAULT 0,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            reviewed_at TIMESTAMPTZ
                        )
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_manual_review_created
                        ON manual_review_queue (created_at)
                    """)
                    # ── Persistent clusters (v2 LLM-first clustering) ────────
                    # Clusters are persistent rows, not rebuild artifacts: a
                    # cluster created today exists tomorrow with the same id,
                    # name and members, and grows incrementally via LLM-decided
                    # assignment (Flow A), pool sweeps (Flow B) and nightly
                    # audits (Flow C). status: proposed (human-gated) | active | retired.
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS clusters (
                            id            TEXT PRIMARY KEY,
                            name_ar       TEXT NOT NULL,
                            name_en       TEXT,
                            description   TEXT NOT NULL DEFAULT '',
                            status        TEXT NOT NULL DEFAULT 'proposed',
                            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_clusters_status
                        ON clusters (status)
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS cluster_members (
                            cluster_id    TEXT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
                            incident_id   TEXT NOT NULL,
                            assigned_by   TEXT NOT NULL DEFAULT 'llm',
                            confidence    TEXT,
                            assigned_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            PRIMARY KEY (cluster_id, incident_id)
                        )
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_cluster_members_incident
                        ON cluster_members (incident_id)
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS assignment_log (
                            id             SERIAL PRIMARY KEY,
                            incident_id    TEXT NOT NULL,
                            candidates     JSONB NOT NULL DEFAULT '[]',
                            verdict        JSONB NOT NULL DEFAULT '{}',
                            prompt_version TEXT NOT NULL DEFAULT '',
                            model_version  TEXT NOT NULL DEFAULT '',
                            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_assignment_log_incident
                        ON assignment_log (incident_id)
                    """)
                    # ── Taxonomy overrides (admin console) ──────────────────
                    # Admin-added services/offerings merged on top of the
                    # FROZEN code taxonomy at runtime (domain/taxonomy.py
                    # effective_* view). Never edits the base.
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS taxonomy_overrides (
                            system   TEXT NOT NULL,
                            service  TEXT NOT NULL,
                            offering TEXT NOT NULL DEFAULT '',
                            PRIMARY KEY (system, service, offering)
                        )
                    """)
                    # ── Assignment groups (admin console) ────────────────────
                    # The managed list of TEAMS an incident is routed to
                    # (assign_group on incidents) — Payments, Infrastructure,
                    # Operations, App Support... Admin CRUD; the Add-Incident
                    # form offers these as a dropdown. Free-text assign_group
                    # values from imports remain as-is (mapped by the
                    # frontend's mapTeam keywords); this list is the canonical
                    # set for NEW manual incidents.
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS assignment_groups (
                            id         SERIAL PRIMARY KEY,
                            name       TEXT NOT NULL UNIQUE,
                            description TEXT NOT NULL DEFAULT '',
                            sort_order INTEGER NOT NULL DEFAULT 0,
                            active     BOOLEAN NOT NULL DEFAULT TRUE,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                    """)
                    # Seed the four teams the dashboard already knows.
                    cur.execute("""
                        INSERT INTO assignment_groups (name, description, sort_order)
                        SELECT * FROM (VALUES
                            ('Payments',      'Payment, billing and visa issues', 1),
                            ('Infrastructure','Network, infra and gate problems',  2),
                            ('Operations',    'Transport, accommodation, health and field ops', 3),
                            ('App Support',   'Application support (default)',      4)
                        ) AS seed(name, description, sort_order)
                        WHERE NOT EXISTS (SELECT 1 FROM assignment_groups)
                    """)
                conn.commit()
            finally:
                self._pool.putconn(conn)

            _log.info("PostgreSQL store ready at %s:%s/%s",
                      settings.pg_host, settings.pg_port, settings.pg_database)
            self._ready = True
        except Exception as exc:
            _log.warning("Failed to connect to PostgreSQL: %s. Store disabled.", exc)
            self._pool = None
            self._ready = False

    def close(self) -> None:
        if self._pool:
            _log.info("Closing PostgreSQL connection pool")
            self._pool.closeall()

    @property
    def ready(self) -> bool:
        return self._ready

    # ── Embedding ──────────────────────────────────────────────────

    def embedding_ready(self) -> bool:
        """Public probe: is the embedding model loaded? (diagnostics use this
        instead of reaching into the private _model attribute.)"""
        return self._model is not None

    def embedding_dim(self) -> int | None:
        """Public probe: embedding dimension (None when the model isn't
        loaded). Used by /test/all instead of private _model access."""
        if self._model is None:
            return None
        v = self._model.encode("test ticket")
        return int(np.asarray(v).shape[-1])

    def _embed(self, text: str) -> np.ndarray | None:
        if self._model is None:
            return None
        vec = self._model.encode(text, normalize_embeddings=True)
        result = np.asarray(vec, dtype=np.float32)
        _log.debug("Embedding generated — input=%d chars, dim=%d", len(text), len(result))
        return result

    @staticmethod
    def _build_embedding_text(
        title: str, description: str, extracted_text: str = "",
        classification: ClassificationResult | None = None,
    ) -> str:
        parts = [title, description]
        if extracted_text:
            parts.append(f"OCR: {extracted_text}")
        text = " | ".join(p for p in parts if p).strip()
        if classification is not None:
            text += (
                f" | Classified as: {classification.affected_system} / "
                f"{classification.service} / {classification.incident_type} / "
                f"{classification.category}"
            )
        return text

    def generate_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def _getconn(self):
        if self._pool is None:
            raise RuntimeError("Store not connected")
        return self._pool.getconn()

    def _putconn(self, conn):
        if self._pool:
            self._pool.putconn(conn)
