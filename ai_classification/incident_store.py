"""SQLite-backed incident store with embedding-based similarity search for live deduplication."""

import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from sentence_transformers import SentenceTransformer

from .config import settings
from .models import ClassificationResult

_log = logging.getLogger(__name__)


@dataclass
class SimilarMatch:
    id: str
    title: str
    similarity: float
    classification: ClassificationResult


class IncidentStore:
    """Thread-safe store: embeds incidents, finds similar *active* ones for deduplication."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model: SentenceTransformer | None = None
        self._db: sqlite3.Connection | None = None
        self._ready = False

    # ── Lifecycle ──────────────────────────────────────────────────

    def setup(self) -> None:
        if self._ready:
            return

        try:
            self._model = SentenceTransformer(settings.embedding_model_name, device="cpu")
        except Exception as exc:
            _log.warning("Failed to load embedding model '%s': %s. Similarity search disabled.",
                         settings.embedding_model_name, exc)
            self._model = None

        try:
            self._db = sqlite3.connect(settings.db_path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("""CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                extracted_text TEXT NOT NULL DEFAULT '',
                embedding BLOB,
                classification_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL
            )""")
        except Exception as exc:
            _log.warning("Failed to open SQLite at '%s': %s. Incident persistence disabled.",
                         settings.db_path, exc)
            self._db = None

        self._ready = True

        # Schema migrations for databases predating these columns.
        if self._db is not None:
            for col_sql in [
                "ALTER TABLE incidents ADD COLUMN extracted_text TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE incidents ADD COLUMN status TEXT NOT NULL DEFAULT 'active'",
            ]:
                try:
                    self._db.execute(col_sql)
                except Exception:
                    pass

    def close(self) -> None:
        if self._db:
            self._db.close()

    @property
    def ready(self) -> bool:
        return self._ready

    # ── Embedding ──────────────────────────────────────────────────

    def _embed(self, text: str) -> np.ndarray | None:
        if self._model is None:
            return None
        vec = self._model.encode(text, normalize_embeddings=True)
        return np.asarray(vec, dtype=np.float32)

    @staticmethod
    def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        return float(np.dot(a, b))

    @staticmethod
    def _build_embedding_text(
        title: str, description: str, extracted_text: str = "",
        classification: ClassificationResult | None = None,
    ) -> str:
        """Concatenate title, description, OCR text, and classification fingerprint."""
        parts = [title, description]
        if extracted_text:
            parts.append(f"OCR: {extracted_text}")
        text = " | ".join(p for p in parts if p).strip()
        if classification is not None:
            text += (
                f" | Classified as: {classification.affected_system} / "
                f"{classification.service} / {classification.incident_type} / "
                f"{classification.category}"
            )
        return text

    # ── Similarity search (live deduplication, active incidents only) ──

    def find_similar(
        self, text: str, *,
        extracted_text: str = "",
        threshold: float | None = None,
        top_k: int = 5,
        classification: ClassificationResult | None = None,
    ) -> list[SimilarMatch]:
        if not self._ready or self._db is None or self._model is None:
            return []
        threshold = threshold if threshold is not None else settings.similarity_threshold
        query_vec = self._embed(
            self._build_embedding_text(text, "", extracted_text, classification=classification)
        )
        if query_vec is None:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT id, title, embedding, classification_json FROM incidents "
                "WHERE embedding IS NOT NULL AND status = 'active'"
            ).fetchall()
        results = []
        for row_id, row_title, blob, class_json in rows:
            if blob is None:
                continue
            score = self._cosine_similarity(query_vec, np.frombuffer(blob, dtype=np.float32))
            if score >= threshold:
                try:
                    cls_result = ClassificationResult.model_validate(json.loads(class_json))
                except Exception:
                    continue
                results.append((score, row_id, row_title, cls_result))
        results.sort(key=lambda x: x[0], reverse=True)
        return [
            SimilarMatch(id=rid, title=rtitle, similarity=score, classification=rcls)
            for score, rid, rtitle, rcls in results[:top_k]
        ]

    # ── Persist ────────────────────────────────────────────────────

    def save_incident(
        self, incident_id: str, title: str, description: str,
        classification: ClassificationResult, extracted_text: str = "",
    ) -> None:
        if not self._ready or self._db is None:
            return
        embedding = self._embed(
            self._build_embedding_text(title, description, extracted_text, classification)
        )
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO incidents "
                "(id, title, description, extracted_text, embedding, classification_json, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
                (
                    incident_id, title, description, extracted_text,
                    embedding.tobytes() if embedding is not None else None,
                    classification.model_dump_json(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._db.commit()

    def generate_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def resolve_incident(self, incident_id: str) -> bool:
        """Mark an incident resolved so it no longer surfaces in duplicate checks. Returns False if unknown."""
        if not self._ready or self._db is None:
            return False
        with self._lock:
            cur = self._db.execute(
                "UPDATE incidents SET status = 'resolved' WHERE id = ?",
                (incident_id,),
            )
            self._db.commit()
        return cur.rowcount > 0
