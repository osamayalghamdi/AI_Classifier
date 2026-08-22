"""Quarantined sub-offering store surface (mixin).

The v2 persistent-clustering path (services/cluster/persistent.py) superseded
the sub-offering engine; the tables and methods below are kept ONLY for the
quarantined engine in legacy/suboffering_engine/ (see README). Methods are
verbatim copies of the original IncidentStore methods; they rely on the
IncidentStore internals (_ready, _pool, _getconn, _putconn, generate_id).
"""

import json

from ai_classification.shared.store import IncidentStore


class SubOfferingStoreMixin:
    """Sub-offering + proposal + pool store surface (dormant engine)."""

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
        """Attach member ticket texts (id, title, description) so a
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
                svc = json.loads(cj).get("service", "")
            except Exception:
                svc = ""
            by_id[rid] = {"id": rid, "title": title, "description": desc,
                          "service": svc}
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
                        target_sub_offering_id: str = "", note: str = "") -> dict | None:
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


class LegacySubOfferingStore(SubOfferingStoreMixin, IncidentStore):
    """IncidentStore + the quarantined sub-offering surface, composed for the
    legacy engine's tests/scripts (the live app never constructs this)."""


# Module-level singleton for the legacy engine — mirrors the live store's
# lifecycle (call .setup() before use; reuses the same env settings).
store = LegacySubOfferingStore()
