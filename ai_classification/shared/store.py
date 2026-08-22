"""PostgreSQL + pgvector incident store.

Uses pgvector for indexed cosine similarity. Thread-safe via a connection pool.

Pipeline position: 40_store — Postgres/pgvector persistence.

C-3 restructure: the app lifespan and the module-level service wrappers
(get_health / resolve_incident / get_incident / delete_all_incidents /
list_incidents) moved out — lifespan + worker startup live in
ai_classification/app.py, and the api modules call store.<method> directly
(or through thin local wrappers in ai_classification/api/incidents.py).
"""

import logging

from sentence_transformers import SentenceTransformer

from ai_classification.domain.models import ClassificationResult, SimilarMatch
from ai_classification.shared.config import settings
from ai_classification.shared.db import DBBase, VECTOR_DIM
from ai_classification.shared.store_incidents import IncidentsMixin
from ai_classification.shared.store_clusters import ClustersMixin
from ai_classification.shared.store_logs import LogsMixin

# NOTE: `SentenceTransformer` and `settings` must stay module-level names on
# THIS module — DBBase.setup() (shared/db.py) and IncidentsMixin (shared/
# store_incidents.py) resolve them through `ai_classification.shared.store`
# at call time so tests can monkeypatch store_mod.SentenceTransformer /
# store_mod.settings (see tests/shared/test_incident_store.py).

_log = logging.getLogger(__name__)

# Column names for the common incident SELECT (17 cols, 0-indexed).
# Used by _row_to_incident to map DB rows → dicts.
_INCIDENT_COLS: tuple[str, ...] = (
    "id", "title", "description", "extracted_text", "classification_json",
    "status", "created_at", "documents", "assign_group", "assignee", "priority",
    "notes", "discussion_history", "escalation_info", "completion_code",
    "ticket_kind", "classification_status",
)


class IncidentStore(DBBase, IncidentsMixin, ClustersMixin, LogsMixin):
    """PostgreSQL-backed store with pgvector cosine similarity."""


# ── Module-level singleton ──────────────────────────────────────────────

store = IncidentStore()
