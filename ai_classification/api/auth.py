"""Shared auth dependency for the API surface.

`require_token` guards every endpoint that must not be reachable without
credentials — the E1-E9 integration API (/api/v1/*), plus the dangerous
unauthenticated endpoints that were historically open: POST /reset (deletes
ALL incidents) and /test/llm (spends LLM tokens). /health and /ready stay
exempt by design (k8s probes).

Moved verbatim from api/integration.py so incidents.py and diagnostics.py
can reuse it without importing the integration router (C-3 restructure).
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from ai_classification.services.jobs.integration.schemas import Err, error_body
from ai_classification.shared.config import settings


# ── Auth (E6) — every non-health endpoint ─────────────────────────────

def require_token(authorization: str | None = Header(None)) -> None:
    expected = settings.integration_token
    if not expected:
        raise HTTPException(
            status_code=401,
            detail=error_body(Err.UNAUTHORIZED, "Integration token is not configured on the server"),
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail=error_body(Err.UNAUTHORIZED, "Missing Authorization header (Bearer <token>)"),
        )
    supplied = authorization[len("Bearer "):]
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail=error_body(Err.UNAUTHORIZED, "Invalid token"),
        )
