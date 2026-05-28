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
                """)
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS clusters (
                    id            TEXT PRIMARY KEY,
                    summary       TEXT NOT NULL DEFAULT '',
                    system        TEXT NOT NULL,
                    service       TEXT NOT NULL,
                    worst_severity TEXT NOT NULL,
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL
                )
                """
            )
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS cluster_members (
                    cluster_id  TEXT NOT NULL,
                    incident_id TEXT NOT NULL,
                    similarity  REAL NOT NULL,
                    PRIMARY KEY (cluster_id, incident_id),
                    FOREIGN KEY (cluster_id) REFERENCES clusters(id),
                    FOREIGN KEY (incident_id) REFERENCES incidents(id)
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

        # ── Migrate existing records to augmented embeddings ─────────────────
        # Previous versions stored embeddings from raw title+description only.
        # The new scheme folds the classification fingerprint into the embedding
        # text.  Re-embed any record that was stored under the old scheme so
        # that similarity comparisons remain consistent.
        if self._model is not None and self._db is not None:
            self._migrate_embeddings()

    def _migrate_embeddings(self) -> None:
        """One-time migration: re-embed records that still use raw-text embeddings.

        Detection: re-embed a record and compare — if the stored embedding
        differs from what we'd produce now, update it.
        """
        rows = self._db.execute(
            "SELECT id, title, description, classification_json, embedding "
            "FROM incidents"
        ).fetchall()

        for row_id, title, description, class_json, blob in rows:
            try:
                class_data = json.loads(class_json)
                cls_result = ClassificationResult.model_validate(class_data)
            except Exception:
                continue  # skip corrupted records, leave as-is

            expected_text = self._build_embedding_text(title, description, cls_result)
            expected_vec = self._embed(expected_text)
            if expected_vec is None:
                continue

            # If the stored blob is None or the length doesn't match, re-embed.
            expected_bytes = expected_vec.tobytes()
            if blob is None or len(blob) != len(expected_bytes):
                with self._lock:
                    self._db.execute(
                        "UPDATE incidents SET embedding = ? WHERE id = ?",
                        (expected_bytes, row_id),
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

    @staticmethod
    def _build_embedding_text(title: str, description: str, classification: "ClassificationResult | None" = None) -> str:
        """Build text to embed — includes the classification fingerprint when available.

        Folding the LLM's structured classification into the embedding string
        anchors semantically similar incidents together even when the raw wording
        differs (e.g. "Stripe errors" → "Payment gateway down" both map to
        ``Payment Gateway / Checkout / Degradation / Performance``).
        """
        text = f"{title} {description}".strip()
        if classification is not None:
            text = (
                f"{text} | Classified as: "
                f"{classification.affected_system} / "
                f"{classification.service} / "
                f"{classification.incident_type} / "
                f"{classification.category}"
            )
        return text

    def find_similar(
        self,
        text: str,
        *,
        threshold: float | None = None,
        top_k: int = 5,
        classification: "ClassificationResult | None" = None,
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
        classification:
            The current incident's classification result — when provided,
            the fingerprint is folded into the query embedding so incidents
            classified into the same system/service/type/category cluster
            closer together regardless of wording differences in the title/description.

        Returns
        -------
        list[SimilarMatch]
            Matches sorted by similarity descending — closest first.
        """
        if not self._ready or self._db is None or self._model is None:
            return []

        threshold = threshold if threshold is not None else settings.similarity_threshold
        query_text = self._build_embedding_text(text, "", classification)
        query_vec = self._embed(query_text)
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

        The embedding text includes the classification fingerprint so that
        future similarity searches are anchored on structured fields
        (affected_system, service, incident_type, category) in addition to
        raw wording — this makes "Stripe errors" match "Payment gateway down"
        when both share the same classification fingerprint.

        Thread-safe — uses an internal lock for the write.
        """
        if not self._ready or self._db is None:
            return

        text = self._build_embedding_text(title, description, classification)
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

    # ── Reporting (cluster management) ────────────────────────────

    def link_to_cluster(
        self,
        incident_id: str,
        title: str,
        description: str,
        classification: ClassificationResult,
        matches: list,
    ) -> str | None:
        """Find or create a cluster for this incident.

        Only matches that share the same ``affected_system`` are considered
        for clustering — this prevents cross-system chain-linking (e.g. a
        VPN incident getting pulled into a Payment Gateway cluster).

        If the incident has same-system matches above threshold, it joins an
        existing cluster (or starts a new one with those matches). The
        cluster summary is then generated by the LLM once on first merge.

        Returns the cluster ID, or None if no clustering is possible.
        """
        if not self._ready or self._db is None:
            return None

        now = datetime.now(timezone.utc).isoformat()

        # Only consider matches from the same affected system
        same_system = [
            m for m in matches
            if m.classification.affected_system == classification.affected_system
        ]
        match_ids = [m.id for m in same_system]
        if not match_ids:
            return None

        # Check if any matched incident already belongs to a cluster
        placeholders = ",".join("?" for _ in match_ids)
        existing = self._db.execute(
            f"SELECT DISTINCT cluster_id FROM cluster_members "
            f"WHERE incident_id IN ({placeholders})",
            match_ids,
        ).fetchall()

        if existing:
            cluster_id = existing[0][0]
        else:
            cluster_id = uuid.uuid4().hex[:12]
            sev = max(
                [classification.severity] + [m.classification.severity for m in same_system],
                key=lambda s: {"Critical": 4, "Major": 3, "Minor": 2, "Cosmetic": 1}.get(s, 0),
            )
            with self._lock:
                self._db.execute(
                    "INSERT INTO clusters (id, summary, system, service, worst_severity, created_at, updated_at) "
                    "VALUES (?, '', ?, ?, ?, ?, ?)",
                    (cluster_id, classification.affected_system, classification.service, sev, now, now),
                )
                # Add all matched incidents to the new cluster
                for m in matches:
                    self._db.execute(
                        "INSERT OR IGNORE INTO cluster_members (cluster_id, incident_id, similarity) VALUES (?, ?, ?)",
                        (cluster_id, m.id, round(m.similarity, 4)),
                    )

        # Add this incident to the cluster
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO cluster_members (cluster_id, incident_id, similarity) VALUES (?, ?, 1.0)",
                (cluster_id, incident_id),
            )
            self._db.execute(
                "UPDATE clusters SET updated_at = ? WHERE id = ?",
                (now, cluster_id),
            )
        return cluster_id

    def update_cluster_summary(self, cluster_id: str, summary: str) -> None:
        """Store the LLM-generated summary for a cluster."""
        if not self._ready or self._db is None:
            return
        with self._lock:
            self._db.execute(
                "UPDATE clusters SET summary = ? WHERE id = ?",
                (summary, cluster_id),
            )
            self._db.commit()

    def get_report(self, *, since: str | None = None, until: str | None = None) -> list[dict]:
        """Fetch clusters active in a time window, with their incidents.

        Each cluster dict includes summary, system, service, worst_severity,
        count, and an incidents list (id, title, severity, created_at).

        Returns clusters sorted by count descending — most frequent first.
        """
        if not self._ready or self._db is None:
            return []

        clauses = ["1=1"]
        params: list[str] = []
        if since:
            clauses.append("c.updated_at >= ?")
            params.append(since)
        if until:
            clauses.append("c.updated_at < ?")
            params.append(until)

        where = " AND ".join(clauses)
        rows = self._db.execute(
            f"""
            SELECT c.id, c.summary, c.system, c.service, c.worst_severity,
                   COUNT(cm.incident_id) as cnt
            FROM clusters c
            JOIN cluster_members cm ON cm.cluster_id = c.id
            WHERE {where}
            GROUP BY c.id
            ORDER BY cnt DESC
            """,
            params,
        ).fetchall()

        clusters = []
        for row in rows:
            cid, summary, system, service, worst, cnt = row
            incidents = self._db.execute(
                """
                SELECT i.id, i.title, i.classification_json, i.created_at
                FROM incidents i
                JOIN cluster_members cm ON cm.incident_id = i.id
                WHERE cm.cluster_id = ?
                ORDER BY i.created_at
                """,
                (cid,),
            ).fetchall()
            incident_list = []
            for inc_id, inc_title, class_json, created_at in incidents:
                try:
                    sev = json.loads(class_json).get("severity", "Minor")
                except Exception:
                    sev = "Minor"
                incident_list.append({
                    "id": inc_id,
                    "title": inc_title,
                    "severity": sev,
                    "created_at": created_at,
                })
            clusters.append({
                "summary": summary,
                "affected_system": system,
                "affected_service": service,
                "count": cnt,
                "worst_severity": worst,
                "incidents": incident_list,
            })
        return clusters
