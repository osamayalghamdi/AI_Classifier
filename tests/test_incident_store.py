"""Tests for IncidentStore — PostgreSQL + pgvector embeddings and live-duplicate
similarity search.

Runs against a real Postgres database (see conftest.py) rather than mocking
psycopg2 — catches real SQL/pgvector integration bugs a mock would miss.
"""

import threading
from dataclasses import replace

import numpy as np
import pytest

from ai_classification.core.store import IncidentStore, VECTOR_DIM
from ai_classification.domain.models import ClassificationResult
from ai_classification.domain.taxonomy import (
    AffectedSystem, IncidentType, Severity, Urgency, Category,
)
from ai_classification.config import settings as base_settings

from .conftest import TEST_PG_DATABASE


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
        canonical_statement="Test incident.",
    )
    defaults.update(overrides)
    return ClassificationResult(**defaults)


class FixedVecModel:
    """Mock embedding model — returns vectors from a predefined map, random for unknowns.

    Vectors are VECTOR_DIM-wide to match the real `vector(VECTOR_DIM)` column.
    """

    def __init__(self, vec_map: dict[str, np.ndarray] | None = None):
        self._map = vec_map or {}

    def encode(self, text: str, normalize_embeddings: bool = True) -> np.ndarray:
        if text in self._map:
            v = self._map[text].copy()
        else:
            rng = np.random.RandomState(abs(hash(text)) % (2 ** 31))
            v = rng.randn(VECTOR_DIM).astype(np.float32)
        if normalize_embeddings:
            norm = np.linalg.norm(v)
            if norm > 0:
                v = v / norm
        return v.astype(np.float32)


def _pad(vec: np.ndarray) -> np.ndarray:
    """Embed a short test vector into a full VECTOR_DIM-wide vector (signal in the
    first dims, zero elsewhere) so it fits the real `vector(VECTOR_DIM)` column."""
    full = np.zeros(VECTOR_DIM, dtype=np.float32)
    full[: len(vec)] = vec
    return full


def _truncate(s: IncidentStore) -> None:
    conn = s._getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE incidents")
        conn.commit()
    finally:
        s._putconn(conn)


def _insert_raw(
    s: IncidentStore, iid: str, title: str, description: str,
    extracted_text: str, embedding: np.ndarray, classification: ClassificationResult,
    status: str = "active",
) -> None:
    """Insert a row directly, bypassing save_incident's embedding logic, so a
    test can pin the exact stored vector for deterministic similarity checks."""
    conn = s._getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO incidents "
                "(id, title, description, extracted_text, embedding, classification_json, status, created_at) "
                "VALUES (%s, %s, %s, %s, %s::vector, %s, %s, NOW())",
                (iid, title, description, extracted_text, embedding.tolist(),
                 classification.model_dump_json(), status),
            )
        conn.commit()
    finally:
        s._putconn(conn)


def _get_embedding(s: IncidentStore, iid: str):
    conn = s._getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT embedding FROM incidents WHERE id = %s", (iid,))
            row = cur.fetchone()
    finally:
        s._putconn(conn)
    return row[0] if row else None


def _make_store(monkeypatch, model, threshold=None):
    """Build an IncidentStore against the real test Postgres database."""
    import ai_classification.core.store as store_mod

    monkeypatch.setattr(store_mod, "SentenceTransformer", lambda *a, **_: model)

    overrides = {"pg_database": TEST_PG_DATABASE}
    if threshold is not None:
        overrides["similarity_threshold"] = threshold
    test_settings = replace(base_settings, **overrides)
    monkeypatch.setattr(store_mod, "settings", test_settings)

    s = IncidentStore()
    s.setup()
    _truncate(s)
    return s


@pytest.fixture
def store(monkeypatch):
    """IncidentStore with mocked embeddings, against a truncated test database."""
    s = _make_store(monkeypatch, FixedVecModel())
    yield s
    s.close()


@pytest.fixture
def store_with_vecs(monkeypatch):
    """Store where known keys map to controlled unit vectors for similarity tests."""
    # Two nearly-identical vectors (dot ≈ 0.99) and one orthogonal vector.
    base = _pad(np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32))
    near = _pad(np.array([0.995, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32))
    near /= np.linalg.norm(near)
    far = _pad(np.array([0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32))

    vec_map: dict[str, np.ndarray] = {"BASE": base, "NEAR": near, "FAR": far}

    s = _make_store(monkeypatch, FixedVecModel(vec_map), threshold=0.90)
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
        assert store.get_incident(iid) is not None

    def test_defaults_to_active_status(self, store):
        iid = store.generate_id()
        store.save_incident(iid, "Title", "Desc", _make_result())
        assert store.get_incident(iid)["status"] == "active"

    def test_stores_extracted_text(self, store):
        iid = store.generate_id()
        result = _make_result()
        store.save_incident(iid, "Title", "Desc", result, extracted_text="OCR data")
        assert store.get_incident(iid)["extracted_text"] == "OCR data"

    def test_stores_embedding_blob(self, store):
        iid = store.generate_id()
        store.save_incident(iid, "T", "D", _make_result())
        assert _get_embedding(store, iid) is not None

    def test_generate_id_is_unique(self, store):
        ids = {store.generate_id() for _ in range(100)}
        assert len(ids) == 100


# ── find_similar (live deduplication) ──────────────────────────────────


class TestFindSimilar:
    def test_empty_store_returns_empty(self, store):
        assert store.find_similar("something") == []

    def test_finds_above_threshold(self, store_with_vecs):
        s, base, near, far = store_with_vecs
        result = _make_result()
        iid = s.generate_id()

        # Insert incident directly with the `base` embedding so we control the vector.
        _insert_raw(s, iid, "title", "desc", "", base, result)

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
        _insert_raw(s, iid, "title", "desc", "some ocr text", base, result)

        # Querying "BASE" (→ `base` vector) matches; querying "FAR" (→ `far` vector) does not.
        matches_base = s.find_similar("BASE", extracted_text="", threshold=0.90)
        matches_far = s.find_similar("FAR", extracted_text="", threshold=0.90)

        assert any(m.id == iid for m in matches_base)
        assert not any(m.id == iid for m in matches_far)

    def test_resolved_incident_excluded_from_matches(self, store_with_vecs):
        """A duplicate that's already resolved shouldn't keep flagging new submissions."""
        s, base, near, far = store_with_vecs
        result = _make_result()
        iid = s.generate_id()

        # Insert directly with the `base` embedding for a deterministic score
        # (save_incident mixes the classification fingerprint into the embedded
        # text, which no longer matches a fixed key in FixedVecModel's vec_map).
        _insert_raw(s, iid, "title", "desc", "", base, result)

        matches_before = s.find_similar("BASE", threshold=0.90)
        assert any(m.id == iid for m in matches_before)

        assert s.resolve_incident(iid) is True

        matches_after = s.find_similar("BASE", threshold=0.90)
        assert not any(m.id == iid for m in matches_after)


# ── resolve_incident ─────────────────────────────────────────────────


class TestResolveIncident:
    def test_resolving_known_incident_returns_true(self, store):
        iid = store.generate_id()
        store.save_incident(iid, "T", "D", _make_result())
        assert store.resolve_incident(iid) is True
        assert store.get_incident(iid)["status"] == "resolved"

    def test_resolving_unknown_incident_returns_false(self, store):
        assert store.resolve_incident("does-not-exist") is False

    def test_resolving_twice_is_idempotent(self, store):
        iid = store.generate_id()
        store.save_incident(iid, "T", "D", _make_result())
        assert store.resolve_incident(iid) is True
        assert store.resolve_incident(iid) is True


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
        assert len(store.list_incidents()) == 20
