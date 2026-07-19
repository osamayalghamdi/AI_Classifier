"""PostgreSQL + pgvector incident store, plus app lifecycle and store-facing
service calls.

Uses pgvector for indexed cosine similarity. Thread-safe via a connection pool.
"""

import json
import logging
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
                conn.commit()
            finally:
                self._pool.putconn(conn)

            _log.info("PostgreSQL store ready at %s:%s/%s",
                      settings.pg_host, settings.pg_port, settings.pg_database)
        except Exception as exc:
            _log.warning("Failed to connect to PostgreSQL: %s. Store disabled.", exc)
            self._pool = None

        self._ready = True

    def close(self) -> None:
        if self._pool:
            _log.info("Closing PostgreSQL connection pool")
            self._pool.closeall()

    @property
    def ready(self) -> bool:
        return self._ready

    def _getconn(self):
        if self._pool is None:
            raise RuntimeError("Store not connected")
        return self._pool.getconn()

    def _putconn(self, conn):
        if self._pool:
            self._pool.putconn(conn)

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

    # ── Persist ────────────────────────────────────────────────────

    def save_incident(
        self, incident_id: str, title: str, description: str,
        classification: ClassificationResult, extracted_text: str = "",
        documents: list[str] | None = None,
        assign_group: str = "",
        assignee: str = "",
        priority: str = "medium",
        notes: str | None = None,
        discussion_history: list[dict] | None = None,
        escalation_info: str | None = None,
        completion_code: str | None = None,
        content_hash: str | None = None,
        source_ticket_ids: list[str] | None = None,
    ) -> None:
        if not self._ready or self._pool is None:
            return
        # Embed the signature — use taxonomy string for exact match when FM code is known
        # FM-000 means no taxonomy match → fall back to LLM-generated signature
        fm_code = getattr(classification, 'failure_mode', 'FM-000') or 'FM-000'
        if fm_code != 'FM-000':
            try:
                from .failure_modes import FAILURE_MODES
                fm_name = FAILURE_MODES[fm_code][0]  # (name, system, service, severity, includes, excludes)
                raw_cs = fm_name
            except (KeyError, ImportError):
                raw_cs = classification.signature or classification.canonical_statement or self._build_embedding_text(
                    title, description, extracted_text, classification
                )
        else:
            raw_cs = classification.signature or classification.canonical_statement or self._build_embedding_text(
                title, description, extracted_text, classification
            )
        # Strip label prefix before embedding — "Nusuk Masar Haj/contracts: " → removed
        if ":" in raw_cs:
            embed_text = raw_cs.split(":", 1)[1].strip()
        else:
            embed_text = raw_cs
        embedding = self._embed(embed_text)
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                import json as _json
                cur.execute(
                    "INSERT INTO incidents "
                    "(id, title, description, extracted_text, embedding, classification_json, "
                    "status, created_at, documents, assign_group, assignee, priority, notes, "
                    "discussion_history, escalation_info, completion_code, "
                    "content_hash, occurrence_count, first_seen, last_seen, source_ticket_ids) "
                    "VALUES (%s, %s, %s, %s, %s::vector, %s, 'active', %s, "
                    "%s::jsonb, %s, %s, %s, %s, %s::jsonb, %s, %s, "
                    "%s, 1, %s, %s, %s::jsonb) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "  title=EXCLUDED.title, description=EXCLUDED.description, "
                    "  extracted_text=EXCLUDED.extracted_text, embedding=EXCLUDED.embedding, "
                    "  classification_json=EXCLUDED.classification_json, "
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
                        datetime.now(timezone.utc),
                        _json.dumps(documents or []),
                        assign_group,
                        assignee,
                        priority,
                        notes,
                        _json.dumps(discussion_history or []),
                        escalation_info,
                        completion_code,
                        content_hash,
                        datetime.now(timezone.utc),
                        datetime.now(timezone.utc),
                        _json.dumps(source_ticket_ids or []),
                    ),
                )
            conn.commit()
            _log.info("Saved incident %s — system=%s, severity=%s",
                      incident_id, classification.affected_system, classification.severity)
        finally:
            self._putconn(conn)

    def generate_id(self) -> str:
        return uuid.uuid4().hex[:12]

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
                    import json as _json
                    return {
                        "id": row[0],
                        "occurrence_count": row[1],
                        "first_seen": row[2],
                        "last_seen": row[3],
                        "source_ticket_ids": row[4] if isinstance(row[4], list) else (_json.loads(row[4]) if row[4] else []),
                        "classification_json": row[5],
                    }
                return None
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

    # ── Sync from external ticketing system ───────────────────────

    def upsert_from_external(self, incident_id: str, title: str, description: str,
                             status: str, created_at: str) -> bool:
        if not self._ready or self._pool is None:
            return False
        local_status = "active" if status in ("open", "in_progress", "third_party") else "resolved"
        text = f"{title} {description}"
        embedding = self._embed(text)
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO incidents "
                    "(id, title, description, extracted_text, embedding, classification_json, status, created_at) "
                    "VALUES (%s, %s, %s, '', %s::vector, '{}', %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "  title=EXCLUDED.title, description=EXCLUDED.description, "
                    "  status=EXCLUDED.status",
                    (
                        incident_id, title, description,
                        embedding.tolist() if embedding is not None else None,
                        local_status, self._parse_ts(created_at),
                    ),
                )
            conn.commit()
            _log.info("Upserted external incident %s — status=%s", incident_id, status)
            return True
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
                    "discussion_history, escalation_info, completion_code "
                    "FROM incidents WHERE id = %s",
                    (incident_id,),
                )
                row = cur.fetchone()
            if row is None:
                return None
            import json as _json
            return {
                "id": row[0],
                "title": row[1],
                "description": row[2],
                "extracted_text": row[3],
                "classification": row[4],
                "status": row[5],
                "created_at": row[6].isoformat() if row[6] else "",
                "documents": row[7] if isinstance(row[7], list) else (_json.loads(row[7]) if row[7] else []),
                "assign_group": row[8] or "",
                "assignee": row[9] or "",
                "priority": row[10] or "medium",
                "notes": row[11],
                "discussion_history": row[12] if isinstance(row[12], list) else (_json.loads(row[12]) if row[12] else []),
                "escalation_info": row[13],
                "completion_code": row[14],
            }
        finally:
            self._putconn(conn)

    def list_incidents_with_embeddings(self) -> list[tuple[dict, np.ndarray | None]]:
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                import json as _json
                cur.execute(
                    "SELECT id, title, description, extracted_text, classification_json, "
                    "status, created_at, embedding::text, documents, assign_group, assignee, "
                    "priority, notes, discussion_history, escalation_info, completion_code "
                    "FROM incidents WHERE status = 'active' "
                    "ORDER BY created_at DESC"
                )
                rows = cur.fetchall()
            result = []
            for r in rows:
                emb = None
                if r[7]:
                    emb = np.array([float(x) for x in r[7].strip("[]").split(",")], dtype=np.float32)
                result.append((
                    {
                        "id": r[0],
                        "title": r[1],
                        "description": r[2],
                        "extracted_text": r[3],
                        "classification": r[4],
                        "status": r[5],
                        "created_at": r[6].isoformat() if r[6] else "",
                        "documents": r[8] if isinstance(r[8], list) else (_json.loads(r[8]) if r[8] else []),
                        "assign_group": r[9] or "",
                        "assignee": r[10] or "",
                        "priority": r[11] or "medium",
                        "notes": r[12],
                        "discussion_history": r[13] if isinstance(r[13], list) else (_json.loads(r[13]) if r[13] else []),
                        "escalation_info": r[14],
                        "completion_code": r[15],
                    },
                    emb,
                ))
            return result
        finally:
            self._putconn(conn)

    def list_incidents(self, status: str | None = None) -> list[dict]:
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                import json as _json
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
            return [
                {
                    "id": r[0],
                    "title": r[1],
                    "description": r[2],
                    "extracted_text": r[3],
                    "classification": r[4],
                    "status": r[5],
                    "created_at": r[6].isoformat() if r[6] else "",
                    "documents": r[7] if isinstance(r[7], list) else (_json.loads(r[7]) if r[7] else []),
                    "assign_group": r[8] or "",
                    "assignee": r[9] or "",
                    "priority": r[10] or "medium",
                    "notes": r[11],
                    "discussion_history": r[12] if isinstance(r[12], list) else (_json.loads(r[12]) if r[12] else []),
                    "escalation_info": r[13],
                    "completion_code": r[14],
                }
                for r in rows
            ]
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

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _parse_ts(ts: str) -> datetime:
        try:
            return datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return datetime.now(timezone.utc)


# ── Module-level singleton + app lifecycle ──────────────────────────────

store = IncidentStore()


# Start/stop app: init store, begin background sync
@asynccontextmanager
async def lifespan(app: FastAPI):
    _log.info("Starting app — model=%s, host=%s:%s", settings.llm_model, settings.host, settings.port)
    store.setup()
    if store.ready:
        _log.info("Store ready")
    else:
        _log.warning("Store FAILED (embeddings disabled)")

    start_sync_worker(store)

    from ..core.grouping import start_rebuild_loop
    start_rebuild_loop()

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
