"""v2 LLM-first persistent clustering — Flows A/B/C/D + invariants + API shape.

Real test DB (ai_incidents_test, forced by conftest) + real bge-m3 embeddings;
the LLM is mocked at the module seam (persistent.call_llm) so every flow is
deterministic. Coverage:

  Flow A — assign (high/medium), none_fit, assign+low, hallucinated cluster id,
           assignment_log row with prompt_version
  Flow B — grouping -> proposals, 2-member birth, ID-mismatch discard
  Flow C — member removal -> pool, refined description, ID mismatch + 60% floor
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
                   status: str = "active") -> str:
    """Insert an incident directly (no LLM classification) with a fake
    classification_json carrying the service + a canonical statement."""
    cls = {"affected_system": "Nusuk Masar Haj", "service": service,
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
                  member_ids: list[str], status: str = "active") -> str:
    store.create_cluster(cid, name_ar, description, status=status)
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

        # now remove 2 of 3 = 67% > 60% — verdict discarded, members untouched
        _save_incident("tst-k4", "four", service="Nusuk Masar Haj.Service Unavailability")
        store.add_cluster_member("cl_tst_audit2", "tst-k4", assigned_by="llm")
        _json_llm(monkeypatch, {"keep": ["tst-k1"],
                                "remove": [{"id": "tst-k4", "reason": "a"},
                                           {"id": "tst-k2", "reason": "b"}],
                                "description": "d"})
        r = pc.audit_cluster("cl_tst_audit2")
        assert r["discarded"] == "pruning floor"
        assert store.cluster_member_ids("cl_tst_audit2") == ["tst-k1", "tst-k2", "tst-k4"]


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
