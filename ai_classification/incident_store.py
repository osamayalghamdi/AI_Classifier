"""SQLite-backed incident store with embedding-based similarity search and clustering."""

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
from .schemas import Severity

_log = logging.getLogger(__name__)

# Highest severity = highest rank; used in max() comparisons.
_SEVERITY_RANK: dict[str, int] = {
    s.value: i for i, s in enumerate(reversed(list(Severity)))
}


@dataclass
class SimilarMatch:
    id: str
    title: str
    similarity: float
    classification: ClassificationResult


class IncidentStore:
    """Thread-safe store: embeds incidents, finds similar ones, manages clusters."""

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
                created_at TEXT NOT NULL
            )""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS clusters (
                id TEXT PRIMARY KEY, summary TEXT NOT NULL DEFAULT '',
                system TEXT NOT NULL, service TEXT NOT NULL,
                worst_severity TEXT NOT NULL,
                centroid_embedding BLOB,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS cluster_members (
                cluster_id TEXT NOT NULL, incident_id TEXT NOT NULL,
                similarity REAL NOT NULL,
                PRIMARY KEY (cluster_id, incident_id),
                FOREIGN KEY (cluster_id) REFERENCES clusters(id),
                FOREIGN KEY (incident_id) REFERENCES incidents(id)
            )""")
        except Exception as exc:
            _log.warning("Failed to open SQLite at '%s': %s. Incident persistence disabled.",
                         settings.db_path, exc)
            self._db = None

        self._ready = True

        # Schema migrations for databases predating these columns.
        if self._db is not None:
            for col_sql in [
                "ALTER TABLE clusters ADD COLUMN centroid_embedding BLOB",
                "ALTER TABLE incidents ADD COLUMN extracted_text TEXT NOT NULL DEFAULT ''",
            ]:
                try:
                    self._db.execute(col_sql)
                except Exception:
                    pass

        # Back-fill centroid embeddings from summaries on first run after migration.
        if self._model is not None and self._db is not None:
            rows = self._db.execute(
                "SELECT id, summary FROM clusters WHERE summary != '' AND centroid_embedding IS NULL"
            ).fetchall()
            for cid, summary in rows:
                vec = self._embed(summary)
                if vec is not None:
                    with self._lock:
                        self._db.execute(
                            "UPDATE clusters SET centroid_embedding = ? WHERE id = ?",
                            (vec.tobytes(), cid),
                        )
            if rows:
                self._db.commit()

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

    # ── Similarity search ──────────────────────────────────────────

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
                "SELECT id, title, embedding, classification_json FROM incidents WHERE embedding IS NOT NULL"
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

    def find_cluster_by_similarity(
        self, text: str, *,
        extracted_text: str = "",
        classification: ClassificationResult | None = None,
    ) -> str | None:
        """Return the best-matching cluster centroid ID, or None (O(clusters))."""
        if not self._ready or self._db is None or self._model is None:
            return None
        query_vec = self._embed(
            self._build_embedding_text(text, "", extracted_text, classification=classification)
        )
        if query_vec is None:
            return None
        threshold = settings.similarity_threshold
        with self._lock:
            rows = self._db.execute(
                "SELECT id, centroid_embedding, system FROM clusters WHERE centroid_embedding IS NOT NULL"
            ).fetchall()
        best: tuple[float, str] | None = None
        for cid, blob, system in rows:
            if classification and system != classification.affected_system:
                continue
            score = self._cosine_similarity(query_vec, np.frombuffer(blob, dtype=np.float32))
            if score >= threshold and (best is None or score > best[0]):
                best = (score, cid)
        return best[1] if best else None

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
                "(id, title, description, extracted_text, embedding, classification_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
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

    # ── Cluster management ─────────────────────────────────────────

    def link_to_cluster(
        self, incident_id: str, classification: ClassificationResult, matches: list,
    ) -> str | None:
        if not self._ready or self._db is None:
            return None
        now = datetime.now(timezone.utc).isoformat()
        same_system = [
            m for m in matches
            if m.classification.affected_system == classification.affected_system
        ]
        match_ids = [m.id for m in same_system]
        if not match_ids:
            return None
        placeholders = ",".join("?" for _ in match_ids)
        existing = self._db.execute(
            f"SELECT DISTINCT cluster_id FROM cluster_members WHERE incident_id IN ({placeholders})",
            match_ids,
        ).fetchall()
        if existing:
            cluster_id = existing[0][0]
        else:
            cluster_id = uuid.uuid4().hex[:12]
            sev = max(
                [classification.severity] + [m.classification.severity for m in same_system],
                key=lambda s: _SEVERITY_RANK.get(str(s), 0),
            )
            with self._lock:
                self._db.execute(
                    "INSERT INTO clusters (id, summary, system, service, worst_severity, created_at, updated_at) "
                    "VALUES (?, '', ?, ?, ?, ?, ?)",
                    (cluster_id, classification.affected_system, classification.service, sev, now, now),
                )
                for m in matches:
                    self._db.execute(
                        "INSERT OR IGNORE INTO cluster_members (cluster_id, incident_id, similarity) VALUES (?, ?, ?)",
                        (cluster_id, m.id, round(m.similarity, 4)),
                    )
                self._db.commit()
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO cluster_members (cluster_id, incident_id, similarity) VALUES (?, ?, 1.0)",
                (cluster_id, incident_id),
            )
            self._db.execute("UPDATE clusters SET updated_at = ? WHERE id = ?", (now, cluster_id))
            self._db.commit()
        return cluster_id

    def add_to_cluster(self, cluster_id: str, incident_id: str) -> None:
        if not self._ready or self._db is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO cluster_members (cluster_id, incident_id, similarity) VALUES (?, ?, 1.0)",
                (cluster_id, incident_id),
            )
            self._db.execute("UPDATE clusters SET updated_at = ? WHERE id = ?", (now, cluster_id))
            self._db.commit()

    def get_cluster_incidents(self, cluster_id: str) -> list[dict]:
        if not self._ready or self._db is None:
            return []
        rows = self._db.execute(
            "SELECT i.title, i.description, i.classification_json "
            "FROM incidents i JOIN cluster_members cm ON cm.incident_id = i.id "
            "WHERE cm.cluster_id = ? ORDER BY i.created_at",
            (cluster_id,),
        ).fetchall()
        return [{"title": r[0], "description": r[1], "classification_json": r[2]} for r in rows]

    def recalculate_centroid_from_members(self, cluster_id: str) -> None:
        """Recompute centroid as the normalized mean of all member embeddings."""
        if not self._ready or self._db is None or self._model is None:
            return
        with self._lock:
            rows = self._db.execute(
                "SELECT i.embedding FROM incidents i "
                "JOIN cluster_members cm ON cm.incident_id = i.id "
                "WHERE cm.cluster_id = ? AND i.embedding IS NOT NULL",
                (cluster_id,),
            ).fetchall()
        if not rows:
            return
        vecs = [np.frombuffer(r[0], dtype=np.float32) for r in rows]
        centroid = np.mean(vecs, axis=0).astype(np.float32)
        norm = np.linalg.norm(centroid)
        if norm > 0:
            centroid = centroid / norm
        with self._lock:
            self._db.execute(
                "UPDATE clusters SET centroid_embedding = ? WHERE id = ?",
                (centroid.tobytes(), cluster_id),
            )
            self._db.commit()

    def update_cluster_summary(self, cluster_id: str, summary: str) -> None:
        if not self._ready or self._db is None:
            return
        with self._lock:
            self._db.execute(
                "UPDATE clusters SET summary = ? WHERE id = ?",
                (summary, cluster_id),
            )
            self._db.commit()
        self.recalculate_centroid_from_members(cluster_id)

    # ── Reporting ──────────────────────────────────────────────────

    def get_report(self, *, since: str | None = None, until: str | None = None) -> list[dict]:
        if not self._ready or self._db is None:
            return []
        clauses = ["1=1"]
        params: list = []
        if since:
            clauses.append("c.updated_at >= ?")
            params.append(since)
        if until:
            clauses.append("c.updated_at < ?")
            params.append(until)
        where = " AND ".join(clauses)
        rows = self._db.execute(f"""
            SELECT c.id, c.summary, c.system, c.service, c.worst_severity,
                   COUNT(cm.incident_id) as cnt
            FROM clusters c JOIN cluster_members cm ON cm.cluster_id = c.id
            WHERE {where} GROUP BY c.id ORDER BY cnt DESC
        """, params).fetchall()
        clusters = []
        for cid, summary, system, service, worst, cnt in rows:
            incidents = self._db.execute("""
                SELECT i.id, i.title, i.classification_json, i.created_at
                FROM incidents i JOIN cluster_members cm ON cm.incident_id = i.id
                WHERE cm.cluster_id = ? ORDER BY i.created_at
            """, (cid,)).fetchall()
            incident_list = []
            for inc_id, inc_title, class_json, created_at in incidents:
                try:
                    sev = json.loads(class_json).get("severity", "Minor")
                except Exception:
                    sev = "Minor"
                incident_list.append({"id": inc_id, "title": inc_title, "severity": sev, "created_at": created_at})
            clusters.append({
                "cluster_id": cid,
                "summary": summary, "affected_system": system, "affected_service": service,
                "count": cnt, "worst_severity": worst, "incidents": incident_list,
            })
        return clusters
