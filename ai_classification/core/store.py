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

from ..config import settings
from ..domain.models import ClassificationResult
from ..sync import start_sync_worker

_log = logging.getLogger(__name__)

VECTOR_DIM = 1024  # BAAI/bge-m3 output dim

# Column names for the common incident SELECT (15 cols, 0-indexed).
# Used by _row_to_incident to map DB rows → dicts.
_INCIDENT_COLS: tuple[str, ...] = (
    "id", "title", "description", "extracted_text", "classification_json",
    "status", "created_at", "documents", "assign_group", "assignee", "priority",
    "notes", "discussion_history", "escalation_info", "completion_code",
)


@dataclass
class SimilarMatch:
    id: str
    title: str
    similarity: float
    classification: ClassificationResult


class IncidentStore:
    """PostgreSQL-backed store with pgvector cosine similarity."""

    def __init__(self) -> None:
        self._model: SentenceTransformer | None = None
        self._pool: psycopg2.pool.ThreadedConnectionPool | None = None
        self._ready = False

    # ── Lifecycle ──────────────────────────────────────────────────

    def setup(self) -> None:
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
                    ]:
                        try:
                            cur.execute(col_sql)
                        except Exception:
                            pass

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

    # ── Persist ────────────────────────────────────────────────────

    def update_classification(self, incident_id: str, classification_json: str) -> None:
        """Re-classification update (retry worker): replace the stored
        classification on an existing row without touching its identity,
        status, or occurrence bookkeeping."""
        if not self._ready or self._pool is None:
            return
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE incidents SET classification_json = %s WHERE id = %s",
                    (classification_json, incident_id))
            conn.commit()  # psycopg2 opens a txn implicitly — must commit or the write is rolled back on putconn
        finally:
            self._putconn(conn)
        self._invalidate_cluster_caches(incident_id)

    @staticmethod
    def _invalidate_cluster_caches(incident_id: str) -> None:
        """A ticket's cluster membership changed (moved / re-classified) →
        drop cached cluster names/verdicts containing it. Lazy import:
        grouping imports this module at load, so import grouping here at
        call time (no import cycle)."""
        try:
            from .grouping import invalidate_incident
            invalidate_incident(incident_id)
        except Exception:  # pragma: no cover — best-effort, never block writes
            pass

    def save_incident(
        self, incident_id: str, title: str, description: str,
        classification: ClassificationResult, extracted_text: str = "",
        documents: list[str] | None = None,
        assign_group: str = "",
        assignee: str = "",
        priority: str = "medium",
        status: str = "active",
        notes: str | None = None,
        discussion_history: list[dict] | None = None,
        escalation_info: str | None = None,
        completion_code: str | None = None,
        content_hash: str | None = None,
        source_ticket_ids: list[str] | None = None,
    ) -> None:
        if not self._ready or self._pool is None:
            return
        # Embed the TICKET'S OWN TEXT (title + description), never the FM
        # name or classification output. Earlier versions embedded the FM
        # display name / canonical statement — every ticket in the same FM
        # bucket got an IDENTICAL vector, so clustering similarity always
        # read 100%. Real text only: proven by the pairwise experiments
        # (pure title+"\n"+description is the only valid similarity signal).
        embed_text = self._build_embedding_text(title, description, extracted_text)
        embedding = self._embed(embed_text)
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO incidents "
                    "(id, title, description, extracted_text, embedding, classification_json, "
                    "status, created_at, documents, assign_group, assignee, priority, notes, "
                    "discussion_history, escalation_info, completion_code, "
                    "content_hash, occurrence_count, first_seen, last_seen, source_ticket_ids) "
                    "VALUES (%s, %s, %s, %s, %s::vector, %s, %s, %s, "
                    "%s::jsonb, %s, %s, %s, %s, %s::jsonb, %s, %s, "
                    "%s, 1, %s, %s, %s::jsonb) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "  title=EXCLUDED.title, description=EXCLUDED.description, "
                    "  extracted_text=EXCLUDED.extracted_text, embedding=EXCLUDED.embedding, "
                    "  classification_json=EXCLUDED.classification_json, "
                    "  status=EXCLUDED.status, "
                    "  documents=EXCLUDED.documents, assign_group=EXCLUDED.assign_group, "
                    "  assignee=EXCLUDED.assignee, priority=EXCLUDED.priority, "
                    "  notes=EXCLUDED.notes, discussion_history=EXCLUDED.discussion_history, "
                    "  escalation_info=EXCLUDED.escalation_info, "
                    "  completion_code=EXCLUDED.completion_code, "
                    "  content_hash=EXCLUDED.content_hash",
                    (
                        incident_id, title, description, extracted_text,
                        embedding.tolist() if embedding is not None else None,
                        classification.model_dump_json(),
                        status,
                        datetime.now(timezone.utc),
                        json.dumps(documents or []),
                        assign_group,
                        assignee,
                        priority,
                        notes,
                        json.dumps(discussion_history or []),
                        escalation_info,
                        completion_code,
                        content_hash,
                        datetime.now(timezone.utc),
                        datetime.now(timezone.utc),
                        json.dumps(source_ticket_ids or []),
                    ),
                )
            conn.commit()
            _log.info("Saved incident %s — system=%s, severity=%s",
                      incident_id, classification.affected_system, classification.severity)
        finally:
            self._putconn(conn)

    def increment_occurrence(self, incident_id: str) -> None:
        if not self._ready or self._pool is None:
            return
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE incidents SET occurrence_count = occurrence_count + 1, "
                    "last_seen = NOW() WHERE id = %s",
                    (incident_id,)
                )
                conn.commit()
        finally:
            self._putconn(conn)

    def set_status(self, incident_id: str, new_status: str) -> bool:
        """Update incident status. Maps external statuses to internal active/resolved."""
        if not self._ready or self._pool is None:
            return False
        local_status = "active" if new_status in ("open", "in_progress", "third_party") else "resolved"
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE incidents SET status = %s WHERE id = %s",
                    (local_status, incident_id),
                )
            conn.commit()
            return cur.rowcount > 0
        finally:
            self._putconn(conn)

    def resolve_incident(self, incident_id: str) -> bool:
        if not self._ready or self._pool is None:
            return False
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE incidents SET status = 'resolved' WHERE id = %s",
                    (incident_id,),
                )
            conn.commit()
            return cur.rowcount > 0
        finally:
            self._putconn(conn)

    def find_fallback_incidents(self, limit: int = 10) -> list[dict]:
        """Active incidents whose stored classification is the LLM-failure
        fallback (low confidence + reasoning starting with
        'Classification failed after'). These are the heal candidates."""
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, title, description, extracted_text FROM incidents "
                    "WHERE status = 'active' "
                    "AND classification_json::jsonb->>'confidence' = 'low' "
                    "AND classification_json::jsonb->>'reasoning' LIKE %s "
                    "LIMIT %s",
                    ("Classification failed after%", limit),
                )
                cols = ("id", "title", "description", "extracted_text")
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            self._putconn(conn)

    def reclassify_incident(
        self, incident_id: str, title: str, description: str,
        extracted_text: str, classification,
    ) -> bool:
        """Update an incident's classification + embedding in place (heal path).

        Embedding is recomputed from the TICKET'S OWN TEXT — same rule as
        save_incident (the embedding signal must stay the real ticket text)."""
        if not self._ready or self._pool is None:
            return False
        embed_text = self._build_embedding_text(title, description, extracted_text)
        embedding = self._embed(embed_text)
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE incidents SET classification_json = %s, embedding = %s "
                    "WHERE id = %s",
                    (classification.model_dump_json(),
                     embedding.tolist() if embedding is not None else None,
                     incident_id),
                )
            conn.commit()
            if cur.rowcount > 0:
                self._invalidate_cluster_caches(incident_id)
            return cur.rowcount > 0
        finally:
            self._putconn(conn)

    # ── Admin / Reset ──────────────────────────────────────────────

    def delete_all(self) -> int:
        """Delete all incidents. Returns count deleted."""
        if not self._ready or self._pool is None:
            return 0
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM incidents")
                count = cur.rowcount
            conn.commit()
            _log.warning("Deleted all incidents — count=%d", count)
            return count
        finally:
            self._putconn(conn)

    # ── Similarity search (pgvector ANN) ──────────────────────────

    def find_similar(
        self, text: str, *,
        extracted_text: str = "",
        threshold: float | None = None,
        top_k: int = 5,
        classification: ClassificationResult | None = None,
    ) -> list[SimilarMatch]:
        if not self._ready or self._pool is None or self._model is None:
            return []
        threshold = threshold if threshold is not None else settings.similarity_threshold
        # settings.similarity_threshold (0.80) is deliberately stricter than
        # grouping.SIMILARITY_THRESHOLD (0.50) — this is fine-grained dedupe
        # on classify (must be near-identical), not a recall-oriented grouping pass.
        # Query and stored embeddings both use canonical_statement directly
        embed_text = text
        if extracted_text:
            embed_text = f"{extracted_text} | {text}"
        query_vec = self._embed(embed_text)
        if query_vec is None:
            return []

        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, title, classification_json, "
                    "  (1 - (embedding <=> %s::vector)) AS similarity "
                    "FROM incidents "
                    "WHERE status = 'active' AND embedding IS NOT NULL "
                    "  AND (1 - (embedding <=> %s::vector)) >= %s "
                    "ORDER BY embedding <=> %s::vector "
                    "LIMIT %s",
                    (query_vec.tolist(), query_vec.tolist(), threshold, query_vec.tolist(), top_k),
                )
                results = []
                for row_id, row_title, class_json, sim in cur.fetchall():
                    try:
                        cls_result = ClassificationResult.model_validate(json.loads(class_json))
                    except Exception:
                        continue
                    results.append(SimilarMatch(id=row_id, title=row_title, similarity=float(sim), classification=cls_result))
                _log.info("Similarity search — query='%s', threshold=%.2f, matches=%d",
                          text[:60], threshold, len(results))
                for m in results:
                    _log.debug("  Match: %s — %.1f%% — %s", m.id, m.similarity * 100, m.title[:50])
                return results
        finally:
            self._putconn(conn)

    def get_incident_by_hash(self, content_hash: str) -> dict | None:
        if not self._ready or self._pool is None:
            return None
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, occurrence_count, first_seen, last_seen, "
                    "source_ticket_ids, classification_json "
                    "FROM incidents WHERE content_hash = %s AND status = 'active' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (content_hash,)
                )
                row = cur.fetchone()
                if row:
                    return {
                        "id": row[0],
                        "occurrence_count": row[1],
                        "first_seen": row[2],
                        "last_seen": row[3],
                        "source_ticket_ids": row[4] if isinstance(row[4], list) else (json.loads(row[4]) if row[4] else []),
                        "classification_json": row[5],
                    }
                return None
        finally:
            self._putconn(conn)

    def get_incident_by_source_ticket_id(self, ticket_id: str) -> dict | None:
        """Look up an incident by its originating ticket ID (source_ticket_ids JSONB)."""
        if not self._ready or self._pool is None:
            return None
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, title, description, extracted_text, classification_json, "
                    "status, created_at, documents, assign_group, assignee, priority, notes, "
                    "discussion_history, escalation_info, completion_code, "
                    "content_hash, occurrence_count, first_seen, last_seen, source_ticket_ids "
                    "FROM incidents WHERE source_ticket_ids @> %s::jsonb "
                    "ORDER BY created_at DESC LIMIT 1",
                    (json.dumps([ticket_id]),),
                )
                row = cur.fetchone()
            if row is None:
                return None
            return self._row_to_incident(row, extended=True)
        finally:
            self._putconn(conn)

    # ── Read ─────────────────────────────────────────────────────

    def get_incident(self, incident_id: str) -> dict | None:
        if not self._ready or self._pool is None:
            return None
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, title, description, extracted_text, classification_json, "
                    "status, created_at, documents, assign_group, assignee, priority, notes, "
                    "discussion_history, escalation_info, completion_code, "
                    "content_hash, occurrence_count, first_seen, last_seen, source_ticket_ids "
                    "FROM incidents WHERE id = %s",
                    (incident_id,),
                )
                row = cur.fetchone()
            if row is None:
                return None
            return self._row_to_incident(row, extended=True)
        finally:
            self._putconn(conn)

    def list_incidents(self, status: str | None = None) -> list[dict]:
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cols = ("id, title, description, extracted_text, classification_json, "
                        "status, created_at, documents, assign_group, assignee, priority, "
                        "notes, discussion_history, escalation_info, completion_code")
                if status:
                    cur.execute(
                        f"SELECT {cols} FROM incidents WHERE status = %s ORDER BY created_at DESC",
                        (status,),
                    )
                else:
                    cur.execute(
                        f"SELECT {cols} FROM incidents ORDER BY created_at DESC"
                    )
                rows = cur.fetchall()
            return [self._row_to_incident(r) for r in rows]
        finally:
            self._putconn(conn)

    def list_incidents_with_embeddings(self) -> list[tuple[dict, np.ndarray | None]]:
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, title, description, extracted_text, classification_json, "
                    "status, created_at, documents, assign_group, assignee, "
                    "priority, notes, discussion_history, escalation_info, completion_code, "
                    "embedding::text "
                    "FROM incidents "
                    "ORDER BY created_at DESC"
                )
                rows = cur.fetchall()
            result = []
            for r in rows:
                emb_str = r[15]
                emb = None
                if emb_str:
                    emb = np.array([float(x) for x in emb_str.strip("[]").split(",")], dtype=np.float32)
                result.append((self._row_to_incident(r, embedding_str=emb_str), emb))
            return result
        finally:
            self._putconn(conn)

    def generate_id(self) -> str:
        return uuid.uuid4().hex[:12]

    # ── Sub-offering engine (Phase 2) ────────────────────────────────
    def create_sub_offering(self, offering_id: str, name: str,
                            created_from_cluster_id: str = "",
                            status: str = "active") -> dict | None:
        if not self._ready or self._pool is None:
            return None
        sid = self.generate_id()
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sub_offerings "
                    "(id, offering_id, name, status, created_from_cluster_id) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id, offering_id, name, status, "
                    "created_from_cluster_id, created_at",
                    (sid, offering_id, name, status, created_from_cluster_id),
                )
                row = cur.fetchone()
            conn.commit()
            return self._row_to_sub_offering(row) if row else None
        finally:
            self._putconn(conn)

    def list_sub_offerings(self, offering_id: str | None = None,
                           status: str | None = "active") -> list[dict]:
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                sql = "SELECT id, offering_id, name, status, created_from_cluster_id, created_at FROM sub_offerings"
                where, args = [], []
                if offering_id is not None:
                    where.append("offering_id = %s")
                    args.append(offering_id)
                if status is not None:
                    where.append("status = %s")
                    args.append(status)
                if where:
                    sql += " WHERE " + " AND ".join(where)
                sql += " ORDER BY created_at DESC"
                cur.execute(sql, args)
                return [self._row_to_sub_offering(r) for r in cur.fetchall()]
        finally:
            self._putconn(conn)

    def get_sub_offering(self, sub_offering_id: str) -> dict | None:
        if not self._ready or self._pool is None:
            return None
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, offering_id, name, status, created_from_cluster_id, created_at "
                    "FROM sub_offerings WHERE id = %s", (sub_offering_id,))
                row = cur.fetchone()
            return self._row_to_sub_offering(row) if row else None
        finally:
            self._putconn(conn)

    def set_sub_offering_status(self, sub_offering_id: str, status: str) -> bool:
        if not self._ready or self._pool is None:
            return False
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE sub_offerings SET status = %s WHERE id = %s",
                            (status, sub_offering_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            self._putconn(conn)

    def add_exemplar(self, sub_offering_id: str, incident_id: str, title: str,
                     description: str, embedding) -> dict | None:
        if not self._ready or self._pool is None:
            return None
        eid = self.generate_id()
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sub_offering_exemplars "
                    "(id, sub_offering_id, incident_id, title, description, embedding) "
                    "VALUES (%s, %s, %s, %s, %s, %s::vector) "
                    "RETURNING id, sub_offering_id, incident_id, title, created_at",
                    (eid, sub_offering_id, incident_id, title, description,
                     embedding.tolist() if embedding is not None else None),
                )
                row = cur.fetchone()
            conn.commit()
            return {"id": row[0], "sub_offering_id": row[1], "incident_id": row[2],
                    "title": row[3], "created_at": row[4]} if row else None
        finally:
            self._putconn(conn)

    def list_exemplars(self, sub_offering_id: str) -> list[dict]:
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, sub_offering_id, incident_id, title, description, "
                    "embedding::text, created_at FROM sub_offering_exemplars "
                    "WHERE sub_offering_id = %s ORDER BY created_at", (sub_offering_id,))
                out = []
                for r in cur.fetchall():
                    out.append({"id": r[0], "sub_offering_id": r[1], "incident_id": r[2],
                                "title": r[3], "description": r[4],
                                "embedding": r[5], "created_at": r[6]})
                return out
        finally:
            self._putconn(conn)

    # ── Manual review queue (Recovery: exhausted retries) ─────────────

    def queue_add(self, incident_id: str, reason: str = "") -> None:
        if not self._ready or self._pool is None:
            return
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO manual_review_queue (incident_id, reason) "
                    "VALUES (%s, %s) ON CONFLICT (incident_id) DO UPDATE SET "
                    "attempts = manual_review_queue.attempts + 1, "
                    "reason = EXCLUDED.reason",
                    (incident_id, reason))
        finally:
            self._putconn(conn)

    def queue_list(self) -> list[dict]:
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT incident_id, reason, attempts, created_at "
                    "FROM manual_review_queue WHERE reviewed_at IS NULL "
                    "ORDER BY created_at")
                return [{"incident_id": r[0], "reason": r[1], "attempts": r[2],
                         "created_at": r[3]} for r in cur.fetchall()]
        finally:
            self._putconn(conn)

    def pool_add(self, offering_id: str, incident_id: str) -> None:
        if not self._ready or self._pool is None:
            return
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO unmatched_pool (offering_id, incident_id) VALUES (%s, %s) "
                    "ON CONFLICT (offering_id, incident_id) DO NOTHING",
                    (offering_id, incident_id))
            conn.commit()
        finally:
            self._putconn(conn)

    def pool_remove(self, offering_id: str, incident_id: str) -> None:
        if not self._ready or self._pool is None:
            return
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM unmatched_pool WHERE offering_id = %s AND incident_id = %s",
                            (offering_id, incident_id))
            conn.commit()
        finally:
            self._putconn(conn)
        self._invalidate_cluster_caches(incident_id)

    def pool_remove_many(self, offering_id: str, incident_ids: list[str]) -> None:
        if not self._ready or self._pool is None or not incident_ids:
            return
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                for iid in incident_ids:
                    cur.execute("DELETE FROM unmatched_pool WHERE offering_id = %s AND incident_id = %s",
                                (offering_id, iid))
            conn.commit()
        finally:
            self._putconn(conn)
        for iid in incident_ids:
            self._invalidate_cluster_caches(iid)

    def pool_set_cooldown(self, offering_id: str, incident_ids: list[str], until) -> None:
        """Rejected-proposal cooldown: members stay in pool (upsert) but are
        excluded from batch clustering until `until` (datetime)."""
        if not self._ready or self._pool is None or not incident_ids:
            return
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                for iid in incident_ids:
                    cur.execute(
                        "INSERT INTO unmatched_pool (offering_id, incident_id, cooldown_until) "
                        "VALUES (%s, %s, %s) "
                        "ON CONFLICT (offering_id, incident_id) DO UPDATE SET cooldown_until = %s",
                        (offering_id, iid, until, until))
            conn.commit()
        finally:
            self._putconn(conn)

    def pool_list(self, offering_id: str | None = None) -> list[dict]:
        """Pool members with incident data. Returns [{incident_id, title, cooldown_until}]."""
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                sql = ("SELECT p.incident_id, i.title, p.cooldown_until FROM unmatched_pool p "
                       "LEFT JOIN incidents i ON i.id = p.incident_id")
                args = []
                if offering_id is not None:
                    sql += " WHERE p.offering_id = %s"
                    args.append(offering_id)
                sql += " ORDER BY p.offering_id, p.added_at"
                cur.execute(sql, args)
                return [{"incident_id": r[0], "title": r[1], "cooldown_until": r[2]}
                        for r in cur.fetchall()]
        finally:
            self._putconn(conn)

    def pool_clear(self, offering_id: str | None = None) -> int:
        if not self._ready or self._pool is None:
            return 0
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                if offering_id is None:
                    cur.execute("DELETE FROM unmatched_pool")
                else:
                    cur.execute("DELETE FROM unmatched_pool WHERE offering_id = %s", (offering_id,))
                n = cur.rowcount
            conn.commit()
            return n
        finally:
            self._putconn(conn)

    def create_proposal(self, offering_id: str, member_ids: list[str], mean_sim: float,
                        verifier_reasons: dict, purity_flags: dict,
                        proposed_label: str = "") -> dict | None:
        if not self._ready or self._pool is None:
            return None
        pid = self.generate_id()
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO cluster_proposals "
                    "(id, offering_id, member_ids, mean_sim, verifier_reasons, purity_flags, "
                    "proposed_label) VALUES (%s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb, %s) "
                    "RETURNING id, offering_id, member_ids, mean_sim, verifier_reasons, "
                    "purity_flags, proposed_label, status, decision, "
                    "target_sub_offering_id, decision_note, created_at, decided_at",
                    (pid, offering_id, json.dumps(member_ids), mean_sim,
                     json.dumps(verifier_reasons), json.dumps(purity_flags), proposed_label),
                )
                row = cur.fetchone()
            conn.commit()
            return self._row_to_proposal(row) if row else None
        finally:
            self._putconn(conn)

    def _enrich_proposal_members(self, proposals: list[dict]) -> list[dict]:
        """Attach member ticket texts (id, title, description, failure_mode) so a
        human reviewer can check each member's real text against the verifier
        reasons — the gate that makes the proposal queue honest."""
        if not proposals or not self._ready or self._pool is None:
            return proposals
        member_ids = sorted({mid for p in proposals for mid in p["member_ids"]})
        if not member_ids:
            return proposals
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, title, description, classification_json "
                    "FROM incidents WHERE id = ANY(%s)", (member_ids,))
                rows = cur.fetchall()
        finally:
            self._putconn(conn)
        by_id = {}
        for rid, title, desc, cj in rows:
            try:
                fm = json.loads(cj).get("failure_mode", "")
            except Exception:
                fm = ""
            by_id[rid] = {"id": rid, "title": title, "description": desc,
                          "failure_mode": fm}
        for p in proposals:
            p["members"] = [by_id[mid] for mid in p["member_ids"] if mid in by_id]
        return proposals

    def list_proposals(self, status: str | None = None,
                       offering_id: str | None = None) -> list[dict]:
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                sql = ("SELECT id, offering_id, member_ids, mean_sim, verifier_reasons, "
                       "purity_flags, proposed_label, status, decision, "
                       "target_sub_offering_id, decision_note, created_at, decided_at "
                       "FROM cluster_proposals")
                where, args = [], []
                if status is not None:
                    where.append("status = %s")
                    args.append(status)
                if offering_id is not None:
                    where.append("offering_id = %s")
                    args.append(offering_id)
                if where:
                    sql += " WHERE " + " AND ".join(where)
                sql += " ORDER BY created_at DESC"
                cur.execute(sql, args)
                return self._enrich_proposal_members(
                    [self._row_to_proposal(r) for r in cur.fetchall()])
        finally:
            self._putconn(conn)

    def get_proposal(self, proposal_id: str) -> dict | None:
        if not self._ready or self._pool is None:
            return None
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, offering_id, member_ids, mean_sim, verifier_reasons, "
                    "purity_flags, proposed_label, status, decision, "
                    "target_sub_offering_id, decision_note, created_at, decided_at "
                    "FROM cluster_proposals WHERE id = %s", (proposal_id,))
                row = cur.fetchone()
            if row is None:
                return None
            return self._enrich_proposal_members([self._row_to_proposal(row)])[0]
        finally:
            self._putconn(conn)

    def decide_proposal(self, proposal_id: str, decision: str,
                        target_sub_offering_id: str = "", note: str = "",
                        decided_by: str = "admin") -> dict | None:
        """One-shot decision: only pending -> approved/rejected/merged. Returns the
        updated proposal, or the existing row if already decided (no-op)."""
        if not self._ready or self._pool is None:
            return None
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE cluster_proposals SET status = %s, decision = %s, "
                    "target_sub_offering_id = %s, decision_note = %s, decided_at = NOW() "
                    "WHERE id = %s AND status = 'pending'",
                    (decision, decision, target_sub_offering_id, note, proposal_id))
                conn.commit()
                if cur.rowcount == 0:
                    return self.get_proposal(proposal_id)  # already decided / not found
                return self.get_proposal(proposal_id)
        finally:
            self._putconn(conn)

    def delete_all_proposals(self) -> int:
        if not self._ready or self._pool is None:
            return 0
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM cluster_proposals")
                n = cur.rowcount
            conn.commit()
            return n
        finally:
            self._putconn(conn)

    @staticmethod
    def _row_to_sub_offering(row) -> dict:
        return {"id": row[0], "offering_id": row[1], "name": row[2], "status": row[3],
                "created_from_cluster_id": row[4], "created_at": row[5]}

    @staticmethod
    def _row_to_proposal(row) -> dict:
        return {"id": row[0], "offering_id": row[1],
                "member_ids": json.loads(row[2]) if isinstance(row[2], str) else row[2],
                "mean_sim": float(row[3]),
                "verifier_reasons": json.loads(row[4]) if isinstance(row[4], str) else row[4],
                "purity_flags": json.loads(row[5]) if isinstance(row[5], str) else row[5],
                "proposed_label": row[6], "status": row[7], "decision": row[8],
                "target_sub_offering_id": row[9], "decision_note": row[10],
                "created_at": row[11], "decided_at": row[12]}

    @staticmethod
    def _row_to_incident(row, *, embedding_str: str | None = None,
                         extended: bool = False) -> dict:
        """Convert a DB row to an incident dict.

        Handles three query shapes:
          - Base (15 cols): list_incidents
          - Extended (20 cols, extended=True): get_incident
          - Embedding (16 cols, embedding_str set): list_incidents_with_embeddings

        The 15 base columns are always at indices 0–14.
        Extended fields are at 15–19.
        Embedding text is passed separately (appended to SELECT as last col).
        """
        d = {
            "id": row[0],
            "title": row[1],
            "description": row[2],
            "extracted_text": row[3],
            "classification": row[4],
            "status": row[5],
            "created_at": row[6].isoformat() if row[6] else "",
            "documents": row[7] if isinstance(row[7], list) else (json.loads(row[7]) if row[7] else []),
            "assign_group": row[8] or "",
            "assignee": row[9] or "",
            "priority": row[10] or "medium",
            "notes": row[11],
            "discussion_history": row[12] if isinstance(row[12], list) else (json.loads(row[12]) if row[12] else []),
            "escalation_info": row[13],
            "completion_code": row[14],
        }
        # Parse classification_json once → classification_dict
        raw = row[4]
        if isinstance(raw, str):
            try:
                d["classification_dict"] = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, TypeError):
                d["classification_dict"] = {}
        else:
            d["classification_dict"] = raw or {}
        if extended:
            d.update({
                "content_hash": row[15],
                "occurrence_count": row[16] or 1,
                "first_seen": row[17].isoformat() if row[17] else "",
                "last_seen": row[18].isoformat() if row[18] else "",
                "source_ticket_ids": row[19] if isinstance(row[19], list) else (json.loads(row[19]) if row[19] else []),
            })
        if embedding_str is not None:
            d["_embedding_raw"] = embedding_str
        return d
    def _getconn(self):
        if self._pool is None:
            raise RuntimeError("Store not connected")
        return self._pool.getconn()

    def _putconn(self, conn):
        if self._pool:
            self._pool.putconn(conn)



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

    from ..seams.repool import start_repool_worker
    start_repool_worker()

    from ..core.grouping import start_rebuild_loop
    start_rebuild_loop()

    # Service status monitor — loud logging when any service (esp. the LLM
    # endpoint) is unreachable; state exposed via GET /status.
    from ..core.status_monitor import monitor
    monitor.start()

    # E1-E9 integration worker (async ingest queue) — gated so tests can
    # drive the queue synchronously (INTEGRATION_WORKER_ENABLED=0).
    if settings.integration_worker_enabled:
        from ..integration import start_integration_worker
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
