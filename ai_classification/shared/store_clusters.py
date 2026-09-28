"""Persistent cluster store surface (ClustersMixin).

Extracted from store.py (refactor C-1): persistent clusters + members +
assignment log (v2 LLM-first clustering tables).

Pipeline position: 40_store — Postgres/pgvector persistence."""

import json
import logging
from datetime import datetime, timedelta, timezone

import numpy as np

_log = logging.getLogger(__name__)


class ClustersMixin:
    """Persistent clusters (v2 LLM-first): rows in `clusters` /
    `cluster_members` / `assignment_log`."""

    # ── Persistent clusters (v2 LLM-first) ─────────────────────────────
    # Clusters + members are DB rows (see setup() DDL). Invariants:
    #   * an incident belongs to AT MOST ONE cluster (any status) — enforced
    #     in add_cluster_member (remove-from-others, atomic)
    #   * the unassigned pool is DERIVED: incidents with no cluster_members row
    #   * proposal members are cluster_members rows of a status='proposed'
    #     cluster; approval = status flip to 'active' (no copy)

    def create_cluster(self, cluster_id: str, name_ar: str, description: str,
                       name_en: str = "", status: str = "proposed",
                       affected_system: str = "") -> dict | None:
        """Create a cluster row. ``affected_system`` is the ONE system this
        cluster serves (system-scoped clustering): every member must belong
        to it (enforced in add_cluster_member). '' = legacy / unscoped — the
        first member claims it (see add_cluster_member); retrieval treats it
        as the 'Unknown' bucket until then.

        CONCURRENT-SAFE: the id is content-derived, so two workers (the sweep
        and Flow A minting the same group at the same moment) can race on the
        same id. `ON CONFLICT DO NOTHING` turns that into "somebody already
        created it" — the INSERT returns no row and this returns None — instead
        of a UniqueViolation that would abort a whole sweep.
        """
        if not self._ready or self._pool is None:
            return None
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO clusters (id, name_ar, name_en, description, status, affected_system) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING "
                    "RETURNING id, name_ar, name_en, description, status, affected_system, "
                    "created_at, updated_at",
                    (cluster_id, name_ar, name_en, description, status,
                     (affected_system or "").strip()),
                )
                row = cur.fetchone()
            conn.commit()
            return self._row_to_cluster(row) if row else None
        finally:
            self._putconn(conn)

    def get_cluster(self, cluster_id: str) -> dict | None:
        if not self._ready or self._pool is None:
            return None
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name_ar, name_en, description, status, affected_system, "
                    "created_at, updated_at "
                    "FROM clusters WHERE id = %s", (cluster_id,))
                row = cur.fetchone()
            return self._row_to_cluster(row) if row else None
        finally:
            self._putconn(conn)

    def list_clusters(self, status: str | None = None,
                      affected_system: str | None = None) -> list[dict]:
        """List clusters, optionally filtered by status and/or the cluster's
        system (system-scoped retrieval: Flow A only pulls same-system
        clusters as candidates)."""
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                sql = ("SELECT id, name_ar, name_en, description, status, affected_system, "
                       "created_at, updated_at FROM clusters")
                conds, args = [], []
                if status is not None:
                    conds.append("status = %s"); args.append(status)
                if affected_system is not None:
                    conds.append("affected_system = %s"); args.append(affected_system)
                if conds:
                    sql += " WHERE " + " AND ".join(conds)
                sql += " ORDER BY created_at ASC"
                cur.execute(sql, tuple(args))
                return [self._row_to_cluster(r) for r in cur.fetchall()]
        finally:
            self._putconn(conn)

    def set_cluster_status(self, cluster_id: str, status: str,
                           expected_status: str | None = None) -> bool:
        """Set a cluster's status. With expected_status the update is a
        compare-and-set (only applied if the row still has that status), so
        two concurrent decisions cannot both win."""
        if not self._ready or self._pool is None:
            return False
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                if expected_status is None:
                    cur.execute(
                        "UPDATE clusters SET status = %s, updated_at = NOW() "
                        "WHERE id = %s", (status, cluster_id))
                else:
                    cur.execute(
                        "UPDATE clusters SET status = %s, updated_at = NOW() "
                        "WHERE id = %s AND status = %s",
                        (status, cluster_id, expected_status))
            conn.commit()
            return cur.rowcount > 0
        finally:
            self._putconn(conn)

    def update_cluster_fields(self, cluster_id: str, *,
                              name_ar: str | None = None,
                              name_en: str | None = None,
                              description: str | None = None) -> bool:
        """Update any subset of {name_ar, name_en, description}; bumps updated_at."""
        if not self._ready or self._pool is None:
            return False
        sets, args = [], []
        if name_ar is not None:
            sets.append("name_ar = %s"); args.append(name_ar)
        if name_en is not None:
            sets.append("name_en = %s"); args.append(name_en)
        if description is not None:
            sets.append("description = %s"); args.append(description)
        if not sets:
            return False
        sets.append("updated_at = NOW()")
        args.append(cluster_id)
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE clusters SET {', '.join(sets)} WHERE id = %s", args)
            conn.commit()
            return cur.rowcount > 0
        finally:
            self._putconn(conn)

    def cluster_member_ids(self, cluster_id: str) -> list[str]:
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT incident_id FROM cluster_members WHERE cluster_id = %s "
                    "ORDER BY assigned_at ASC", (cluster_id,))
                return [r[0] for r in cur.fetchall()]
        finally:
            self._putconn(conn)

    def list_cluster_members(self, cluster_id: str) -> list[dict]:
        """Members enriched with incident text (title/description/classification/
        status) — no embeddings. Order: assigned_at ASC (stable, oldest first)."""
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cm.cluster_id, cm.incident_id, cm.assigned_by, cm.confidence, "
                    "       cm.assigned_at, i.title, i.description, i.classification_json, "
                    "       i.status, i.created_at "
                    "FROM cluster_members cm JOIN incidents i ON i.id = cm.incident_id "
                    "WHERE cm.cluster_id = %s "
                    "ORDER BY cm.assigned_at ASC", (cluster_id,))
                return [self._row_to_cluster_member(r) for r in cur.fetchall()]
        finally:
            self._putconn(conn)

    def cluster_member_embeddings(self, cluster_id: str) -> list[dict]:
        """(incident_id, title, description, embedding) per member with an
        embedding — used to build retrieval centroids. pgvector columns come
        back as text ('[1.0, ...]'); parsed like list_incidents_with_embeddings."""
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cm.incident_id, i.title, i.description, i.embedding::text "
                    "FROM cluster_members cm JOIN incidents i ON i.id = cm.incident_id "
                    "WHERE cm.cluster_id = %s AND i.embedding IS NOT NULL "
                    "ORDER BY cm.assigned_at ASC", (cluster_id,))
                members = []
                for rid, title, description, emb_str in cur.fetchall():
                    if not emb_str:
                        continue
                    emb = np.array([float(x) for x in emb_str.strip("[]").split(",")],
                                   dtype=np.float32)
                    members.append({"incident_id": rid, "title": title,
                                    "description": description, "embedding": emb})
                return members
        finally:
            self._putconn(conn)

    def add_cluster_member(self, cluster_id: str, incident_id: str,
                           assigned_by: str = "llm", confidence: str | None = None) -> bool:
        """Insert a member, enforcing the one-cluster-per-incident invariant:
        the incident is removed from ANY other cluster first (same transaction).

        SYSTEM-SCOPING INVARIANT (2026-09): a cluster serves ONE system — an
        incident from a different system (Hajj vs Umrah = different teams) is
        REFUSED here, whatever the caller asked for. The cluster's system is
        read from the row; the incident's from its stored classification. An
        UNSCOPED cluster (affected_system = '') is claimed by its first
        member (row stamped with that system), so legacy rows and manual
        groups keep working while never mixing systems. 'Unknown' is its own
        bucket (unclassified incidents cluster together). Returns True on
        insert, False if refused or already there.
        """
        if not self._ready or self._pool is None:
            return False
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                # Serialise concurrent adds of the SAME incident (Flow A
                # arrival threads, the sweep, admin edits): without it two
                # transactions each delete-then-insert and the ticket ends up
                # in two clusters. Released at commit/rollback.
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (incident_id,))
                # ── System check — refuse cross-system membership ──────────
                cur.execute("SELECT affected_system FROM clusters WHERE id = %s",
                            (cluster_id,))
                row = cur.fetchone()
                if row is None:
                    conn.rollback()  # close the read transaction before returning
                    return False
                cluster_sys = (row[0] or "").strip()
                cur.execute("SELECT classification_json FROM incidents WHERE id = %s",
                            (incident_id,))
                row = cur.fetchone()
                inc_sys = "Unknown"
                if row is not None:
                    try:
                        cls = json.loads(row[0] or "{}")
                    except Exception:
                        cls = {}
                    inc_sys = (cls.get("affected_system") or "Unknown").strip() or "Unknown"
                if cluster_sys == "":
                    # Unscoped cluster (legacy row / admin group without a
                    # declared system): the FIRST member claims it — the row
                    # is stamped with the member's system so every later
                    # member must match (Hajj and Umrah can never mix).
                    cur.execute(
                        "UPDATE clusters SET affected_system = %s, updated_at = NOW() "
                        "WHERE id = %s", (inc_sys, cluster_id))
                elif cluster_sys != inc_sys:
                    _log.warning(
                        "add_cluster_member REFUSED: %s (%s) -> cluster %s (%s) — "
                        "cross-system membership blocked (one cluster = one system)",
                        incident_id[:10], inc_sys, cluster_id[:10], cluster_sys)
                    conn.rollback()  # nothing changed — close the transaction
                    return False
                # ── One-cluster-per-incident + insert (same transaction) ───
                cur.execute(
                    "DELETE FROM cluster_members WHERE incident_id = %s AND cluster_id <> %s",
                    (incident_id, cluster_id))
                # An explicit HUMAN assignment also re-stamps an existing row:
                # the audit treats assigned_by='human' as a pin it may not
                # remove, so "add this member by hand" must win over the
                # provenance the row already carries (e.g. 'seed' or 'llm').
                if assigned_by in ("human", "admin"):
                    cur.execute(
                        "UPDATE cluster_members SET assigned_by = %s, "
                        "confidence = COALESCE(%s, confidence), assigned_at = NOW() "
                        "WHERE cluster_id = %s AND incident_id = %s",
                        (assigned_by, confidence, cluster_id, incident_id))
                cur.execute(
                    "INSERT INTO cluster_members (cluster_id, incident_id, assigned_by, confidence) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (cluster_id, incident_id) DO NOTHING",
                    (cluster_id, incident_id, assigned_by, confidence))
            conn.commit()
            return cur.rowcount > 0
        finally:
            self._putconn(conn)

    def remove_cluster_member(self, cluster_id: str, incident_id: str) -> bool:
        if not self._ready or self._pool is None:
            return False
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM cluster_members WHERE cluster_id = %s AND incident_id = %s",
                    (cluster_id, incident_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            self._putconn(conn)

    def remove_cluster_members(self, cluster_id: str) -> int:
        if not self._ready or self._pool is None:
            return 0
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM cluster_members WHERE cluster_id = %s",
                            (cluster_id,))
                n = cur.rowcount
            conn.commit()
            return n
        finally:
            self._putconn(conn)

    def delete_cluster(self, cluster_id: str) -> bool:
        """Delete a cluster row AND its members (admin group delete).

        Membership rows are removed first (cluster_members has no FK to
        clusters, so order matters only for the unassigned-pool invariant:
        members of a deleted cluster go back to the unassigned pool)."""
        if not self._ready or self._pool is None:
            return False
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM cluster_members WHERE cluster_id = %s",
                            (cluster_id,))
                cur.execute("DELETE FROM clusters WHERE id = %s", (cluster_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            self._putconn(conn)

    def incident_cluster(self, incident_id: str) -> dict | None:
        """The cluster (any status) this incident currently belongs to, or None."""
        if not self._ready or self._pool is None:
            return None
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT c.id, c.name_ar, c.name_en, c.description, c.status, c.affected_system "
                    "FROM cluster_members cm JOIN clusters c ON c.id = cm.cluster_id "
                    "WHERE cm.incident_id = %s", (incident_id,))
                row = cur.fetchone()
            if row is None:
                return None
            return {"id": row[0], "name_ar": row[1], "name_en": row[2],
                    "description": row[3], "status": row[4],
                    "affected_system": row[5] or ""}
        finally:
            self._putconn(conn)

    def unassigned_incident_ids(self) -> list[str]:
        """Derived unassigned pool: every incident with no cluster_members row."""
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM incidents "
                    "WHERE id NOT IN (SELECT incident_id FROM cluster_members) "
                    "ORDER BY created_at ASC")
                return [r[0] for r in cur.fetchall()]
        finally:
            self._putconn(conn)

    def log_assignment(self, incident_id: str, candidates: list[str],
                       verdict: dict, prompt_version: str, model_version: str) -> int:
        """One row per LLM decision — the audit trail (assignment_log)."""
        if not self._ready or self._pool is None:
            return 0
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO assignment_log "
                    "(incident_id, candidates, verdict, prompt_version, model_version) "
                    "VALUES (%s, %s::jsonb, %s::jsonb, %s, %s) RETURNING id",
                    (incident_id, json.dumps(candidates), json.dumps(verdict),
                     prompt_version, model_version))
                row = cur.fetchone()
            conn.commit()
            return row[0] if row else 0
        finally:
            self._putconn(conn)

    def audit_vetoes(self, incident_id: str, cluster_ids: list[str],
                     ttl_days: int | None = None) -> set[str]:
        """Clusters that have REJECTED this incident in an audit and not taken
        it back since — the anti-oscillation ledger.

        WHY: Flow C (nightly audit) evicts impure members, but Flow A/B re-run
        every sweep (15 min) and re-assign them to the SAME cluster — the LLMs
        disagree and the two loops fight forever (prod: the same 4 tickets
        removed and re-added nightly). The audit sees ALL members and is the
        authority on purity, so its newest verdict per (incident, cluster)
        wins:

          * newest verdict is ``audit_remove`` -> veto (Flow A must not
            re-assign, Flow B must not re-mint that pair);
          * newest verdict is ``audit_keep``   -> clear (the audit itself
            reversed the rejection, so the ticket may come back).

        ``ttl_days`` bounds the veto (persistent.AUDIT_VETO_TTL_DAYS): an
        eviction older than that stops blocking, so a wrong audit verdict can
        never lock a ticket out of a cluster forever — it just costs one retry
        per month instead of one every 15 minutes. None = no expiry.

        Returns the subset of ``cluster_ids`` currently vetoing the incident.
        Only audit verdicts count: `assign` rows are exactly the decisions
        this ledger exists to override.
        """
        if not self._ready or self._pool is None or not cluster_ids:
            return set()
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                sql = ("SELECT cluster_id, action, created_at FROM ("
                       "  SELECT DISTINCT ON (verdict->>'cluster_id')"
                       "         verdict->>'cluster_id' AS cluster_id,"
                       "         verdict->>'action' AS action, id, created_at"
                       "  FROM assignment_log"
                       "  WHERE incident_id = %s AND verdict->>'action' IN ('audit_remove', 'audit_keep')"
                       "  ORDER BY verdict->>'cluster_id', id DESC"
                       ") t WHERE cluster_id = ANY(%s)")
                args: list = [incident_id, list(cluster_ids)]
                if ttl_days is not None:
                    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
                    sql += " AND created_at >= %s"
                    args.append(cutoff)
                cur.execute(sql, tuple(args))
                return {r[0] for r in cur.fetchall() if r[1] == "audit_remove"}
        finally:
            self._putconn(conn)

    def cross_system_members(self, cluster_id: str, cluster_system: str) -> list[str]:
        """Members whose CURRENT classified system differs from the cluster's.

        The store refuses cross-system INSERTs (add_cluster_member), but rows
        that predate that invariant — or incidents RE-CLASSIFIED into another
        system after joining — stay behind, so Hajj clusters still show Umrah
        tickets on the dashboard. This is the reconciliation query: the sweep
        evicts whatever it returns (see persistent._evict_cross_system_members).

        Missing/blank classification counts as 'Unknown', exactly like
        add_cluster_member. An UNSCOPED cluster ('') is not reconciled: its
        first member claims the system, so it has no declared scope to violate.
        """
        if not self._ready or self._pool is None:
            return []
        cluster_sys = (cluster_system or "").strip()
        if not cluster_sys:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cm.incident_id, i.classification_json "
                    "FROM cluster_members cm JOIN incidents i ON i.id = cm.incident_id "
                    "WHERE cm.cluster_id = %s", (cluster_id,))
                out = []
                for incident_id, raw in cur.fetchall():
                    try:
                        cls = json.loads(raw or "{}")
                    except Exception:
                        cls = {}
                    inc_sys = (cls.get("affected_system") or "Unknown").strip() or "Unknown"
                    if inc_sys != cluster_sys:
                        out.append(incident_id)
                return out
        finally:
            self._putconn(conn)

    def list_assignment_log(self, incident_id: str | None = None,
                            limit: int = 200) -> list[dict]:
        if not self._ready or self._pool is None:
            return []
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                if incident_id is not None:
                    cur.execute(
                        "SELECT id, incident_id, candidates, verdict, prompt_version, "
                        "model_version, created_at FROM assignment_log "
                        "WHERE incident_id = %s ORDER BY id DESC LIMIT %s",
                        (incident_id, limit))
                else:
                    cur.execute(
                        "SELECT id, incident_id, candidates, verdict, prompt_version, "
                        "model_version, created_at FROM assignment_log "
                        "ORDER BY id DESC LIMIT %s", (limit,))
                return [{"id": r[0], "incident_id": r[1], "candidates": r[2],
                         "verdict": r[3], "prompt_version": r[4],
                         "model_version": r[5], "created_at": r[6]}
                        for r in cur.fetchall()]
        finally:
            self._putconn(conn)

    @staticmethod
    def _row_to_cluster(row) -> dict:
        return {"id": row[0], "name_ar": row[1], "name_en": row[2],
                "description": row[3], "status": row[4],
                "affected_system": row[5] or "", "created_at": row[6],
                "updated_at": row[7]}

    @staticmethod
    def _row_to_cluster_member(row) -> dict:
        return {"cluster_id": row[0], "incident_id": row[1], "assigned_by": row[2],
                "confidence": row[3], "assigned_at": row[4], "title": row[5],
                "description": row[6], "classification_json": row[7],
                "status": row[8], "created_at": row[9]}
