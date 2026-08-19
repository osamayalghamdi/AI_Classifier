"""Taxonomy gaps review API — surfaces classifier v3 OFFERING-GAP hits.

GET /taxonomy-gaps                       list aggregated taxonomy gaps

The v3 pipeline records a gap whenever stage 3 answers NONE_OF_THE_ABOVE:
no taxonomy offering fits the ticket. Each gap aggregates by
(service, suggested_offering) so the review gate can show, next to the
pending proposals, where the taxonomy is missing coverage — e.g.
System/Application - Nusuk Masar Haj -> "Company Evaluation" (x9) — and
an operator can extend the taxonomy to absorb them.

Mirrors the /proposals router style: APIRouter, logging, dict return,
no auth on the review router.
"""
import logging

from fastapi import APIRouter

from ai_classification.shared.store import store

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/taxonomy-gaps", tags=["taxonomy-gaps"])


@router.get("")
def list_taxonomy_gaps():
    _log.info("GET /taxonomy-gaps")
    try:
        gaps = store.list_taxonomy_gaps() or []
    except AttributeError:
        # store.list_taxonomy_gaps lands in a parallel worktree (worker B);
        # the review UI must never 500 while it is missing.
        _log.warning("store.list_taxonomy_gaps unavailable — returning empty gaps")
        return {"gaps": [], "total": 0}
    return {"gaps": gaps, "total": len(gaps)}
