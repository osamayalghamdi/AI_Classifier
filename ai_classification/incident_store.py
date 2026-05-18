"""SQLite-backed incident store with semantic similarity search.

Stores every classified incident along with its text embedding so
incoming incidents can be matched against past ones by meaning
rather than exact string matching.

Uses ``sentence-transformers`` for embeddings and cosine similarity
for comparison. Fully local — no network calls during inference.
"""

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from sentence_transformers import SentenceTransformer

from .config import settings
from .models import ClassificationResult


# ── Public data class ──────────────────────────────────────────────────


@dataclass
class IncidentRecord:
    """A single row from the incidents table."""
    id: str
    title: str
    description: str
    classification: ClassificationResult
    created_at: str


@dataclass
class SimilarMatch:
    """One past incident that scored above the similarity threshold."""
    id: str
    title: str
    similarity: float
    classification: ClassificationResult


# ── Store ──────────────────────────────────────────────────────────────


class IncidentStore:
    """Thread-safe store that embeds text and finds similar past incidents.

    Usage
    -----
    >>> store = IncidentStore()
    >>> store.setup()
    >>> matches = store.find_similar("checkout slow for EU users")
    >>> store.save_incident("…", "Checkout slow", "desc", ClassificationResult(...))
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model: SentenceTransformer | None = None
        self._db: sqlite3.Connection | None = None
        self._ready = False
        self._dim = 384  # all-MiniLM-L6-v2 output dimension

    # ── Lifecycle ──────────────────────────────────────────────────

    def setup(self) -> None:
        """Initialise the embedding model and SQLite database.

        Safe to call multiple times — subsequent calls are a no-op.
        On failure (e.g. disk full, model won't load), sets ``ready``
        to False so the caller can degrade gracefully.
        """
        if self._ready:
            return

        # Embedding model
        try:
            self._model = SentenceTransformer(
                settings.embedding_model_name,
                device="cpu",
            )
            self._dim = self._model.get_sentence_embedding_dimension()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "Failed to load embedding model '%s': %s. "
                "Similarity search disabled.",
                settings.embedding_model_name, exc,
            )
            self._model = None

        # SQLite
        try:
            self._db = sqlite3.connect(
                settings.db_path,
                check_same_thread=False,   # we use our own lock
            )
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    id            TEXT PRIMARY KEY,
                    title         TEXT NOT NULL,
                    description   TEXT NOT NULL DEFAULT '',
                    embedding     BLOB,              -- numpy float32 raw bytes
                    classification_json TEXT NOT NULL,
                    created_at    TEXT NOT NULL
                )
                """
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "Failed to open SQLite at '%s': %s. "
                "Incident persistence disabled.",
                settings.db_path, exc,
            )
            self._db = None

        self._ready = True

    def close(self) -> None:
        if self._db:
            self._db.close()

    @property
    def ready(self) -> bool:
        return self._ready

    # ── Embedding ──────────────────────────────────────────────────

    def _embed(self, text: str) -> np.ndarray | None:
        """Return a normalised float32 embedding vector or None."""
        if self._model is None:
            return None
        vec = self._model.encode(text, normalize_embeddings=True)
        return np.asarray(vec, dtype=np.float32)

    @staticmethod
    def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        """Cosine similarity between two normalised vectors.

        Since both are unit vectors, this is just the dot product.
        """
        return float(np.dot(a, b))

    # ── Public API ─────────────────────────────────────────────────

    def find_similar(
        self,
        text: str,
        *,
        threshold: float | None = None,
        top_k: int = 5,
    ) -> list[SimilarMatch]:
        """Search past incidents semantically similar to *text*.

        Parameters
        ----------
        text:
            The text to compare (typically title + description).
        threshold:
            Minimum similarity score (0‑1). Falls back to
            ``settings.similarity_threshold`` when not provided.
        top_k:
            Maximum number of matches to return.

        Returns
        -------
        list[SimilarMatch]
            Matches sorted by similarity descending — closest first.
        """
        if not self._ready or self._db is None or self._model is None:
            return []

        threshold = threshold if threshold is not None else settings.similarity_threshold
        query_vec = self._embed(text)
        if query_vec is None:
            return []

        with self._lock:
            rows = self._db.execute(
                "SELECT id, title, embedding, classification_json "
                "FROM incidents WHERE embedding IS NOT NULL"
            ).fetchall()

        results: list[tuple[float, str, str, ClassificationResult]] = []
        for row_id, row_title, blob, class_json in rows:
            if blob is None:
                continue
            stored_vec = np.frombuffer(blob, dtype=np.float32)
            score = self._cosine_similarity(query_vec, stored_vec)
            if score >= threshold:
                try:
                    class_data = json.loads(class_json)
                    cls_result = ClassificationResult.model_validate(class_data)
                except Exception:
                    continue  # skip corrupted records
                results.append((score, row_id, row_title, cls_result))

        results.sort(key=lambda x: x[0], reverse=True)
        return [
            SimilarMatch(id=rid, title=rtitle, similarity=score, classification=rcls)
            for score, rid, rtitle, rcls in results[:top_k]
        ]

    def save_incident(
        self,
        incident_id: str,
        title: str,
        description: str,
        classification: ClassificationResult,
    ) -> None:
        """Persist a classified incident with its embedding.

        Thread-safe — uses an internal lock for the write.
        """
        if not self._ready or self._db is None:
            return

        text = f"{title} {description}"
        embedding = self._embed(text)

        with self._lock:
            self._db.execute(
                """
                INSERT OR REPLACE INTO incidents
                    (id, title, description, embedding, classification_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    title,
                    description,
                    embedding.tobytes() if embedding is not None else None,
                    classification.model_dump_json(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._db.commit()

    def generate_id(self) -> str:
        """Returns a short unique ID for a new incident."""
        return uuid.uuid4().hex[:12]
