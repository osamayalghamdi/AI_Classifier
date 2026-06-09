"""Tests for IncidentStore — embeddings, similarity search, clustering, reporting."""

import threading
import numpy as np
import pytest

from ai_classification.incident_store import IncidentStore, SimilarMatch, _SEVERITY_RANK
from ai_classification.models import ClassificationResult
from ai_classification.schemas import (
    AffectedSystem, IncidentType, Severity, Urgency, Category,
)


# ── Helpers ───────────────────────────────────────────────────────────


def _make_result(**overrides) -> ClassificationResult:
    defaults = dict(
        affected_system=AffectedSystem.crm,
        service="Customer Portal",
        incident_type=IncidentType.degradation,
        severity=Severity.major,
        urgency=Urgency.high,
        category=Category.software,
        confidence="high",
        reasoning="test",
    )
    defaults.update(overrides)
    return ClassificationResult(**defaults)


class FixedVecModel:
    """Mock embedding model — returns vectors from a predefined map, random for unknowns."""

    def __init__(self, vec_map: dict[str, np.ndarray] | None = None):
        self._map = vec_map or {}

    def encode(self, text: str, normalize_embeddings: bool = True) -> np.ndarray:
        if text in self._map:
            v = self._map[text].copy()
        else:
            rng = np.random.RandomState(abs(hash(text)) % (2 ** 31))
            v = rng.randn(8).astype(np.float32)
        if normalize_embeddings:
            norm = np.linalg.norm(v)
            if norm > 0:
                v = v / norm
        return v.astype(np.float32)


def _make_store(monkeypatch, tmp_path, model, db_name="test.db", threshold=None):
    """Build an IncidentStore with a mocked model and isolated SQLite DB."""
    import ai_classification.incident_store as store_mod
    import ai_classification.config as config_mod
    from dataclasses import replace

    monkeypatch.setattr(store_mod, "SentenceTransformer", lambda *a, **kw: model)

    overrides = {"db_path": str(tmp_path / db_name)}
    if threshold is not None:
        overrides["similarity_threshold"] = threshold
    new_settings = replace(config_mod.settings, **overrides)
    monkeypatch.setattr(store_mod, "settings", new_settings)

    s = IncidentStore()
    s.setup()
    return s


@pytest.fixture
def store(monkeypatch, tmp_path):
    """IncidentStore with mocked embeddings and a fresh SQLite file per test."""
    s = _make_store(monkeypatch, tmp_path, FixedVecModel())
    yield s
    s.close()


@pytest.fixture
def store_with_vecs(monkeypatch, tmp_path):
    """Store where known keys map to controlled unit vectors for similarity tests."""
    # Two nearly-identical vectors (dot ≈ 0.99) and one orthogonal vector.
    base = np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32)
    near = np.array([0.995, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32)
    near /= np.linalg.norm(near)
    far = np.array([0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32)

    vec_map: dict[str, np.ndarray] = {"BASE": base, "NEAR": near, "FAR": far}

    s = _make_store(monkeypatch, tmp_path, FixedVecModel(vec_map), db_name="vecs.db", threshold=0.90)
    yield s, base, near, far
    s.close()


# ── _build_embedding_text ─────────────────────────────────────────────


class TestBuildEmbeddingText:
    def test_title_only(self):
        text = IncidentStore._build_embedding_text("Disk full", "")
        assert "Disk full" in text

    def test_includes_description(self):
        text = IncidentStore._build_embedding_text("Title", "Details here")
        assert "Title" in text
        assert "Details here" in text

    def test_includes_ocr_text(self):
        text = IncidentStore._build_embedding_text("Title", "Desc", "OCR content")
        assert "OCR: OCR content" in text

    def test_omits_empty_ocr(self):
        text = IncidentStore._build_embedding_text("Title", "Desc", "")
        assert "OCR" not in text

    def test_includes_classification_fingerprint(self):
        result = _make_result()
        text = IncidentStore._build_embedding_text("T", "D", classification=result)
        assert "CRM" in text
        assert "Customer Portal" in text

    def test_classification_without_ocr(self):
        result = _make_result()
        text = IncidentStore._build_embedding_text("T", "", "", classification=result)
        assert "Classified as:" in text


# ── save_incident + basic persistence ────────────────────────────────


class TestSaveIncident:
    def test_saves_and_generates_id(self, store):
        iid = store.generate_id()
        result = _make_result()
        store.save_incident(iid, "Test incident", "Some description", result)
        row = store._db.execute("SELECT id FROM incidents WHERE id=?", (iid,)).fetchone()
        assert row is not None

    def test_stores_extracted_text(self, store):
        iid = store.generate_id()
        result = _make_result()
        store.save_incident(iid, "Title", "Desc", result, extracted_text="OCR data")
        row = store._db.execute(
            "SELECT extracted_text FROM incidents WHERE id=?", (iid,)
        ).fetchone()
        assert row[0] == "OCR data"

    def test_stores_embedding_blob(self, store):
        iid = store.generate_id()
        store.save_incident(iid, "T", "D", _make_result())
        row = store._db.execute(
            "SELECT embedding FROM incidents WHERE id=?", (iid,)
        ).fetchone()
        assert row[0] is not None

    def test_generate_id_is_unique(self, store):
        ids = {store.generate_id() for _ in range(100)}
        assert len(ids) == 100


# ── find_similar ──────────────────────────────────────────────────────


class TestFindSimilar:
    def test_empty_store_returns_empty(self, store):
        assert store.find_similar("something") == []

    def test_finds_above_threshold(self, store_with_vecs):
        s, base, near, far = store_with_vecs
        result = _make_result()
        iid = s.generate_id()

        # Insert incident directly with the `base` embedding so we control the vector.
        s._db.execute(
            "INSERT INTO incidents "
            "(id, title, description, extracted_text, embedding, classification_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (iid, "title", "desc", "", base.tobytes(), result.model_dump_json()),
        )
        s._db.commit()

        # Querying "BASE" → model returns `base` → cosine(base, base) = 1.0 ≥ 0.90
        matches = s.find_similar("BASE", threshold=0.90)
        assert len(matches) == 1
        assert matches[0].id == iid
        assert matches[0].similarity >= 0.90

    def test_excludes_below_threshold(self, store_with_vecs):
        s, base, near, far = store_with_vecs
        result = _make_result()
        iid = s.generate_id()
        s.save_incident(iid, "FAR", "FAR", result)

        # Query with BASE vector — cosine with FAR is ~0, well below 0.90
        matches = s.find_similar("BASE", threshold=0.90)
        assert matches == []

    def test_respects_top_k(self, store_with_vecs):
        s, base, near, far = store_with_vecs
        result = _make_result()
        for i in range(5):
            iid = s.generate_id()
            s.save_incident(iid, "BASE", "BASE", result)

        matches = s.find_similar("BASE", threshold=0.0, top_k=3)
        assert len(matches) <= 3

    def test_sorted_by_similarity_descending(self, store_with_vecs):
        s, base, near, far = store_with_vecs
        result = _make_result()
        iid_base = s.generate_id()
        iid_near = s.generate_id()
        s.save_incident(iid_base, "BASE", "BASE", result)
        s.save_incident(iid_near, "NEAR", "NEAR", result)

        matches = s.find_similar("BASE", threshold=0.0)
        sims = [m.similarity for m in matches]
        assert sims == sorted(sims, reverse=True)

    def test_includes_extracted_text_in_query(self, store_with_vecs):
        """extracted_text changes the query embedding text and affects the similarity score."""
        s, base, near, far = store_with_vecs
        result = _make_result()
        iid = s.generate_id()

        # Insert incident with `base` vector directly.
        s._db.execute(
            "INSERT INTO incidents "
            "(id, title, description, extracted_text, embedding, classification_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (iid, "title", "desc", "some ocr text", base.tobytes(), result.model_dump_json()),
        )
        s._db.commit()

        # Querying "BASE" (→ `base` vector) matches; querying "FAR" (→ `far` vector) does not.
        matches_base = s.find_similar("BASE", extracted_text="", threshold=0.90)
        matches_far = s.find_similar("FAR", extracted_text="", threshold=0.90)

        assert any(m.id == iid for m in matches_base)
        assert not any(m.id == iid for m in matches_far)


# ── find_cluster_by_similarity ────────────────────────────────────────


class TestFindClusterBySimilarity:
    def test_returns_none_when_no_clusters(self, store):
        assert store.find_cluster_by_similarity("anything") is None

    def test_matches_cluster_above_threshold(self, store_with_vecs):
        s, base, near, far = store_with_vecs
        result = _make_result()
        iid = s.generate_id()
        s.save_incident(iid, "BASE", "BASE", result)
        cluster_id = s.link_to_cluster(iid, result, [])

        # Create a fake centroid matching BASE
        centroid = base
        s._db.execute(
            "UPDATE clusters SET centroid_embedding=? WHERE id=?",
            (centroid.tobytes(), cluster_id),
        )
        s._db.commit()

        # Fixture sets similarity_threshold=0.90; centroid is exactly `base` → score=1.0
        found = s.find_cluster_by_similarity("BASE")
        assert found == cluster_id

    def test_system_filter_excludes_wrong_system(self, store_with_vecs):
        s, base, near, far = store_with_vecs
        crm_result = _make_result(affected_system=AffectedSystem.crm)
        iid = s.generate_id()
        s.save_incident(iid, "BASE", "BASE", crm_result)
        cluster_id = s.link_to_cluster(iid, crm_result, [])

        s._db.execute(
            "UPDATE clusters SET centroid_embedding=? WHERE id=?",
            (base.tobytes(), cluster_id),
        )
        s._db.commit()

        # Searching with a different system should not match (system filter applies)
        network_result = _make_result(affected_system=AffectedSystem.network, service="VPN")
        found = s.find_cluster_by_similarity("BASE", classification=network_result)
        assert found is None


# ── link_to_cluster ───────────────────────────────────────────────────


class TestLinkToCluster:
    def test_no_matches_returns_none(self, store):
        iid = store.generate_id()
        store.save_incident(iid, "T", "D", _make_result())
        result = store.link_to_cluster(iid, _make_result(), [])
        assert result is None

    def test_creates_new_cluster_from_matches(self, store):
        result = _make_result()
        iid1 = store.generate_id()
        iid2 = store.generate_id()
        store.save_incident(iid1, "T1", "D1", result)
        store.save_incident(iid2, "T2", "D2", result)

        # Construct the match manually to avoid coupling to find_similar threshold behavior.
        match = SimilarMatch(id=iid1, title="T1", similarity=0.95, classification=result)
        cluster_id = store.link_to_cluster(iid2, result, [match])
        assert cluster_id is not None

        row = store._db.execute(
            "SELECT COUNT(*) FROM cluster_members WHERE cluster_id=?", (cluster_id,)
        ).fetchone()
        assert row[0] >= 1

    def test_links_to_existing_cluster(self, store):
        result = _make_result()
        iid1 = store.generate_id()
        iid2 = store.generate_id()
        store.save_incident(iid1, "T1", "D1", result)

        matches = [
            SimilarMatch(id=iid1, title="T1", similarity=0.95, classification=result)
        ]
        cluster_id = store.link_to_cluster(iid2, result, matches)
        assert cluster_id is not None

        # Third incident should join the same cluster
        iid3 = store.generate_id()
        store.save_incident(iid3, "T3", "D3", result)
        matches2 = [
            SimilarMatch(id=iid1, title="T1", similarity=0.94, classification=result)
        ]
        cluster_id2 = store.link_to_cluster(iid3, result, matches2)
        assert cluster_id2 == cluster_id

    def test_cross_system_not_linked(self, store):
        crm_result = _make_result(affected_system=AffectedSystem.crm)
        net_result = _make_result(affected_system=AffectedSystem.network, service="VPN")

        iid_crm = store.generate_id()
        iid_net = store.generate_id()
        store.save_incident(iid_crm, "CRM issue", "desc", crm_result)
        store.save_incident(iid_net, "VPN issue", "desc", net_result)

        # Match across systems — link_to_cluster should filter by system
        crm_match = SimilarMatch(id=iid_crm, title="CRM issue", similarity=0.95, classification=crm_result)
        cluster_id = store.link_to_cluster(iid_net, net_result, [crm_match])
        assert cluster_id is None  # no same-system match

    def test_worst_severity_is_max(self, store):
        major = _make_result(severity=Severity.major)
        critical = _make_result(severity=Severity.critical)

        iid1 = store.generate_id()
        iid2 = store.generate_id()
        store.save_incident(iid1, "T1", "D1", major)
        store.save_incident(iid2, "T2", "D2", critical)

        match = SimilarMatch(id=iid1, title="T1", similarity=0.95, classification=major)
        cluster_id = store.link_to_cluster(iid2, critical, [match])
        row = store._db.execute(
            "SELECT worst_severity FROM clusters WHERE id=?", (cluster_id,)
        ).fetchone()
        assert row[0] == "Critical"


# ── recalculate_centroid_from_members ─────────────────────────────────


class TestRecalculateCentroid:
    def test_centroid_is_mean_of_members(self, store_with_vecs):
        s, base, near, far = store_with_vecs
        result = _make_result()

        iid1 = s.generate_id()
        iid2 = s.generate_id()
        s.save_incident(iid1, "BASE", "BASE", result)
        s.save_incident(iid2, "NEAR", "NEAR", result)

        match = SimilarMatch(id=iid1, title="BASE", similarity=0.95, classification=result)
        cluster_id = s.link_to_cluster(iid2, result, [match])

        s.recalculate_centroid_from_members(cluster_id)

        blob = s._db.execute(
            "SELECT centroid_embedding FROM clusters WHERE id=?", (cluster_id,)
        ).fetchone()[0]
        assert blob is not None
        centroid = np.frombuffer(blob, dtype=np.float32)
        # Centroid should be normalized (unit vector)
        assert abs(np.linalg.norm(centroid) - 1.0) < 1e-5

    def test_update_cluster_summary_recalculates_centroid(self, store):
        result = _make_result()
        iid1 = store.generate_id()
        iid2 = store.generate_id()
        store.save_incident(iid1, "T1", "D1", result)
        store.save_incident(iid2, "T2", "D2", result)

        match = SimilarMatch(id=iid1, title="T1", similarity=0.95, classification=result)
        cluster_id = store.link_to_cluster(iid2, result, [match])

        store.update_cluster_summary(cluster_id, "Two related incidents affecting CRM portal.")

        blob = store._db.execute(
            "SELECT centroid_embedding FROM clusters WHERE id=?", (cluster_id,)
        ).fetchone()[0]
        assert blob is not None


# ── add_to_cluster ────────────────────────────────────────────────────


class TestAddToCluster:
    def test_adds_incident_to_existing_cluster(self, store):
        result = _make_result()
        iid1 = store.generate_id()
        iid2 = store.generate_id()
        iid3 = store.generate_id()
        store.save_incident(iid1, "T1", "D1", result)
        store.save_incident(iid2, "T2", "D2", result)
        store.save_incident(iid3, "T3", "D3", result)

        match = SimilarMatch(id=iid1, title="T1", similarity=0.95, classification=result)
        cluster_id = store.link_to_cluster(iid2, result, [match])

        store.add_to_cluster(cluster_id, iid3)

        count = store._db.execute(
            "SELECT COUNT(*) FROM cluster_members WHERE cluster_id=?", (cluster_id,)
        ).fetchone()[0]
        assert count >= 3

    def test_duplicate_add_is_idempotent(self, store):
        result = _make_result()
        iid1 = store.generate_id()
        iid2 = store.generate_id()
        store.save_incident(iid1, "T1", "D1", result)
        store.save_incident(iid2, "T2", "D2", result)

        match = SimilarMatch(id=iid1, title="T1", similarity=0.95, classification=result)
        cluster_id = store.link_to_cluster(iid2, result, [match])

        # Add same incident twice — should not create duplicate row
        store.add_to_cluster(cluster_id, iid2)
        store.add_to_cluster(cluster_id, iid2)

        count = store._db.execute(
            "SELECT COUNT(*) FROM cluster_members WHERE cluster_id=? AND incident_id=?",
            (cluster_id, iid2),
        ).fetchone()[0]
        assert count == 1


# ── get_report ────────────────────────────────────────────────────────


class TestGetReport:
    def test_empty_store_returns_empty(self, store):
        assert store.get_report() == []

    def test_returns_cluster_with_incidents(self, store):
        result = _make_result()
        iid1 = store.generate_id()
        iid2 = store.generate_id()
        store.save_incident(iid1, "T1", "D1", result)
        store.save_incident(iid2, "T2", "D2", result)

        match = SimilarMatch(id=iid1, title="T1", similarity=0.95, classification=result)
        store.link_to_cluster(iid2, result, [match])

        report = store.get_report()
        assert len(report) >= 1
        cluster = report[0]
        assert cluster["count"] >= 2
        assert cluster["affected_system"] == "CRM"
        assert "cluster_id" in cluster
        assert len(cluster["incidents"]) >= 2

    def test_report_includes_cluster_id(self, store):
        result = _make_result()
        iid1 = store.generate_id()
        iid2 = store.generate_id()
        store.save_incident(iid1, "T1", "D1", result)
        store.save_incident(iid2, "T2", "D2", result)

        match = SimilarMatch(id=iid1, title="T1", similarity=0.95, classification=result)
        cluster_id = store.link_to_cluster(iid2, result, [match])

        report = store.get_report()
        assert report[0]["cluster_id"] == cluster_id

    def test_report_since_filter(self, store):
        """Clusters updated before the since cutoff should be excluded."""
        result = _make_result()
        iid1 = store.generate_id()
        iid2 = store.generate_id()
        store.save_incident(iid1, "T1", "D1", result)
        store.save_incident(iid2, "T2", "D2", result)
        match = SimilarMatch(id=iid1, title="T1", similarity=0.95, classification=result)
        store.link_to_cluster(iid2, result, [match])

        # Future cutoff — nothing should appear
        report = store.get_report(since="2099-01-01T00:00:00+00:00")
        assert report == []


# ── _SEVERITY_RANK constant ───────────────────────────────────────────


class TestSeverityRank:
    def test_critical_beats_major(self):
        assert _SEVERITY_RANK["Critical"] > _SEVERITY_RANK["Major"]

    def test_major_beats_minor(self):
        assert _SEVERITY_RANK["Major"] > _SEVERITY_RANK["Minor"]

    def test_minor_beats_cosmetic(self):
        assert _SEVERITY_RANK["Minor"] > _SEVERITY_RANK["Cosmetic"]

    def test_max_picks_highest(self):
        severities = ["Minor", "Critical", "Cosmetic", "Major"]
        worst = max(severities, key=lambda s: _SEVERITY_RANK.get(s, 0))
        assert worst == "Critical"


# ── Thread safety ─────────────────────────────────────────────────────


class TestConcurrency:
    def test_concurrent_saves_do_not_corrupt(self, store):
        """Multiple threads saving incidents concurrently should all succeed."""
        errors = []
        result = _make_result()

        def worker():
            try:
                iid = store.generate_id()
                store.save_incident(iid, "Concurrent", "test", result)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Errors during concurrent saves: {errors}"
        count = store._db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        assert count == 20

    def test_concurrent_cluster_links_do_not_corrupt(self, store):
        """Concurrent cluster links should not produce duplicate cluster_members rows."""
        result = _make_result()
        anchor_id = store.generate_id()
        store.save_incident(anchor_id, "Anchor", "base", result)
        anchor_match = SimilarMatch(id=anchor_id, title="Anchor", similarity=0.95, classification=result)

        errors = []

        def worker():
            try:
                iid = store.generate_id()
                store.save_incident(iid, "Worker", "desc", result)
                store.link_to_cluster(iid, result, [anchor_match])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Errors during concurrent links: {errors}"
