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

import logging

from fastapi import Header, HTTPException

from ai_classification.services.jobs.integration.schemas import Err, error_body
from ai_classification.shared.config import settings

_log = logging.getLogger(__name__)
# The fallback is loud and once per process: an operator reading the logs
# should see that privilege separation is not in effect yet.
_warned_admin_fallback = False


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
    # Compare bytes: compare_digest raises TypeError on non-ASCII str,
    # which would turn a garbage header into a 500 instead of a 401.
    if not secrets.compare_digest(supplied.encode("utf-8", "surrogateescape"),
                                  expected.encode("utf-8")):
        raise HTTPException(
            status_code=401,
            detail=error_body(Err.UNAUTHORIZED, "Invalid token"),
        )


# ── Admin console credential ──────────────────────────────────────────

def _admin_expected() -> str:
    """The credential /admin/* accepts: ADMIN_TOKEN, else the integration
    token (backward compatible — with a one-time warning)."""
    global _warned_admin_fallback
    if settings.admin_token:
        return settings.admin_token
    if not _warned_admin_fallback:
        _warned_admin_fallback = True
        _log.warning(
            "ADMIN_TOKEN is not set — /admin/* falls back to INTEGRATION_API_TOKEN. "
            "Anyone holding the SMAX integration credential can write .env, reset the "
            "database and run tests in-container. Set ADMIN_TOKEN to separate them."
        )
    return settings.integration_token


def require_admin_token(authorization: str | None = Header(None),
                        x_admin_token: str | None = Header(None)) -> None:
    """Guard the admin console.

    Accepts either header so the console can carry a DIFFERENT credential
    from the dashboard's:
      * Authorization: Bearer <ADMIN_TOKEN>      (curl, scripts)
      * X-Admin-Token: <ADMIN_TOKEN>             (admin.html, keeps the
                                                  dashboard's Bearer token
                                                  intact in the same browser)
    """
    expected = _admin_expected()
    if not expected:
        raise HTTPException(
            status_code=401,
            detail=error_body(Err.UNAUTHORIZED, "No admin token is configured on the server"),
        )
    supplied = ""
    if x_admin_token:
        supplied = x_admin_token
    elif authorization and authorization.startswith("Bearer "):
        supplied = authorization[len("Bearer "):]
    else:
        raise HTTPException(
            status_code=401,
            detail=error_body(Err.UNAUTHORIZED,
                              "Missing admin credential (X-Admin-Token or Bearer <token>)"),
        )
    if not secrets.compare_digest(supplied.encode("utf-8", "surrogateescape"),
                                  expected.encode("utf-8")):
        raise HTTPException(
            status_code=401,
            detail=error_body(Err.UNAUTHORIZED, "Invalid admin token"),
        )
