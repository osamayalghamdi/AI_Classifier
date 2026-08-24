"""Classification-log + taxonomy-gap store surface (LogsMixin).

Extracted from store.py (refactor C-1): classifier-v3 audit trail
(classification_log), taxonomy gaps, and failed-classification recovery
candidates.

Pipeline position: 40_store — Postgres/pgvector persistence."""

import json
import logging

_log = logging.getLogger(__name__)


class LogsMixin:
    """Classification log + taxonomy gaps (classifier v3 audit trail)."""

    # ── Classification log + taxonomy gaps (classifier v3) ──────────────

    def log_classification(
        self, incident_ref: str, stage: str, prompt_version: str,
        model: str, raw_verdict: str, extra: dict | None = None,
    ) -> None:
        """Append one LLM decision to classification_log (v3 audit trail).

        Every triage / cascade / verification LLM call logs its raw verdict
        here. Append-only — the pipeline never reads it back."""
        if not self._ready or self._pool is None:
            return
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO classification_log "
                    "(incident_ref, stage, prompt_version, model, raw_verdict, extra) "
                    "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
                    (incident_ref, stage, prompt_version, model, raw_verdict,
                     json.dumps(extra or {})),
                )
            conn.commit()
        finally:
            self._putconn(conn)

    def record_taxonomy_gap(
        self, service: str, suggested_offering: str, incident_ref: str,
    ) -> None:
        """Record (or aggregate) a taxonomy gap: the classifier found no
        offering for `service` and suggests `suggested_offering`.

        Upserts on UNIQUE(service, suggested_offering): count+1, appends
        the incident ref to incident_refs, bumps last_seen."""
        if not self._ready or self._pool is None:
            return
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO taxonomy_gaps "
                    "(id, service, suggested_offering, incident_refs, count) "
                    "VALUES (%s, %s, %s, %s::jsonb, 1) "
                    "ON CONFLICT (service, suggested_offering) DO UPDATE SET "
                    "  count = taxonomy_gaps.count + 1, "
                    "  incident_refs = taxonomy_gaps.incident_refs || excluded.incident_refs, "
                    "  last_seen = NOW()",
                    (self.generate_id(), service, suggested_offering,
                     json.dumps([incident_ref])),
                )
            conn.commit()
        finally:
            self._putconn(conn)

    def list_taxonomy_gaps(self) -> list[dict]:
        """All taxonomy gaps, most-reported first."""
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, service, suggested_offering, incident_refs, count, "
                    "first_seen, last_seen FROM taxonomy_gaps ORDER BY count DESC"
                )
                out = []
                for r in cur.fetchall():
                    out.append({
                        "id": r[0],
                        "service": r[1],
                        "suggested_offering": r[2],
                        "incident_refs": r[3] if isinstance(r[3], list) else (json.loads(r[3]) if r[3] else []),
                        "count": r[4],
                        "first_seen": r[5].isoformat() if r[5] else "",
                        "last_seen": r[6].isoformat() if r[6] else "",
                    })
                return out
        finally:
            self._putconn(conn)

    def list_classification_log(
        self, incident_ref: str | None = None, limit: int = 500,
    ) -> list[dict]:
        """Classification-log entries, newest first, optionally filtered by
        incident ref (capped at `limit`)."""
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                if incident_ref is not None:
                    cur.execute(
                        "SELECT id, incident_ref, stage, prompt_version, model, "
                        "raw_verdict, extra, created_at FROM classification_log "
                        "WHERE incident_ref = %s ORDER BY created_at DESC, id DESC LIMIT %s",
                        (incident_ref, limit),
                    )
                else:
                    cur.execute(
                        "SELECT id, incident_ref, stage, prompt_version, model, "
                        "raw_verdict, extra, created_at FROM classification_log "
                        "ORDER BY created_at DESC, id DESC LIMIT %s",
                        (limit,),
                    )
                out = []
                for r in cur.fetchall():
                    out.append({
                        "id": r[0],
                        "incident_ref": r[1],
                        "stage": r[2],
                        "prompt_version": r[3],
                        "model": r[4],
                        "raw_verdict": r[5],
                        "extra": r[6] if isinstance(r[6], (dict, list)) else (json.loads(r[6]) if r[6] else {}),
                        "created_at": r[7].isoformat() if r[7] else "",
                    })
                return out
        finally:
            self._putconn(conn)

    def _failed_classifications(self, limit: int = 10) -> list[dict]:
        """Active incidents whose classification FAILED — v3 status column
        ('failed', either the column or the classification_json field) OR
        the legacy fallback marker in reasoning (pre-v3 rows). These are
        the heal / recovery candidates."""
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, title, description, extracted_text FROM incidents "
                    "WHERE status = 'active' "
                    "AND (classification_status = 'failed' "
                    "     OR classification_json::jsonb->>'classification_status' = 'failed' "
                    "     OR classification_json::jsonb->>'reasoning' LIKE %s) "
                    "LIMIT %s",
                    ("Classification failed after%", limit),
                )
                cols = ("id", "title", "description", "extracted_text")
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            self._putconn(conn)

    # ── Taxonomy overrides (admin console) ──────────────────────────────
    # Admin-added services/offerings, merged on top of the frozen code
    # taxonomy at runtime (domain/taxonomy.py effective_* view).

    def list_taxonomy_overrides(self) -> list[dict]:
        """All override rows: {system, service, offering}."""
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT system, service, offering FROM taxonomy_overrides "
                    "ORDER BY system, service, offering")
                cols = ("system", "service", "offering")
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            self._putconn(conn)

    def upsert_taxonomy_override(self, system: str, service: str,
                                 offering: str = "") -> None:
        """Add/replace one override row. offering='' means 'add the service'
        (possibly with offerings added separately as sibling rows)."""
        if not self._ready or self._pool is None:
            return
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO taxonomy_overrides (system, service, offering) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (system, service, offering) DO NOTHING",
                    (system, service, offering))
            conn.commit()
        finally:
            self._putconn(conn)

    def delete_taxonomy_override(self, system: str, service: str,
                                 offering: str = "") -> bool:
        if not self._ready or self._pool is None:
            return False
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM taxonomy_overrides "
                    "WHERE system = %s AND service = %s AND offering = %s",
                    (system, service, offering))
            conn.commit()
            return cur.rowcount > 0
        finally:
            self._putconn(conn)

    def reload_taxonomy_overrides(self) -> None:
        """Load DB overrides into the runtime registry (startup + admin edit).
        Lazy import avoids a domain->store cycle at module load."""
        from ai_classification.domain.taxonomy import set_runtime_overrides
        merged: dict[str, dict[str, list[str]]] = {}
        for row in self.list_taxonomy_overrides():
            bucket = merged.setdefault(row["system"], {})
            bucket.setdefault(row["service"], [])
            if row["offering"]:
                bucket[row["service"]].append(row["offering"])
        set_runtime_overrides(merged)
