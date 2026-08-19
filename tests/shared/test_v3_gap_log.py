"""Classifier v3 gap/log tables — taxonomy_gaps upsert aggregation and
classification_log listing/filtering.

Runs against a real Postgres database (see conftest.py), same convention as
test_incident_store.py: mocked embedding model, isolated test database.
"""

from dataclasses import replace

import numpy as np
import pytest

from ai_classification.shared.config import settings as base_settings
from ai_classification.shared.store import IncidentStore, VECTOR_DIM

from tests.conftest import TEST_PG_DATABASE


class _FakeEmbedder:
    """Deterministic stand-in embedding model (dim matches the vector column)."""

    def encode(self, text: str, normalize_embeddings: bool = True) -> np.ndarray:
        rng = np.random.RandomState(abs(hash(text)) % (2 ** 31))
        v = rng.randn(VECTOR_DIM).astype(np.float32)
        if normalize_embeddings:
            norm = np.linalg.norm(v)
            if norm > 0:
                v = v / norm
        return v.astype(np.float32)


def _make_store(monkeypatch) -> IncidentStore:
    import ai_classification.shared.store as store_mod

    monkeypatch.setattr(store_mod, "SentenceTransformer", lambda *a, **_k: _FakeEmbedder())
    test_settings = replace(base_settings, pg_database=TEST_PG_DATABASE)
    monkeypatch.setattr(store_mod, "settings", test_settings)

    s = IncidentStore()
    s.setup()
    conn = s._getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE incidents, taxonomy_gaps, classification_log")
        conn.commit()
    finally:
        s._putconn(conn)
    return s


@pytest.fixture
def store(monkeypatch):
    s = _make_store(monkeypatch)
    yield s
    s.close()


# ── record_taxonomy_gap ────────────────────────────────────────────────

class TestRecordTaxonomyGap:
    def test_upsert_aggregates_count_and_refs(self, store):
        store.record_taxonomy_gap("System A", "Missing offering X", "inc-001")
        store.record_taxonomy_gap("System A", "Missing offering X", "inc-002")

        gaps = store.list_taxonomy_gaps()
        assert len(gaps) == 1, "same (service, suggestion) must aggregate into one row"
        gap = gaps[0]
        assert gap["service"] == "System A"
        assert gap["suggested_offering"] == "Missing offering X"
        assert gap["count"] == 2
        assert sorted(gap["incident_refs"]) == ["inc-001", "inc-002"]

    def test_distinct_suggestions_are_separate_rows(self, store):
        store.record_taxonomy_gap("System A", "Offering X", "inc-001")
        store.record_taxonomy_gap("System A", "Offering Y", "inc-001")
        gaps = store.list_taxonomy_gaps()
        assert len(gaps) == 2

    def test_same_ref_twice_counts_twice(self, store):
        """The same incident hitting the gap twice is two occurrences."""
        store.record_taxonomy_gap("Sys", "S", "inc-1")
        store.record_taxonomy_gap("Sys", "S", "inc-1")
        gaps = store.list_taxonomy_gaps()
        assert gaps[0]["count"] == 2
        assert gaps[0]["incident_refs"] == ["inc-1", "inc-1"]

    def test_ordered_by_count_desc(self, store):
        store.record_taxonomy_gap("Sys", "S1", "i1")
        store.record_taxonomy_gap("Sys", "S1", "i2")
        store.record_taxonomy_gap("Sys", "S2", "i3")
        gaps = store.list_taxonomy_gaps()
        assert gaps[0]["suggested_offering"] == "S1"
        assert gaps[0]["count"] == 2
        assert gaps[1]["count"] == 1


# ── log_classification / list_classification_log ───────────────────────

class TestClassificationLog:
    def test_list_filters_by_incident_ref(self, store):
        store.log_classification("inc-001", "triage", "2026-08-v3", "model-x", '{"kind":"incident"}')
        store.log_classification("inc-001", "stage3", "2026-08-v3", "model-x", '{"service":"A.B"}')
        store.log_classification("inc-002", "triage", "2026-08-v3", "model-x", '{"kind":"inquiry"}')

        rows = store.list_classification_log(incident_ref="inc-001")
        assert len(rows) == 2
        assert all(r["incident_ref"] == "inc-001" for r in rows)
        assert {r["stage"] for r in rows} == {"triage", "stage3"}

        all_rows = store.list_classification_log()
        assert len(all_rows) == 3

    def test_limit(self, store):
        for i in range(5):
            store.log_classification(f"inc-{i}", "triage", "v", "m", "raw")
        assert len(store.list_classification_log(limit=2)) == 2

    def test_extra_json_roundtrip(self, store):
        store.log_classification("inc-1", "verification", "v", "m", "raw",
                                 extra={"verdict": "approve", "n": 1})
        row = store.list_classification_log(incident_ref="inc-1")[0]
        assert row["stage"] == "verification"
        assert row["prompt_version"] == "v"
        assert row["model"] == "m"
        assert row["raw_verdict"] == "raw"
        assert row["extra"] == {"verdict": "approve", "n": 1}
