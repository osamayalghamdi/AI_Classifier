"""Offering-key helpers shared with live clustering (persistent.py).

Only the helpers the LIVE path consumes live here: the offering key
extraction (first segment before the last dot), the OFFERING-000 sentinel,
and the pure-text embedding. The dormant sub-offering ENGINE (exemplar
matching, pool feed, backfill) was quarantined to legacy/suboffering_engine/
when the v2 persistent-clustering path superseded it — see that folder's
README for resurrection.

Frozen: thresholds and prompt values are NOT changed here.
"""
import logging

import numpy as np

from ai_classification.shared.store import store

_log = logging.getLogger(__name__)

OFFERING_000 = "OFFERING-000"


def offering_of(service: str | None) -> str | None:
    """First segment of the service string (before the LAST '.').

    Service values look like "System/Application - Nusuk Masar Haj.Bill
    Generation" — the offering is everything before the last dot. Splitting
    on the FIRST dot is wrong for versioned systems like "7.1 Invoicing and
    Billing - Nusuk Masar Haj.Bill Generation" (would yield "7")."""
    svc = (service or "").strip()
    if "." not in svc:
        return None
    return svc.rsplit(".", 1)[0].strip()


def embed_pure(title: str, description: str) -> np.ndarray | None:
    """Frozen embedding: bge-m3 on pure title + '\\n' + description, normalized."""
    if store._model is None:
        return None
    vec = store._model.encode(f"{title}\n{description}", normalize_embeddings=True)
    return np.asarray(vec, dtype=np.float32)
