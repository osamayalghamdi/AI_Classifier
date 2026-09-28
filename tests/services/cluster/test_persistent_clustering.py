"""v2 LLM-first persistent clustering — Flows A/B/C/D + invariants + API shape.

Real test DB (ai_incidents_test, forced by conftest) + real bge-m3 embeddings;
the LLM is mocked at the module seam (persistent.call_llm) so every flow is
deterministic. Coverage:

  Flow A — assign (high/medium), none_fit, assign+low, hallucinated cluster id,
           assignment_log row with prompt_version
  Flow B — grouping -> proposals, 2-member birth, ID-mismatch discard
  Flow C — member removal -> pool, refined description, ID mismatch + 85% floor
  Invariants — one cluster per incident (enforced in add_cluster_member)
  Review gate — approve -> active, reject -> members back to pool
  API shape — /clusters + /api/reports from tables (dashboard-compatible fields)
"""

import json
import os

os.environ.setdefault("PG_DATABASE", "ai_incidents_test")
os.environ["CLUSTER_ASSIGN_ON_ARRIVAL"] = "0"  # tests drive flows explicitly
os.environ["CLUSTER_AUTO_ACTIVATE"] = "0"      # gate path by default; auto-activate tested separately

import pytest

import ai_classification.services.cluster.persistent as pc
from ai_classification.shared.store import store


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _store_ready():
    store.setup()
    assert store.ready, "store must be ready against the test DB"
    yield


@pytest.fixture(scope="module", autouse=True)
def _wipe_tables():
    """Start from a clean slate: the shared test DB carries leftovers from
    other modules (a stray incident would pollute the derived unassigned
    pool). Each test module here is self-contained; wipe all rows."""
    conn = store._getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cluster_members")
            cur.execute("DELETE FROM clusters")
            cur.execute("DELETE FROM assignment_log")
            cur.execute("DELETE FROM incidents")
        conn.commit()
    finally:
        store._putconn(conn)
    yield


@pytest.fixture(autouse=True)
def _cleanup():
    """Teardown: remove everything this module created. Proposal ids are
    cl_<sha256> (not cl_tst_*) so the whole cluster tables are wiped — the
    module-start wipe guarantees the DB began clean."""
    yield
    conn = store._getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cluster_members")
            cur.execute("DELETE FROM clusters")
            cur.execute("DELETE FROM assignment_log")
            cur.execute("DELETE FROM incidents WHERE id LIKE 'tst-%%'")
        conn.commit()
    finally:
        store._putconn(conn)


def _save_incident(iid: str, title: str, description: str = "",
                   service: str = "Nusuk Masar Haj.Service Unavailability",
                   status: str = "active", system: str | None = None) -> str:
    """Insert an incident directly (no LLM classification) with a fake
    classification_json carrying the service + a canonical statement. The
    incident's system defaults to 'Nusuk Masar Haj' (the pre-v4 tests were
    all Hajj); pass ``system`` for cross-system scenarios."""
    cls = {"affected_system": system if system is not None else "Nusuk Masar Haj",
           "service": service,
           "severity": "Major", "confidence": "high",
           "canonical_statement": f"Incident reported: {title}",
           "incident_type": "", "urgency": "", "category": ""}
    store.save_incident(
        iid, title, description, _FakeResult(cls), "",
        content_hash=f"tst-hash-{iid}", source_ticket_ids=[iid],
        status=status,
    )
    return iid


class _FakeResult:
    """Minimal stand-in for ClassificationResult: save_incident reads
    canonical_statement + severity + affected_system + service and calls
    model_dump_json() for the stored classification_json."""

    def __init__(self, cls: dict):
        self.canonical_statement = cls.get("canonical_statement", "")
        self.severity = cls.get("severity", "Minor")
        self.affected_system = cls.get("affected_system", "")
        self.service = cls.get("service", "")
        self._cls = cls

    def model_dump_json(self) -> str:
        return json.dumps(self._cls, ensure_ascii=False)


def _seed_cluster(cid: str, name_ar: str, description: str,
                  member_ids: list[str], status: str = "active",
                  affected_system: str | None = None) -> str:
    """Create a cluster row + members. ``affected_system`` defaults to the
    first member's system (v4: a cluster serves ONE system — members must
    belong to it, enforced by add_cluster_member). Empty-member clusters are
    seeded UNSCOPED ('') so the first added member claims them."""
    if affected_system is None:
        first = store.get_incident(member_ids[0]) if member_ids else None
        affected_system = pc._system_of_inc(first) if first else ""
    store.create_cluster(cid, name_ar, description, status=status,
                         affected_system=affected_system)
    for m in member_ids:
        store.add_cluster_member(cid, m, assigned_by="seed", confidence="seed")
    return cid


def _fake_llm(monkeypatch, responder):
    """Route persistent.call_llm through `responder(messages, **kwargs)`."""
    monkeypatch.setattr(pc, "call_llm", responder)


def _json_llm(monkeypatch, payload: dict):
    """Respond with a fixed JSON payload (rendered from the request is not
    needed — deterministic per test)."""
    _fake_llm(monkeypatch, lambda messages, **kw: json.dumps(payload))


# ── Flow A — assign on arrival ───────────────────────────────────────────

class TestFlowA:
    def test_high_confidence_assigns_to_existing_cluster(self, monkeypatch):
        _save_incident("tst-a1", "Rawdah permit fails on date selection", "error on done button",
                       service="Nusuk Masar Haj.Issue Permits")
        _save_incident("tst-a2", "Rawdah permit booking error", "cannot select date",
                       service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_rawdah", "فشل إصدار تصريح الروضة",
                      "Rawdah permit issuance fails", ["tst-a1", "tst-a2"])
        _save_incident("tst-a3", "Rawdah permit again failing", "date picker broken",
                       service="Nusuk Masar Haj.Issue Permits")

        _json_llm(monkeypatch, {"action": "assign", "cluster_id": "cl_tst_rawdah",
                                "confidence": "high", "reason": "same Rawdah permit failure"})
        r = pc.assign_incident("tst-a3")

        assert r["action"] == "assign" and r["cluster_id"] == "cl_tst_rawdah"
        assert "tst-a3" in store.cluster_member_ids("cl_tst_rawdah")
        assert store.incident_cluster("tst-a3")["id"] == "cl_tst_rawdah"

    def test_none_fit_stays_in_pool_and_is_logged(self, monkeypatch):
        _save_incident("tst-b1", "Rawdah permit fails", service="Nusuk Masar Haj.Issue Permits")
        _save_incident("tst-b2", "Rawdah permit booking error", service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_rawdah2", "فشل إصدار تصريح الروضة", "Rawdah permit fails",
                      ["tst-b1", "tst-b2"])
        _save_incident("tst-b3", "unrelated CRM login problem", "credentials rejected",
                       service="Nusuk Masar Haj.Service Unavailability")

        _json_llm(monkeypatch, {"action": "none_fit", "cluster_id": None,
                                "confidence": "low", "reason": "different problem"})
        r = pc.assign_incident("tst-b3")

        assert r["action"] == "none_fit"
        assert "tst-b3" not in store.cluster_member_ids("cl_tst_rawdah2")
        assert "tst-b3" in store.unassigned_incident_ids()
        log = store.list_assignment_log(incident_id="tst-b3")
        assert len(log) == 1
        assert log[0]["prompt_version"] == pc.PROMPT_VERSION
        assert log[0]["verdict"]["action"] == "none_fit"
        assert log[0]["candidates"] == ["cl_tst_rawdah2"]

    def test_assign_with_low_confidence_goes_to_pool(self, monkeypatch):
        _save_incident("tst-c1", "Rawdah permit fails", service="Nusuk Masar Haj.Issue Permits")
        _save_incident("tst-c2", "Rawdah permit booking error", service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_rawdah3", "فشل إصدار تصريح الروضة", "Rawdah permit fails",
                      ["tst-c1", "tst-c2"])
        _save_incident("tst-c3", "permit system slow", "takes minutes",
                       service="Nusuk Masar Haj.Issue Permits")

        _json_llm(monkeypatch, {"action": "assign", "cluster_id": "cl_tst_rawdah3",
                                "confidence": "low", "reason": "maybe related"})
        r = pc.assign_incident("tst-c3")

        assert r["action"] == "none_fit"  # low confidence never auto-assigns
        assert "tst-c3" not in store.cluster_member_ids("cl_tst_rawdah3")

    def test_hallucinated_cluster_id_never_assigns(self, monkeypatch):
        _save_incident("tst-d1", "Rawdah permit fails", service="Nusuk Masar Haj.Issue Permits")
        _save_incident("tst-d2", "Rawdah permit booking error", service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_rawdah4", "فشل إصدار تصريح الروضة", "Rawdah permit fails",
                      ["tst-d1", "tst-d2"])
        _save_incident("tst-d3", "permit system slow", service="Nusuk Masar Haj.Issue Permits")

        _json_llm(monkeypatch, {"action": "assign", "cluster_id": "cl_NOPE",
                                "confidence": "high", "reason": "hallucinated"})
        r = pc.assign_incident("tst-d3")

        assert r["action"] == "none_fit"
        assert store.incident_cluster("tst-d3") is None
        log = store.list_assignment_log(incident_id="tst-d3")
        assert log[0]["verdict"].get("_safeguard") == "cluster_id not among candidates"

    def test_no_active_clusters_skips_llm_and_logs(self, monkeypatch):
        _save_incident("tst-e1", "anything at all")
        called = []
        _fake_llm(monkeypatch, lambda messages, **kw: called.append(1) or "{}")

        r = pc.assign_incident("tst-e1")

        assert r["action"] == "none_fit"
        assert called == []  # zero clusters -> zero LLM calls
        assert store.list_assignment_log(incident_id="tst-e1")[0]["verdict"]["action"] == "none_fit"


# ── Flow B — pool sweep ──────────────────────────────────────────────────

class TestFlowB:
    def test_sweep_groups_pool_into_proposals_and_births_2_member_cluster(self, monkeypatch):
        # 5 unassigned tickets; LLM groups 2 financial + 2 housing, 1 singleton.
        for i, t in enumerate(["failed transfer 11M", "transfer not reaching account",
                               "housing check-in broken", "housing check-in error",
                               "random test ticket x/y"]):
            _save_incident(f"tst-f{i}", t, service="Nusuk Masar Haj.Bill Payment")

        def responder(messages, **kw):
            body = messages[-1]["content"]
            return json.dumps({
                "groups": [
                    {"member_ids": ["tst-f0", "tst-f1"], "name_ar": "فشل التحويلات المالية",
                     "description": "failed financial transfers — not a billing UI issue"},
                    {"member_ids": ["tst-f2", "tst-f3"], "name_ar": "تعطل تسكين السكن",
                     "description": "housing check-in fails — not a payment issue"},
                ],
                "singletons": ["tst-f4"],
            })

        _fake_llm(monkeypatch, responder)
        stats = pc.sweep_pool()

        assert stats["proposals_created"] == 2
        props = store.list_clusters(status="proposed")
        assert len(props) == 2
        by_members = {tuple(store.cluster_member_ids(c["id"])): c for c in props}
        assert ("tst-f0", "tst-f1") in by_members  # 2-member cluster born
        assert ("tst-f2", "tst-f3") in by_members
        # members attached to the proposal, singletons stay in the pool
        assert "tst-f0" not in store.unassigned_incident_ids()
        assert "tst-f4" in store.unassigned_incident_ids()

    def test_sweep_id_mismatch_discards_batch_and_logs(self, monkeypatch):
        for i, t in enumerate(["alpha problem", "beta problem", "gamma problem"]):
            _save_incident(f"tst-g{i}", t)

        def responder(messages, **kw):
            body = messages[-1]["content"]
            if "candidate_clusters" in body:
                # Flow A phase (if any active cluster leaked from an earlier
                # test): keep everything in the pool.
                return json.dumps({"action": "none_fit", "cluster_id": None,
                                   "confidence": "low", "reason": "x"})
            # Sweep phase: echo the ACTUAL batch ids, then add an invented id
            # and omit one — the mismatch must be detected and the batch
            # discarded, whatever the pool composition.
            ids = [t["id"] for t in json.loads(body)["tickets"]]
            return json.dumps({"groups": [{"member_ids": ids[:2],
                                           "name_ar": "مجموعة",
                                           "description": "d"}],
                               "singletons": ids[2:] + ["tst-INVENTED"]})

        _fake_llm(monkeypatch, responder)
        stats = pc.sweep_pool()

        assert stats["discarded_batches"] == 1
        assert stats["proposals_created"] == 0
        assert store.list_clusters(status="proposed") == []
        assert store.list_assignment_log(incident_id="__sweep_batch__")[0]["verdict"]["action"] == "discarded"

    def test_sweep_auto_activate_mints_active_clusters(self, monkeypatch):
        """CLUSTER_AUTO_ACTIVATE=1 (user override): the sweep skips the human
        gate and mints groups as ACTIVE clusters directly."""
        from types import SimpleNamespace

        for i, t in enumerate(["payment gateway down", "payment gateway failing"]):
            _save_incident(f"tst-p{i}", t, service="Nusuk Masar Haj.Bill Payment")

        def responder(messages, **kw):
            return json.dumps({
                "groups": [{"member_ids": ["tst-p0", "tst-p1"],
                            "name_ar": "تعطل بوابة الدفع",
                            "description": "payment gateway failures"}],
                "singletons": [],
            })

        monkeypatch.setattr(pc, "settings", SimpleNamespace(
            cluster_auto_activate=True, llm_model="test-model"))
        _fake_llm(monkeypatch, responder)
        stats = pc.sweep_pool()

        assert stats["proposals_created"] == 1
        assert store.list_clusters(status="proposed") == []  # gate skipped
        actives = store.list_clusters(status="active")
        assert len(actives) == 1 and actives[0]["name_ar"] == "تعطل بوابة الدفع"
        assert set(store.cluster_member_ids(actives[0]["id"])) == {"tst-p0", "tst-p1"}

    def test_sweep_description_capped_at_max_words(self, monkeypatch):
        """User rule: descriptions match the name rule — a longer LLM
        description is truncated at mint time."""
        for i, t in enumerate(["payment gateway down", "payment gateway failing"]):
            _save_incident(f"tst-r{i}", t, service="Nusuk Masar Haj.Bill Payment")

        long_desc = " ".join(["كلمة"] * 40)

        def responder(messages, **kw):
            return json.dumps({"groups": [{"member_ids": ["tst-r0", "tst-r1"],
                                           "name_ar": "تعطل بوابة الدفع",
                                           "description": long_desc}],
                               "singletons": []})

        _fake_llm(monkeypatch, responder)
        pc.sweep_pool()

        c = store.list_clusters(status="proposed")[0]
        assert len(c["description"].split()) <= pc._DESC_MAX_WORDS

    def test_sweep_demotes_single_member_clusters(self):
        """USER RULE: 1 incident = individual. A cluster below 2 members is
        retired and its member returns to the pool."""
        _save_incident("tst-s1", "housing provider issuance fails",
                       service="Nusuk Masar Haj.Service Unavailability")
        _seed_cluster("cl_tst_one_member", "فشل اصدار مزود خدمة السكن",
                      "housing provider", ["tst-s1"])

        n = pc._demote_small_clusters()

        assert n == 1
        assert store.get_cluster("cl_tst_one_member")["status"] == "retired"
        assert store.cluster_member_ids("cl_tst_one_member") == []
        assert "tst-s1" in store.unassigned_incident_ids()
        log = store.list_assignment_log(incident_id="tst-s1")
        assert log[0]["verdict"]["action"] == "demoted"

    def test_audit_demotes_when_shrinking_below_two(self, monkeypatch):
        """Audit may shrink a cluster, but never to a single incident —
        the cluster is demoted and the last member returns to the pool."""
        _save_incident("tst-t1", "permit delay", service="Nusuk Masar Haj.Issue Permits")
        _save_incident("tst-t2", "permit stuck", service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_shrink", "تأخر إصدار التصاريح", "permit issuance delays",
                      ["tst-t1", "tst-t2"])

        _json_llm(monkeypatch, {"keep": ["tst-t1"],
                                "remove": [{"id": "tst-t2", "reason": "different"}],
                                "description": "تأخر إصدار التصاريح"})
        r = pc.audit_cluster("cl_tst_shrink")

        assert r["removed"] == ["tst-t2"]
        assert r["demoted"] is True
        assert store.get_cluster("cl_tst_shrink")["status"] == "retired"
        assert "tst-t1" in store.unassigned_incident_ids()  # individual now

    def test_sweep_reruns_flow_a_first(self, monkeypatch):
        # A ticket that now matches a new active cluster is assigned by Flow A
        # before the grouping call ever runs.
        _save_incident("tst-h0", "Rawdah permit fails", service="Nusuk Masar Haj.Issue Permits")
        _save_incident("tst-h1", "Rawdah permit booking error", service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_rawdah5", "فشل إصدار تصريح الروضة", "Rawdah permit fails",
                      ["tst-h0", "tst-h1"])
        _save_incident("tst-h2", "Rawdah permit again failing", service="Nusuk Masar Haj.Issue Permits")

        calls = []

        def responder(messages, **kw):
            calls.append(messages[-1]["content"])
            return json.dumps({"action": "assign", "cluster_id": "cl_tst_rawdah5",
                               "confidence": "high", "reason": "same"})

        _fake_llm(monkeypatch, responder)
        stats = pc.sweep_pool()

        assert stats["flow_a_assigned"] == 1
        assert "tst-h2" in store.cluster_member_ids("cl_tst_rawdah5")
        assert stats["pool_after"] == 0  # nothing left for the grouping call


# ── Flow C — nightly audit ───────────────────────────────────────────────

class TestFlowC:
    def test_audit_removes_wrong_member_back_to_pool(self, monkeypatch):
        _save_incident("tst-i1", "cannot enter pilgrim numbers", "form rejects input",
                       service="Nusuk Masar Haj.Registration")
        _save_incident("tst-i2", "commercial registration rejected", "number 5 rejected",
                       service="Nusuk Masar Haj.Registration")
        _save_incident("tst-i3", "commercial registration fails to submit",
                       service="Nusuk Masar Haj.Registration")
        _seed_cluster("cl_tst_reg", "رفض تسجيل سجلات تجارية",
                      "commercial registration numbers rejected", ["tst-i1", "tst-i2", "tst-i3"])

        def responder(messages, **kw):
            return json.dumps({
                "keep": ["tst-i2", "tst-i3"],
                "remove": [{"id": "tst-i1", "reason": "different problem: pilgrim numbers entry, not registration records"}],
                "description": "commercial registration records rejected",
            })

        _fake_llm(monkeypatch, responder)
        r = pc.audit_cluster("cl_tst_reg")

        assert r["removed"] == ["tst-i1"]
        assert store.cluster_member_ids("cl_tst_reg") == ["tst-i2", "tst-i3"]
        assert "tst-i1" in store.unassigned_incident_ids()  # back to the pool
        assert r["name_regenerated"] is True
        # removal logged per ticket
        log = store.list_assignment_log(incident_id="tst-i1")
        assert log[0]["verdict"]["action"] == "audit_remove"

    def test_audit_id_mismatch_discarded_whole(self, monkeypatch):
        _save_incident("tst-j1", "one", service="Nusuk Masar Haj.Service Unavailability")
        _save_incident("tst-j2", "two", service="Nusuk Masar Haj.Service Unavailability")
        _seed_cluster("cl_tst_audit1", "مجموعة", "desc", ["tst-j1", "tst-j2"])

        _json_llm(monkeypatch, {"keep": ["tst-j1"], "remove": [], "description": "d"})
        r = pc.audit_cluster("cl_tst_audit1")

        assert r["discarded"] == "id mismatch"
        assert store.cluster_member_ids("cl_tst_audit1") == ["tst-j1", "tst-j2"]  # untouched

    def test_audit_pruning_floor_blocks_over_pruning(self, monkeypatch):
        _save_incident("tst-k1", "one", service="Nusuk Masar Haj.Service Unavailability")
        _save_incident("tst-k2", "two", service="Nusuk Masar Haj.Service Unavailability")
        _save_incident("tst-k3", "three", service="Nusuk Masar Haj.Service Unavailability")
        _seed_cluster("cl_tst_audit2", "مجموعة", "desc",
                      ["tst-k1", "tst-k2", "tst-k3"])

        # remove 1 of 3 = 33% — under the floor, allowed; cluster stays at 2
        _json_llm(monkeypatch, {"keep": ["tst-k1", "tst-k2"],
                                "remove": [{"id": "tst-k3", "reason": "different"}],
                                "description": "d"})
        r = pc.audit_cluster("cl_tst_audit2")
        assert r["removed"] == ["tst-k3"]
        assert r["demoted"] is False  # still 2 members — stays a cluster
        assert store.cluster_member_ids("cl_tst_audit2") == ["tst-k1", "tst-k2"]

        # 67% removal (2 of 3) is a REAL cleanup of a mixed cluster — now
        # under the 85% floor it passes; keep=1 leaves it below the 2-member
        # rule, so the cluster demotes (members back to the individual pool).
        _save_incident("tst-k4", "four", service="Nusuk Masar Haj.Service Unavailability")
        store.add_cluster_member("cl_tst_audit2", "tst-k4", assigned_by="llm")
        _json_llm(monkeypatch, {"keep": ["tst-k1"],
                                "remove": [{"id": "tst-k4", "reason": "a"},
                                           {"id": "tst-k2", "reason": "b"}],
                                "description": "d"})
        r = pc.audit_cluster("cl_tst_audit2")
        assert r["demoted"] is True
        assert store.get_cluster("cl_tst_audit2")["status"] == "retired"
        assert store.cluster_member_ids("cl_tst_audit2") == []

    def test_audit_pruning_floor_still_blocks_near_total_removal(self, monkeypatch):
        """Remove 6 of 7 = 86% > 85% — still discarded whole (the floor only
        blocks 'remove everything', the demote path handles real cleanups)."""
        ids = [f"tst-p{i}" for i in range(7)]
        for iid in ids:
            _save_incident(iid, f"ticket {iid}",
                           service="Nusuk Masar Haj.Service Unavailability")
        _seed_cluster("cl_tst_floor", "مجموعة", "desc", ids)

        _json_llm(monkeypatch, {
            "keep": ids[:1],
            "remove": [{"id": iid, "reason": "different"} for iid in ids[1:]],
            "description": "d"})
        r = pc.audit_cluster("cl_tst_floor")
        assert r["discarded"] == "pruning floor"
        assert set(store.cluster_member_ids("cl_tst_floor")) == set(ids)


# ── Invariants + review gate + API shape ─────────────────────────────────

class TestFlowD:
    def test_arabic_name_short_accepted(self, monkeypatch):
        _fake_llm(monkeypatch, lambda messages, **kw: "فشل التحويلات المالية")
        name = pc._arabic_cluster_name([{"id": "x", "title": "t", "description": ""}])
        assert name == "فشل التحويلات المالية"

    def test_arabic_name_over_9_words_rejected(self, monkeypatch):
        """User rule: a label longer than 9 words is rejected — the fallback
        (first member title) is used so no cluster ever gets a long name."""
        _save_incident("tst-q1", "عنوان قصير", service="Nusuk Masar Haj.Service Unavailability")
        _save_incident("tst-q2", "عنوان آخر", service="Nusuk Masar Haj.Service Unavailability")
        long_name = "هذا اسم طويل جدا جدا جدا جدا جدا جدا جدا جدا جدا جدا جدا جدا جدا"  # >9 words, <60 chars
        _fake_llm(monkeypatch, lambda messages, **kw: long_name)
        name = pc._arabic_cluster_name(
            [{"id": "tst-q1", "title": "عنوان قصير", "description": ""}])
        assert name == "عنوان قصير"  # fell back, long label rejected

    def test_regenerate_name_stores_on_row_and_is_short(self, monkeypatch):
        _save_incident("tst-q3", "تحويل فاشل", service="Nusuk Masar Haj.Bill Payment")
        _save_incident("tst-q4", "تحويلات لا تصل", service="Nusuk Masar Haj.Bill Payment")
        _seed_cluster("cl_tst_name1", "اسم قديم طويل جدا جدا جدا جدا جدا",
                      "d", ["tst-q3", "tst-q4"])
        _fake_llm(monkeypatch, lambda messages, **kw: "فشل التحويلات المالية")
        name = pc.regenerate_name("cl_tst_name1")
        assert name == "فشل التحويلات المالية"
        assert store.get_cluster("cl_tst_name1")["name_ar"] == "فشل التحويلات المالية"
        assert len(name.split()) <= pc._AR_NAME_MAX_WORDS


class TestInvariantsAndGate:
    def test_one_cluster_per_incident_enforced(self):
        _save_incident("tst-l1", "shared ticket", service="Nusuk Masar Haj.Service Unavailability")
        _seed_cluster("cl_tst_one1", "أ", "d1", ["tst-l1"])
        _seed_cluster("cl_tst_one2", "ب", "d2", [])

        store.add_cluster_member("cl_tst_one2", "tst-l1", assigned_by="llm")

        assert store.incident_cluster("tst-l1")["id"] == "cl_tst_one2"
        assert "tst-l1" not in store.cluster_member_ids("cl_tst_one1")  # moved, not duplicated

    def test_proposal_approve_activates_reject_returns_to_pool(self):
        _save_incident("tst-m1", "payment fails", service="Nusuk Masar Haj.Bill Payment")
        _save_incident("tst-m2", "transfer rejected", service="Nusuk Masar Haj.Bill Payment")
        _seed_cluster("cl_tst_prop1", "فشل التحويلات", "financial transfers fail",
                      ["tst-m1", "tst-m2"], status="proposed")

        store.set_cluster_status("cl_tst_prop1", "active")
        assert store.get_cluster("cl_tst_prop1")["status"] == "active"
        assert "tst-m1" in store.cluster_member_ids("cl_tst_prop1")

        _seed_cluster("cl_tst_prop2", "مجموعة أخرى", "d", ["tst-m1", "tst-m2"],
                      status="proposed")
        store.remove_cluster_members("cl_tst_prop2")
        store.set_cluster_status("cl_tst_prop2", "retired")
        assert store.get_cluster("cl_tst_prop2")["status"] == "retired"
        assert store.cluster_member_ids("cl_tst_prop2") == []
        # members are back in the derived pool
        assert "tst-m1" in store.unassigned_incident_ids()

    def test_build_clusters_emits_dashboard_shape_from_tables(self):
        _save_incident("tst-n1", "Rawdah permit fails", "date picker",
                       service="Nusuk Masar Haj.Issue Permits")
        _save_incident("tst-n2", "Rawdah permit booking error", "cannot select",
                       service="Nusuk Masar Haj.Issue Permits")
        _save_incident("tst-n3", "unrelated CRM login", "credentials",
                       service="Nusuk Masar Haj.Service Unavailability")
        _seed_cluster("cl_tst_shape1", "فشل إصدار تصريح الروضة", "Rawdah permit issuance fails",
                      ["tst-n1", "tst-n2"])
        # a proposed cluster must NOT appear in the report (own member — the
        # one-cluster invariant moves a ticket only when actually assigned)
        _seed_cluster("cl_tst_shape2", "مقترح", "d", ["tst-n3"], status="proposed")

        rep = pc.build_clusters("daily")

        assert rep["total_incidents"] >= 2
        clusters = {c["cluster_id"]: c for c in rep["clusters"]}
        assert "cl_tst_shape1" in clusters
        assert "cl_tst_shape2" not in clusters
        c = clusters["cl_tst_shape1"]
        assert c["name"] == "فشل إصدار تصريح الروضة"
        assert c["count"] == 2
        assert c["worst_severity"] in ("Critical", "Major", "Minor", "Cosmetic")
        for key in ("affected_system", "affected_service", "summary", "pruned", "incidents"):
            assert key in c
        inc = c["incidents"][0]
        for key in ("id", "title", "severity", "status", "similarity_pct",
                    "canonical_statement", "description"):
            assert key in inc
        assert "subsystem_summary" in rep

    def test_unassigned_pool_is_derived(self):
        _save_incident("tst-o1", "lonely ticket", service="Nusuk Masar Haj.Service Unavailability")
        _save_incident("tst-o2", "clustered ticket", service="Nusuk Masar Haj.Service Unavailability")
        _seed_cluster("cl_tst_pool1", "مجموعة", "d", ["tst-o2"])
        pool = store.unassigned_incident_ids()
        assert "tst-o1" in pool and "tst-o2" not in pool


# ── v4 SYSTEM SCOPING — Hajj and Umrah never share a cluster ──────────────

class TestSystemScoping:
    """One cluster = ONE system (different teams): an incident may only join
    clusters of its own affected_system. Enforced at THREE layers:
      * retrieval (Flow A candidates are pre-filtered per system),
      * minting (Flow B batches the pool per system),
      * the store (add_cluster_member refuses cross-system inserts)."""

    def test_umrah_ticket_never_sees_hajj_cluster_as_candidate(self, monkeypatch):
        """Only a Hajj cluster exists — an Umrah ticket gets ZERO candidates
        (the LLM is never even called with a cross-system cluster)."""
        _save_incident("tst-sys-a1", "Rawdah permit fails", service="Nusuk Masar Haj.Issue Permits")
        _save_incident("tst-sys-a2", "Rawdah permit booking error", service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_hajj1", "فشل إصدار تصريح الروضة", "Rawdah permit issuance fails",
                      ["tst-sys-a1", "tst-sys-a2"], affected_system="Nusuk Masar Haj")
        # Same wording as the Hajj problem — but an Umrah ticket (different team).
        _save_incident("tst-sys-a3", "Rawdah permit fails on date selection", "date picker broken",
                       service="Nusuk Masar Umrah.System/Application", system="Nusuk Masar Umrah")

        called = []
        _fake_llm(monkeypatch, lambda messages, **kw: called.append(1) or "{}")
        r = pc.assign_incident("tst-sys-a3")

        assert r["action"] == "none_fit"
        assert r.get("reason") == "no active clusters"  # zero same-system candidates
        assert called == []                              # LLM never invoked
        assert store.incident_cluster("tst-sys-a3") is None

    def test_flow_a_candidate_list_is_system_filtered(self, monkeypatch):
        """With BOTH a Hajj and an Umrah cluster present, an Umrah ticket only
        sees the Umrah cluster — a hallucinated Hajj id is not among the
        candidates and hits the none_fit safeguard."""
        _save_incident("tst-sys-b1", "Rawdah permit fails", service="Nusuk Masar Umrah.Registration",
                       system="Nusuk Masar Umrah")
        _save_incident("tst-sys-b2", "Rawdah permit booking error", service="Nusuk Masar Umrah.Registration",
                       system="Nusuk Masar Umrah")
        _seed_cluster("cl_tst_umrah1", "فشل إصدار تصريح الروضة", "Rawdah permit issuance fails",
                      ["tst-sys-b1", "tst-sys-b2"], affected_system="Nusuk Masar Umrah")
        _save_incident("tst-sys-b3", "Hajj permit stuck", service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_hajj1", "فشل إصدار تصريح الروضة", "Rawdah permit issuance fails",
                      ["tst-sys-b3"], affected_system="Nusuk Masar Haj")
        # a NEW unassigned Umrah ticket — the assignment target
        _save_incident("tst-sys-b4", "Rawdah permit fails on date selection",
                       service="Nusuk Masar Umrah.Registration", system="Nusuk Masar Umrah")

        seen = []

        def responder(messages, **kw):
            seen.append(messages[-1]["content"])
            return json.dumps({"action": "assign", "cluster_id": "cl_tst_hajj1",
                               "confidence": "high", "reason": "same"})

        _fake_llm(monkeypatch, responder)
        r = pc.assign_incident("tst-sys-b4")

        # the Hajj cluster never reached the LLM prompt nor the assignment log
        assert "cl_tst_hajj1" not in seen[0]
        log = store.list_assignment_log(incident_id="tst-sys-b4")
        assert "cl_tst_hajj1" not in log[0]["candidates"]
        assert log[0]["candidates"] == ["cl_tst_umrah1"]
        # hallucinated id -> not a candidate -> safeguard -> none_fit
        assert r["action"] == "none_fit"
        assert store.incident_cluster("tst-sys-b4") is None

    def test_store_refuses_cross_system_membership(self):
        """The hard invariant: add_cluster_member refuses an Umrah incident in
        a Hajj cluster even when called directly (not via Flow A)."""
        _save_incident("tst-sys-c1", "hajj ticket", service="Nusuk Masar Haj.X")
        _save_incident("tst-sys-c2", "umrah ticket", service="Nusuk Masar Umrah.X",
                       system="Nusuk Masar Umrah")
        _save_incident("tst-sys-c3", "another hajj ticket", service="Nusuk Masar Haj.X")
        _seed_cluster("cl_tst_hajj2", "مجموعة", "d", ["tst-sys-c1"],
                      affected_system="Nusuk Masar Haj")

        assert store.add_cluster_member("cl_tst_hajj2", "tst-sys-c2") is False
        assert store.cluster_member_ids("cl_tst_hajj2") == ["tst-sys-c1"]
        # same-system join still works
        assert store.add_cluster_member("cl_tst_hajj2", "tst-sys-c3") is True
        assert set(store.cluster_member_ids("cl_tst_hajj2")) == {"tst-sys-c1", "tst-sys-c3"}

    def test_sweep_batches_pool_per_system(self, monkeypatch):
        """Flow B partitions the pool by system: each LLM grouping call sees
        ONE system only, and the minted clusters carry the right system —
        no batch can ever produce a mixed Hajj+Umrah cluster."""
        _save_incident("tst-sys-d0", "hajj payment fails", service="Nusuk Masar Haj.Bill Payment")
        _save_incident("tst-sys-d1", "hajj transfer rejected", service="Nusuk Masar Haj.Bill Payment")
        _save_incident("tst-sys-d2", "umrah payment fails", service="Nusuk Masar Umrah.Bill Payment",
                       system="Nusuk Masar Umrah")
        _save_incident("tst-sys-d3", "umrah transfer rejected", service="Nusuk Masar Umrah.Bill Payment",
                       system="Nusuk Masar Umrah")

        sweep_systems = []

        def responder(messages, **kw):
            body = messages[-1]["content"]
            if "candidate_clusters" in body:
                return json.dumps({"action": "none_fit", "cluster_id": None,
                                   "confidence": "low", "reason": "x"})
            payload = json.loads(body)
            sys_name = payload.get("system", "Unknown")
            ids = [t["id"] for t in payload["tickets"]]
            sweep_systems.append(sys_name)
            if len(ids) >= 2:
                return json.dumps({
                    "groups": [{"member_ids": ids, "name_ar": f"مجموعة {sys_name}",
                                "description": "d"}],
                    "singletons": []})
            return json.dumps({"groups": [], "singletons": ids})

        _fake_llm(monkeypatch, responder)
        stats = pc.sweep_pool()

        # every grouping call saw exactly ONE system — the pool was split
        # (the test DB is shared with other modules, so other systems may
        # appear too; the guarantee is that NO single call mixes systems)
        assert {"Nusuk Masar Haj", "Nusuk Masar Umrah"} <= set(sweep_systems)
        assert stats["proposals_created"] >= 2
        props = store.list_clusters(status="proposed")
        by_sys = {c["affected_system"]: set(store.cluster_member_ids(c["id"])) for c in props}
        assert by_sys["Nusuk Masar Haj"] == {"tst-sys-d0", "tst-sys-d1"}
        assert by_sys["Nusuk Masar Umrah"] == {"tst-sys-d2", "tst-sys-d3"}
        # every member belongs to its cluster's system (no mixed cluster)
        for c in props:
            for mid in store.cluster_member_ids(c["id"]):
                assert pc._system_of_inc(store.get_incident(mid)) == c["affected_system"]

    def test_report_emits_cluster_system_from_row(self):
        """The dashboard report carries the cluster's stored system — the
        system filter on the dashboard groups by this value."""
        _save_incident("tst-sys-e1", "umrah permit fails", service="Nusuk Masar Umrah.X",
                       system="Nusuk Masar Umrah")
        _save_incident("tst-sys-e2", "umrah permit error", service="Nusuk Masar Umrah.X",
                       system="Nusuk Masar Umrah")
        _seed_cluster("cl_tst_sysrep", "فشل إصدار تصريح العمرة", "Umrah permit issuance fails",
                      ["tst-sys-e1", "tst-sys-e2"], affected_system="Nusuk Masar Umrah")

        rep = pc.build_clusters("daily")
        clusters = {c["cluster_id"]: c for c in rep["clusters"]}
        assert "cl_tst_sysrep" in clusters
        assert clusters["cl_tst_sysrep"]["affected_system"] == "Nusuk Masar Umrah"

    def test_cluster_id_includes_system(self):
        """Hajj and Umrah often have identical problems/names — the id must
        not collide across systems."""
        a = pc._cluster_id("Nusuk Masar Haj", "مجموعة", ["x", "y"])
        b = pc._cluster_id("Nusuk Masar Umrah", "مجموعة", ["x", "y"])
        assert a != b

    def test_unknown_system_incidents_cluster_together(self):
        """Incidents with no classified system form their own 'Unknown'
        bucket — they never join a known-system cluster either."""
        _save_incident("tst-sys-f1", "no system ticket 1", system="")
        _save_incident("tst-sys-f2", "no system ticket 2", system="")
        _seed_cluster("cl_tst_unk1", "مجموعة", "d", ["tst-sys-f1"],
                      affected_system="Unknown")

        # unknown incident CAN join the unknown cluster
        assert store.add_cluster_member("cl_tst_unk1", "tst-sys-f2") is True
        # ...but a known-system incident CANNOT (even though the cluster is
        # unscoped, the invariant treats '' as 'Unknown')
        _save_incident("tst-sys-f3", "hajj ticket", service="Nusuk Masar Haj.X")
        assert store.add_cluster_member("cl_tst_unk1", "tst-sys-f3") is False
        assert "tst-sys-f3" not in store.cluster_member_ids("cl_tst_unk1")


# ── v5 — audit verdicts are binding, and the sweep stops re-minting ───────
#
# The v5 fixes all come from ONE prod symptom: the dashboard showed clusters
# the nightly audit had already declared impure. Flow C evicted a member, the
# 15-minute sweep handed it straight back (the two LLMs disagree and nothing
# recorded the disagreement), the audit removed it again the next night, and
# 60 empty retired rows piled up from naming clusters that had no members.

def _reclassify(iid: str, system: str, service: str = "Nusuk Masar Haj.X") -> None:
    """Simulate a RE-CLASSIFICATION: flip the stored system of an incident
    that already sits in a cluster (the drift the sweep must reconcile)."""
    cls = {"affected_system": system, "service": service, "severity": "Major",
           "confidence": "high", "canonical_statement": f"Incident reported: {iid}"}
    conn = store._getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE incidents SET classification_json = %s WHERE id = %s",
                        (json.dumps(cls, ensure_ascii=False), iid))
        conn.commit()
    finally:
        store._putconn(conn)


class TestAuditVerdictsAreBinding:
    def test_flow_a_will_not_re_add_an_audit_evicted_member(self, monkeypatch):
        """The core thrash: audit removes a wrong member, Flow A says 'assign'
        again 15 minutes later. The veto must win and the LLM must not even be
        asked (a vetoed candidate is dropped BEFORE the call)."""
        for i in ("tst-v1", "tst-v2", "tst-v3"):
            _save_incident(i, "Rawdah permit fails", service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_veto1", "فشل إصدار تصريح الروضة", "Rawdah permit fails",
                      ["tst-v1", "tst-v2", "tst-v3"])

        _json_llm(monkeypatch, {"keep": ["tst-v1", "tst-v2"],
                                "remove": [{"id": "tst-v3", "reason": "different problem"}],
                                "description": "Rawdah permit fails"})
        assert pc.audit_cluster("cl_tst_veto1")["removed"] == ["tst-v3"]
        assert "tst-v3" in store.unassigned_incident_ids()

        calls = []
        _fake_llm(monkeypatch, lambda messages, **kw: calls.append(1) or json.dumps(
            {"action": "assign", "cluster_id": "cl_tst_veto1",
             "confidence": "high", "reason": "looks the same to me"}))

        r = pc.assign_incident("tst-v3")

        assert r["action"] == "none_fit"
        assert calls == []                       # vetoed out before the LLM call
        assert "tst-v3" not in store.cluster_member_ids("cl_tst_veto1")
        assert "tst-v3" in store.unassigned_incident_ids()
        log = store.list_assignment_log(incident_id="tst-v3")[0]
        assert log["verdict"]["reason"] == "all candidate clusters already rejected this ticket in an audit"
        assert log["verdict"]["vetoed"] == ["cl_tst_veto1"]

    def test_veto_is_per_cluster_not_per_ticket(self, monkeypatch):
        """A ticket evicted from cluster X may still join cluster Y — the veto
        records one disagreement, it does not blacklist the ticket."""
        for i in ("tst-w1", "tst-w2", "tst-w3", "tst-w4"):
            _save_incident(i, "Rawdah permit fails", service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_veto2", "فشل إصدار تصريح الروضة", "Rawdah permit fails",
                      ["tst-w1", "tst-w2", "tst-w3"])
        _seed_cluster("cl_tst_veto3", "مجموعة أخرى", "other problem", ["tst-w4"])

        _json_llm(monkeypatch, {"keep": ["tst-w1", "tst-w2"],
                                "remove": [{"id": "tst-w3", "reason": "different"}],
                                "description": "Rawdah permit fails"})
        pc.audit_cluster("cl_tst_veto2")

        _json_llm(monkeypatch, {"action": "assign", "cluster_id": "cl_tst_veto3",
                                "confidence": "high", "reason": "fits the other one"})
        r = pc.assign_incident("tst-w3")

        assert r["action"] == "assign" and r["cluster_id"] == "cl_tst_veto3"
        assert "tst-w3" in store.cluster_member_ids("cl_tst_veto3")
        assert "tst-w3" not in store.cluster_member_ids("cl_tst_veto2")

    def test_audit_keep_clears_an_earlier_veto(self, monkeypatch):
        """The newest audit verdict wins: once the audit itself accepts the
        ticket again (it came back through a human/admin path), Flow A is free
        to assign it."""
        for i in ("tst-x1", "tst-x2"):
            _save_incident(i, "Rawdah permit fails", service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_veto4", "فشل إصدار تصريح الروضة", "Rawdah permit fails",
                      ["tst-x1", "tst-x2"])
        _save_incident("tst-x3", "Rawdah permit fails again", service="Nusuk Masar Haj.Issue Permits")

        # 1) evicted -> veto
        store.log_assignment("tst-x3", ["cl_tst_veto4"], {
            "action": "audit_remove", "cluster_id": "cl_tst_veto4", "reason": "different"},
            pc.PROMPT_VERSION, "test-model")
        assert store.audit_vetoes("tst-x3", ["cl_tst_veto4"], ttl_days=30) == {"cl_tst_veto4"}

        # 2) a later audit KEEPS it -> veto gone
        store.log_assignment("tst-x3", ["cl_tst_veto4"], {
            "action": "audit_keep", "cluster_id": "cl_tst_veto4"},
            pc.PROMPT_VERSION, "test-model")
        assert store.audit_vetoes("tst-x3", ["cl_tst_veto4"], ttl_days=30) == set()

        _json_llm(monkeypatch, {"action": "assign", "cluster_id": "cl_tst_veto4",
                                "confidence": "high", "reason": "same"})
        r = pc.assign_incident("tst-x3")
        assert r["action"] == "assign"
        assert "tst-x3" in store.cluster_member_ids("cl_tst_veto4")

    def test_veto_expires_after_ttl(self, monkeypatch):
        """A veto older than the TTL stops blocking, so a misjudged audit
        verdict cannot lock a ticket out forever."""
        _save_incident("tst-y1", "Rawdah permit fails", service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_veto5", "فشل إصدار تصريح الروضة", "Rawdah permit fails",
                      ["tst-y1"])
        _json_llm(monkeypatch, {"keep": ["tst-y1"], "remove": [],
                                "description": "Rawdah permit fails"})
        pc.audit_cluster("cl_tst_veto5")

        store.log_assignment("tst-y1", ["cl_tst_veto5"], {
            "action": "audit_remove", "cluster_id": "cl_tst_veto5", "reason": "different"},
            pc.PROMPT_VERSION, "test-model")
        conn = store._getconn()
        try:
            with conn.cursor() as cur:  # age the eviction past the TTL
                cur.execute("UPDATE assignment_log SET created_at = NOW() - INTERVAL '31 days' "
                            "WHERE incident_id = 'tst-y1' AND verdict->>'action' = 'audit_remove'")
            conn.commit()
        finally:
            store._putconn(conn)

        assert store.audit_vetoes("tst-y1", ["cl_tst_veto5"], ttl_days=30) == set()
        assert store.audit_vetoes("tst-y1", ["cl_tst_veto5"], ttl_days=None) == {"cl_tst_veto5"}


class TestSweepDoesNotReMintRejectedClusters:
    def test_flow_b_skips_a_group_the_audit_already_rejected(self, monkeypatch):
        """Tickets evicted from a cluster must not be re-minted as a brand-new
        cluster with the same name (that is how prod grew 8 retired twins of
        one 19-member Rawdah cluster)."""
        for i in ("tst-z1", "tst-z2", "tst-z3", "tst-z4"):
            _save_incident(i, "Rawdah permit fails", service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_dup1", "فشل إصدار تصريح الروضة", "Rawdah permit fails",
                      ["tst-z1", "tst-z2", "tst-z3", "tst-z4"])

        def responder(messages, **kw):
            body = messages[-1]["content"]
            if '"keep"' in body:                  # Flow C audit (1 call)
                return json.dumps({"keep": ["tst-z1", "tst-z2"],
                                   "remove": [{"id": "tst-z3", "reason": "different"},
                                              {"id": "tst-z4", "reason": "different"}],
                                   "description": "فشل إصدار تصريح الروضة"})
            if "NOC shift dashboard" in body:     # Flow D naming (1 call)
                return "فشل إصدار تصريح الروضة"
            if "candidate_clusters" in body:      # Flow A phase
                return json.dumps({"action": "none_fit", "cluster_id": None,
                                   "confidence": "low", "reason": "x"})
            ids = [t["id"] for t in json.loads(body)["tickets"]]  # Flow B grouping
            return json.dumps({"groups": [{"member_ids": ids,
                                           "name_ar": "فشل إصدار تصريح الروضة",
                                           "description": "d"}],
                               "singletons": []})

        _fake_llm(monkeypatch, responder)

        # 2 of 4 = 50% — under the floor, and 2 members stay: cluster survives
        assert pc.audit_cluster("cl_tst_dup1")["removed"] == ["tst-z3", "tst-z4"]
        c = store.get_cluster("cl_tst_dup1")
        assert c["status"] == "active" and c["name_ar"] == "فشل إصدار تصريح الروضة"

        stats = pc.sweep_pool()

        assert stats["veto_skipped"] == 1
        assert stats["proposals_created"] == 0
        assert [c["id"] for c in store.list_clusters(status="active")] == ["cl_tst_dup1"]
        assert "tst-z3" in store.unassigned_incident_ids()
        # a group the LLM names DIFFERENTLY is still allowed (narrow guard)
        assert pc._audit_rejected_by_named_cluster(["tst-z3"], "مشكلة أخرى", "Nusuk Masar Haj") is None

    def test_sweep_payload_carries_the_classified_service(self, monkeypatch):
        """The grouping prompt demands 'same service surface' — the service
        labels must actually reach the LLM (they were missing, which is how
        symptom-level clusters spanning unrelated services were born)."""
        _save_incident("tst-sv1", "export the report list fails", "cannot export xlsx",
                       service="Nusuk Masar Haj.Track Appeal Status")
        _save_incident("tst-sv2", "evaluation form upload fails", "upload rejected",
                       service="Nusuk Masar Haj.Submit Evaluation")

        seen = {}

        def responder(messages, **kw):
            body = messages[-1]["content"]
            if "candidate_clusters" in body:
                return json.dumps({"action": "none_fit", "cluster_id": None,
                                   "confidence": "low", "reason": "x"})
            payload = json.loads(body)
            seen["tickets"] = payload["tickets"]
            return json.dumps({"groups": [], "singletons": [t["id"] for t in payload["tickets"]]})

        _fake_llm(monkeypatch, responder)
        pc.sweep_pool()

        by_id = {t["id"]: t for t in seen["tickets"]}
        assert by_id["tst-sv1"]["service"].endswith("Track Appeal Status")
        assert by_id["tst-sv2"]["service"].endswith("Submit Evaluation")
        assert all("offering" in t for t in seen["tickets"])


class TestSystemReconciliation:
    def test_sweep_evicts_cross_system_member_after_reclassification(self):
        """v4 blocks cross-system INSERTS; a ticket RE-CLASSIFIED into another
        system afterwards still sat in the cluster (prod: an 'Other' ticket in
        a Hajj cluster). The sweep reconciles it."""
        _save_incident("tst-rec1", "permit delay", service="Nusuk Masar Haj.Issue Permits")
        _save_incident("tst-rec2", "permit stuck", service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_rec1", "تأخر إصدار التصاريح", "permit delays",
                      ["tst-rec1", "tst-rec2"], affected_system="Nusuk Masar Haj")

        _reclassify("tst-rec2", "Nusuk Masar Umrah", "Nusuk Masar Umrah.X")

        assert store.cross_system_members("cl_tst_rec1", "Nusuk Masar Haj") == ["tst-rec2"]
        n = pc._evict_cross_system_members()

        assert n == 1
        assert store.cluster_member_ids("cl_tst_rec1") == ["tst-rec1"]
        assert "tst-rec2" in store.unassigned_incident_ids()
        assert store.list_assignment_log(incident_id="tst-rec2")[0]["verdict"]["action"] == "system_evict"
        # the store keeps refusing the insert, so the eviction is stable
        assert store.add_cluster_member("cl_tst_rec1", "tst-rec2") is False

    def test_legacy_foreign_member_is_evicted(self):
        """Rows written BEFORE the invariant existed (prod: 4 Umrah tickets in
        Hajj clusters since 2026-08-24) are cleaned by the same pass."""
        _save_incident("tst-rec3", "rawdah permit fails", service="Nusuk Masar Haj.Issue Permits")
        _save_incident("tst-rec4", "rawdah permit fails", service="Nusuk Masar Umrah.X",
                       system="Nusuk Masar Umrah")
        _seed_cluster("cl_tst_rec2", "فشل إصدار تصريح الروضة", "rawdah fails",
                      ["tst-rec3"], affected_system="Nusuk Masar Haj")
        # bypass the guard the way a pre-v4 row did: straight into the table
        conn = store._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO cluster_members (cluster_id, incident_id, assigned_by, confidence) "
                            "VALUES (%s, %s, 'legacy', 'high')", ("cl_tst_rec2", "tst-rec4"))
            conn.commit()
        finally:
            store._putconn(conn)

        assert pc._evict_cross_system_members() == 1
        assert store.cluster_member_ids("cl_tst_rec2") == ["tst-rec3"]
        # the reconciliation did NOT create an audit veto (system reason, not fit)
        assert store.audit_vetoes("tst-rec4", ["cl_tst_rec2"], ttl_days=None) == set()


class TestNoNamingOfEmptyClusters:
    def test_audit_demote_does_not_rename_the_empty_cluster(self, monkeypatch):
        """Naming an empty cluster is what stored 'لا توجد تذاكر في العنقود'
        ('there are no tickets in the cluster') on 60 retired prod rows: the
        demote path removed every member and then asked the LLM for a name."""
        _save_incident("tst-nm1", "permit delay", service="Nusuk Masar Haj.Issue Permits")
        _save_incident("tst-nm2", "permit stuck", service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_nm1", "تأخر إصدار التصاريح", "permit delays",
                      ["tst-nm1", "tst-nm2"])

        calls = []
        _fake_llm(monkeypatch, lambda messages, **kw: calls.append(1) or json.dumps({
            "keep": ["tst-nm1"],
            "remove": [{"id": "tst-nm2", "reason": "different"}],
            "description": "تأخر إصدار التصاريح"}))

        r = pc.audit_cluster("cl_tst_nm1")

        assert r["demoted"] is True
        assert r["name_regenerated"] is False
        assert len(calls) == 1  # audit only — no naming call
        assert store.get_cluster("cl_tst_nm1")["name_ar"] == "تأخر إصدار التصاريح"

    def test_regenerate_name_is_a_noop_for_a_cluster_without_members(self, monkeypatch):
        _seed_cluster("cl_tst_nm2", "مجموعة", "d", [])
        calls = []
        _fake_llm(monkeypatch, lambda messages, **kw: calls.append(1) or "لا توجد تذاكر في العنقود")

        name = pc.regenerate_name("cl_tst_nm2")

        assert calls == []
        assert name == "مجموعة"

    def test_arabic_cluster_name_never_asks_the_llm_for_an_empty_list(self, monkeypatch):
        _fake_llm(monkeypatch, lambda messages, **kw: "لا توجد تذاكر في العنقود")
        assert pc._arabic_cluster_name([]) == "مجموعة"


class TestAuditSeesTheServiceSurface:
    """The audit could only compare WORDING before v5 — it never received the
    service, which is why prod kept a mixed cluster whose own name was an OR
    ('فشل في عمليات رفع أو تصدير البيانات' = an evaluation-form upload + a
    report export). The service must reach the prompt."""

    def test_audit_payload_carries_each_member_service(self, monkeypatch):
        _save_incident("tst-svc1", "export the report list fails",
                       service="Nusuk Masar Haj.Track Appeal Status")
        _save_incident("tst-svc2", "evaluation form upload fails",
                       service="Nusuk Masar Haj.Submit Evaluation")
        _seed_cluster("cl_tst_svc", "مجموعة", "d", ["tst-svc1", "tst-svc2"])

        seen = {}

        def responder(messages, **kw):
            if "Tickets:" in messages[-1]["content"]:      # audit call
                seen["audit"] = messages[-1]["content"]
                return json.dumps({"keep": ["tst-svc1", "tst-svc2"], "remove": [],
                                   "description": "d"})
            return "مجموعة"                                 # naming call

        _fake_llm(monkeypatch, responder)
        pc.audit_cluster("cl_tst_svc")

        assert "Track Appeal Status" in seen["audit"]
        assert "Submit Evaluation" in seen["audit"]
        assert "tst-svc1" in seen["audit"] and "tst-svc2" in seen["audit"]

    def test_audit_prompt_states_the_surface_and_conservative_rules(self):
        assert "SERVICE SURFACE" in pc.AUDIT_PROMPT
        assert "DIFFERENT services are different" in pc.AUDIT_PROMPT
        assert "A REASON TO REMOVE" in pc.AUDIT_PROMPT          # the conservative list
        assert "money transfers" in pc.AUDIT_PROMPT
        # the assign prompt must require the same failing flow across services
        assert "SERVICE SURFACE" in pc.ASSIGN_PROMPT


class TestHumanPinnedMembers:
    """An operator override must survive the nightly audit.

    The audit is an LLM and disagrees with human judgement on borderline
    tickets; without pinning, every manual fix was undone the next night (the
    audit re-removed reinstated members within hours in prod).
    """

    def test_audit_cannot_remove_a_human_pinned_member(self, monkeypatch):
        _save_incident("tst-pin1", "permit delay", service="Nusuk Masar Haj.Issue Permits")
        _save_incident("tst-pin2", "permit stuck", service="Nusuk Masar Haj.Issue Permits")
        _save_incident("tst-pin3", "permit frozen", service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_pin", "تأخر إصدار التصاريح", "permit delays",
                      ["tst-pin1", "tst-pin2"])
        # an operator adds the third ticket by hand
        assert store.add_cluster_member("cl_tst_pin", "tst-pin3", assigned_by="human") is True

        _json_llm(monkeypatch, {
            "keep": ["tst-pin1", "tst-pin2"],
            "remove": [{"id": "tst-pin3", "reason": "the model disagrees with the operator"}],
            "description": "تأخر إصدار التصاريح"})
        r = pc.audit_cluster("cl_tst_pin")

        assert r["removed"] == []
        assert r["pinned"] == ["tst-pin3"]
        assert "tst-pin3" in store.cluster_member_ids("cl_tst_pin")
        log = store.list_assignment_log(incident_id="tst-pin3")[0]
        assert log["verdict"]["action"] == "audit_remove_overruled"
        assert log["verdict"]["overruled_because"] == "member pinned by a human"

    def test_llm_assigned_members_are_still_removable(self, monkeypatch):
        _save_incident("tst-pin4", "permit delay", service="Nusuk Masar Haj.Issue Permits")
        _save_incident("tst-pin5", "bus data error", service="Nusuk Masar Haj.Manage Buses")
        _save_incident("tst-pin6", "permit stuck", service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_pin2", "تأخر إصدار التصاريح", "permit delays",
                      ["tst-pin4", "tst-pin5", "tst-pin6"])

        _json_llm(monkeypatch, {
            "keep": ["tst-pin4", "tst-pin6"],
            "remove": [{"id": "tst-pin5", "reason": "a different problem"}],
            "description": "تأخر إصدار التصاريح"})
        r = pc.audit_cluster("cl_tst_pin2")

        assert r["removed"] == ["tst-pin5"]
        assert r["pinned"] == []
        assert "tst-pin5" not in store.cluster_member_ids("cl_tst_pin2")

    def test_demotion_never_evicts_a_pinned_member(self, monkeypatch):
        """Demoting a cluster returns EVERY member to the pool — which would
        silently undo the operator's pin. The cluster is kept instead."""
        _save_incident("tst-pin7", "permit delay", service="Nusuk Masar Haj.Issue Permits")
        _save_incident("tst-pin8", "permit stuck", service="Nusuk Masar Haj.Issue Permits")
        _save_incident("tst-pin9", "permit frozen", service="Nusuk Masar Haj.Issue Permits")
        _seed_cluster("cl_tst_pin3", "تأخر إصدار التصاريح", "permit delays",
                      ["tst-pin7", "tst-pin8", "tst-pin9"])
        store.add_cluster_member("cl_tst_pin3", "tst-pin7", assigned_by="human")

        _json_llm(monkeypatch, {
            "keep": ["tst-pin7"],
            "remove": [{"id": "tst-pin8", "reason": "different"},
                       {"id": "tst-pin9", "reason": "also different"}],
            "description": "تأخر إصدار التصاريح"})
        r = pc.audit_cluster("cl_tst_pin3")

        assert r["removed"] == ["tst-pin8", "tst-pin9"]
        assert r["demoted"] is False
        assert store.cluster_member_ids("cl_tst_pin3") == ["tst-pin7"]
        assert store.get_cluster("cl_tst_pin3")["status"] == "active"


class TestServiceConsistencyGuard:
    """Two taxonomy services = two problems — enforced without asking the LLM.

    The grouping LLM still merges tickets that share only a symptom word
    ("cannot enter pilgrim data") while sitting on different services; prod got
    a 'Between cities · Register Arrival' ticket grouped with a
    'Registration · application form' one. The mint path refuses such groups.
    """

    def test_group_spanning_two_services_is_not_minted(self, monkeypatch):
        _save_incident("tst-sg1", "cannot enter the pilgrim in the manifest",
                       service="Between cities - Nusuk Masar Haj.Register Arrival/Departure for in house")
        _save_incident("tst-sg2", "cannot enter pilgrim numbers",
                       service="Registration - Nusuk Masar Haj.Fill out the application form")

        def responder(messages, **kw):
            body = messages[-1]["content"]
            if "candidate_clusters" in body:
                return json.dumps({"action": "none_fit", "cluster_id": None,
                                   "confidence": "low", "reason": "x"})
            ids = [t["id"] for t in json.loads(body)["tickets"]]
            return json.dumps({"groups": [{"member_ids": ids, "name_ar": "فشل إدخال بيانات الحجاج",
                                           "description": "d"}], "singletons": []})

        _fake_llm(monkeypatch, responder)
        stats = pc.sweep_pool()

        assert stats["service_split_skipped"] == 1
        assert stats["proposals_created"] == 0
        assert store.list_clusters() == []
        assert "tst-sg1" in store.unassigned_incident_ids()

    def test_same_service_group_is_still_minted(self, monkeypatch):
        _save_incident("tst-sg3", "manifest entry error for pilgrims",
                       service="Between cities - Nusuk Masar Haj.Register Arrival/Departure for in house")
        _save_incident("tst-sg4", "manifest entry rejects the group",
                       service="Between cities - Nusuk Masar Haj.Register Arrival/Departure for in house")

        def responder(messages, **kw):
            body = messages[-1]["content"]
            if "candidate_clusters" in body:
                return json.dumps({"action": "none_fit", "cluster_id": None,
                                   "confidence": "low", "reason": "x"})
            ids = [t["id"] for t in json.loads(body)["tickets"]]
            return json.dumps({"groups": [{"member_ids": ids, "name_ar": "فشل إدخال بيانات الترحيل",
                                           "description": "d"}], "singletons": []})

        _fake_llm(monkeypatch, responder)
        stats = pc.sweep_pool()

        assert stats["service_split_skipped"] == 0
        assert stats["proposals_created"] == 1

    def test_generic_services_do_not_block_a_group(self):
        """A generic/abstained service carries no surface information."""
        _save_incident("tst-sg5", "permit error one", service="System/Application - Nusuk Masar Haj")
        _save_incident("tst-sg6", "permit error two", service="Istiqbal - Nusuk Masar Haj.OFFERING-GAP")
        assert pc._inconsistent_services(["tst-sg5", "tst-sg6"]) is False
        assert pc._service_key(store.get_incident("tst-sg5")) == ""
        assert pc._service_key(store.get_incident("tst-sg6")) == ""

    def test_service_key_ignores_the_offering(self):
        _save_incident("tst-sg7", "a", service="Between cities - Nusuk Masar Haj.Manage Buses")
        _save_incident("tst-sg8", "b", service="Between cities - Nusuk Masar Haj.Register Arrival/Departure for in house")
        assert pc._inconsistent_services(["tst-sg7", "tst-sg8"]) is False


class TestProductionRobustness:
    """The maintenance paths must be safe to run on a live system at any time."""

    def test_flow_a_on_an_incident_without_a_classification(self):
        """A half-ingested row must not crash the sweep."""
        store.save_incident("tst-rob1", "half ingested ticket", "",
                            type("R", (), {"canonical_statement": "", "severity": "Minor",
                                           "affected_system": "", "service": "",
                                           "model_dump_json": lambda self: "{}"})(),
                            "", content_hash="tst-rob1", source_ticket_ids=["tst-rob1"])
        r = pc.assign_incident("tst-rob1")
        assert r["action"] in ("none_fit", "error")

    def test_sweep_survives_a_cluster_with_no_members(self):
        store.create_cluster("cl_tst_empty", "مجموعة", "d", status="active",
                             affected_system="Nusuk Masar Haj")
        stats = pc.sweep_pool()
        assert stats["demoted"] >= 1                       # retired, not crashed
        assert store.get_cluster("cl_tst_empty")["status"] == "retired"

    def test_service_helpers_tolerate_missing_incidents(self):
        assert pc._service_key({}) == ""
        assert pc._inconsistent_services([]) is False
        assert pc._inconsistent_services(["does-not-exist"]) is False

    def test_audit_vetoes_tolerate_empty_input(self):
        assert store.audit_vetoes("tst-rob1", []) == set()
        assert store.audit_vetoes("does-not-exist", ["cl_tst_empty"]) == set()

    def test_cross_system_reconciliation_is_a_noop_on_a_clean_db(self):
        assert pc._evict_cross_system_members() == 0

    def test_create_cluster_is_idempotent_under_a_race(self):
        """Two workers minting the same content-derived id: the second gets
        None instead of a UniqueViolation that would abort the sweep."""
        first = store.create_cluster("cl_tst_race", "مجموعة", "d", status="active",
                                     affected_system="Nusuk Masar Haj")
        second = store.create_cluster("cl_tst_race", "مجموعة", "d", status="active",
                                      affected_system="Nusuk Masar Haj")
        assert first is not None and second is None
        assert store.get_cluster("cl_tst_race")["name_ar"] == "مجموعة"
