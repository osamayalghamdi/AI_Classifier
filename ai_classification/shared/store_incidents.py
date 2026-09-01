"""Incident CRUD + similarity + queue store surface (IncidentsMixin).

Extracted from store.py (refactor C-1): incident persistence, pgvector
similarity search, hashing/dedupe lookups, occurrence bookkeeping, the manual
review queue, and row→dict mapping.

Pipeline position: 40_store — Postgres/pgvector persistence."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import numpy as np

from ai_classification.domain.models import ClassificationResult

_log = logging.getLogger(__name__)

# ── Status domain ─────────────────────────────────────────────────────
# Incidents keep TWO statuses:
#   - `status`        — the LOCAL view: "active" | "resolved". Drives dedupe
#                       (only active incidents are dedupe candidates) and the
#                       dashboard filters.
#   - `source_status` — the RAW status exactly as the ticketing system
#                       reported it (SMAX webhook / ingestion). DYNAMIC: any
#                       value is accepted and stored verbatim, so a new
#                       upstream status never breaks ingestion.
# The local view is DERIVED from the source status by this rule; the raw
# value is never lost. Extend the set freely — unknown statuses default to
# "active" (the safe default for dedupe).
_RESOLVED_LIKE = frozenset({
    "resolved", "closed", "verified", "cancelled", "canceled", "rejected",
    "duplicate", "completed", "done", "fixed", "withdrawn", "invalid",
})


def to_local_status(source_status: str | None) -> str:
    """Map a raw (external) status to the local active/resolved view."""
    if not source_status:
        return "active"
    return "resolved" if source_status.strip().lower() in _RESOLVED_LIKE else "active"


class IncidentsMixin:
    """Incident CRUD, dedupe hashes, occurrence, similarity, review queue."""

    # ── Persist ────────────────────────────────────────────────────

    def update_classification(
        self, incident_id: str, classification_json: str, *,
        ticket_kind: str | None = None,
        classification_status: str | None = None,
    ) -> None:
        """Re-classification update (retry worker): replace the stored
        classification on an existing row without touching its identity,
        status, or occurrence bookkeeping.

        v3: optionally persists the ticket_kind / classification_status
        columns when the caller passes them (the v3 classify path passes
        result.ticket_kind.value and result.classification_status); legacy
        callers omit them and the columns stay untouched."""
        if not self._ready or self._pool is None:
            return
        sets = ["classification_json = %s"]
        args: list = [classification_json]
        if ticket_kind is not None:
            sets.append("ticket_kind = %s")
            args.append(ticket_kind)
        if classification_status is not None:
            sets.append("classification_status = %s")
            args.append(classification_status)
        args.append(incident_id)
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE incidents SET {', '.join(sets)} WHERE id = %s",
                    args)
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
            from ai_classification.services.cluster.grouping import invalidate_incident
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
        source_status: str | None = None,
        notes: str | None = None,
        discussion_history: list[dict] | None = None,
        escalation_info: str | None = None,
        completion_code: str | None = None,
        content_hash: str | None = None,
        source_ticket_ids: list[str] | None = None,
        ticket_kind: str = "incident",
        classification_status: str = "ok",
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
                    "status, source_status, created_at, documents, assign_group, assignee, priority, notes, "
                    "discussion_history, escalation_info, completion_code, "
                    "ticket_kind, classification_status, "
                    "content_hash, occurrence_count, first_seen, last_seen, source_ticket_ids) "
                    "VALUES (%s, %s, %s, %s, %s::vector, %s, %s, %s, %s, "
                    "%s::jsonb, %s, %s, %s, %s, %s::jsonb, %s, %s, "
                    "%s, %s, "
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
                    "  ticket_kind=EXCLUDED.ticket_kind, "
                    "  classification_status=EXCLUDED.classification_status, "
                    "  content_hash=EXCLUDED.content_hash",
                    (
                        incident_id, title, description, extracted_text,
                        embedding.tolist() if embedding is not None else None,
                        classification.model_dump_json(),
                        status,
                        source_status,
                        datetime.now(timezone.utc),
                        json.dumps(documents or []),
                        assign_group,
                        assignee,
                        priority,
                        notes,
                        json.dumps(discussion_history or []),
                        escalation_info,
                        completion_code,
                        ticket_kind,
                        classification_status,
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
        """Update an incident's status from a raw (external) status value.

        Derives the local active/resolved view (to_local_status) and stores
        the raw value verbatim in source_status so nothing upstream reported
        is lost. Touches last_seen so the update is visible as activity."""
        if not self._ready or self._pool is None:
            return False
        local_status = to_local_status(new_status)
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE incidents SET status = %s, source_status = %s, last_seen = NOW() "
                    "WHERE id = %s",
                    (local_status, new_status, incident_id),
                )
            conn.commit()
            return cur.rowcount > 0
        finally:
            self._putconn(conn)

    def update_status_by_reference(self, source_reference: str, source_status: str) -> dict | None:
        """SMAX webhook path: status-only update keyed on the SOURCE reference.

        Same reference → same incident row: updates status + source_status
        WITHOUT re-classifying and WITHOUT creating a new row. Returns the
        updated incident dict, or None when no incident carries that
        reference (caller decides: 404 or treat as new)."""
        if not self._ready or self._pool is None:
            return None
        existing = self.get_incident_by_source_ticket_id(source_reference)
        if existing is None:
            return None
        incident_id = existing["id"]
        local_status = to_local_status(source_status)
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE incidents SET status = %s, source_status = %s, last_seen = NOW() "
                    "WHERE id = %s",
                    (local_status, source_status, incident_id),
                )
            conn.commit()
        finally:
            self._putconn(conn)
        return self.get_incident(incident_id)

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
                    "AND (classification_json::jsonb->>'reasoning' LIKE %s "
                    "     OR classification_status = 'failed' "
                    "     OR classification_json::jsonb->>'classification_status' = 'failed') "
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
        *,
        ticket_kind: str | None = None,
        classification_status: str | None = None,
    ) -> bool:
        """Update an incident's classification + embedding in place (heal path).

        Embedding is recomputed from the TICKET'S OWN TEXT — same rule as
        save_incident (the embedding signal must stay the real ticket text).
        ticket_kind/classification_status keep the v3 columns in sync with
        the new classification (None = column untouched)."""
        if not self._ready or self._pool is None:
            return False
        embed_text = self._build_embedding_text(title, description, extracted_text)
        embedding = self._embed(embed_text)
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                set_clause = "classification_json = %s, embedding = %s"
                params: list = [classification.model_dump_json(),
                                embedding.tolist() if embedding is not None else None]
                if ticket_kind is not None:
                    set_clause += ", ticket_kind = %s"
                    params.append(ticket_kind)
                if classification_status is not None:
                    set_clause += ", classification_status = %s"
                    params.append(classification_status)
                params.append(incident_id)
                cur.execute(
                    f"UPDATE incidents SET {set_clause} WHERE id = %s",
                    params,
                )
            conn.commit()
            return cur.rowcount > 0
        finally:
            self._putconn(conn)

    # ── Admin / Reset ──────────────────────────────────────────────

    def delete_all(self) -> int:
        """Delete all incidents. Returns count deleted.

        Also clears the persistent-cluster tables (cluster_members,
        clusters, assignment_log) so a reset leaves a fully clean state —
        cluster membership is derived from incidents; stale members would
        corrupt the unassigned pool."""
        if not self._ready or self._pool is None:
            return 0
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM cluster_members")
                cur.execute("DELETE FROM clusters")
                cur.execute("DELETE FROM assignment_log")
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
        # Resolve `settings` and `SimilarMatch` through the store module at
        # call time: tests monkeypatch `store_mod.settings` on the store
        # module (see tests/shared/test_incident_store.py), and the facade
        # keeps the canonical `SimilarMatch` in store.py. A module-level
        # import here would bind the unpatched originals.
        import ai_classification.shared.store as store_mod
        settings = store_mod.settings
        SimilarMatch = store_mod.SimilarMatch
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
                    "status, source_status, created_at, documents, assign_group, assignee, priority, notes, "
                    "discussion_history, escalation_info, completion_code, "
                    "ticket_kind, classification_status, "
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
                    "status, source_status, created_at, documents, assign_group, assignee, priority, notes, "
                    "discussion_history, escalation_info, completion_code, "
                    "ticket_kind, classification_status, "
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

    def list_incidents(self, status: str | None = None,
                       classification_status: str | None = None) -> list[dict]:
        """List incidents, optionally filtered by incident status
        ('active'/'resolved') and/or classification_status ('ok'/'failed').

        classification_status filtering happens in SQL (not in memory) so a
        reclassify sweep over only-failed rows never pulls every incident.
        """
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cols = ("id, title, description, extracted_text, classification_json, "
                        "status, source_status, created_at, documents, assign_group, assignee, priority, "
                        "notes, discussion_history, escalation_info, completion_code, "
                        "ticket_kind, classification_status")
                where = []
                args: list = []
                if status:
                    where.append("status = %s")
                    args.append(status)
                if classification_status:
                    where.append("classification_status = %s")
                    args.append(classification_status)
                if where:
                    cur.execute(
                        f"SELECT {cols} FROM incidents WHERE {' AND '.join(where)} "
                        "ORDER BY created_at DESC",
                        args,
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
                    "status, source_status, created_at, documents, assign_group, assignee, "
                    "priority, notes, discussion_history, escalation_info, completion_code, "
                    "ticket_kind, classification_status, "
                    "embedding::text "
                    "FROM incidents "
                    "ORDER BY created_at DESC"
                )
                rows = cur.fetchall()
            result = []
            for r in rows:
                emb_str = r[18]
                emb = None
                if emb_str:
                    emb = np.array([float(x) for x in emb_str.strip("[]").split(",")], dtype=np.float32)
                result.append((self._row_to_incident(r, embedding_str=emb_str), emb))
            return result
        finally:
            self._putconn(conn)

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

    @staticmethod
    def _row_to_incident(row, *, embedding_str: str | None = None,
                         extended: bool = False) -> dict:
        """Convert a DB row to an incident dict.

        Handles three query shapes:
          - Base (18 cols): list_incidents
          - Extended (23 cols, extended=True): get_incident
          - Embedding (19 cols, embedding_str set): list_incidents_with_embeddings

        The 18 base columns are always at indices 0–17.
        Extended fields are at 18–22.
        Embedding text is passed separately (appended to SELECT as last col).
        """
        d = {
            "id": row[0],
            "title": row[1],
            "description": row[2],
            "extracted_text": row[3],
            "classification": row[4],
            "status": row[5],
            "source_status": row[6],
            "created_at": row[7].isoformat() if row[7] else "",
            "documents": row[8] if isinstance(row[8], list) else (json.loads(row[8]) if row[8] else []),
            "assign_group": row[9] or "",
            "assignee": row[10] or "",
            "priority": row[11] or "medium",
            "notes": row[12],
            "discussion_history": row[13] if isinstance(row[13], list) else (json.loads(row[13]) if row[13] else []),
            "escalation_info": row[14],
            "completion_code": row[15],
            "ticket_kind": row[16],
            "classification_status": row[17],
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
                "content_hash": row[18],
                "occurrence_count": row[19] or 1,
                "first_seen": row[20].isoformat() if row[20] else "",
                "last_seen": row[21].isoformat() if row[21] else "",
                "source_ticket_ids": row[22] if isinstance(row[22], list) else (json.loads(row[22]) if row[22] else []),
            })
        if embedding_str is not None:
            d["_embedding_raw"] = embedding_str
        return d
