"""Webhook receivers — external systems pushing events at us.

SMAX is configured with `POST /api/v1/smax/webhook` as its outbound
notification URL (bearer auth, same INTEGRATION_API_TOKEN as the E1-E9
contract). All SMAX field-name translation lives in
ai_classification/seams/smax_webhook.py (the source adapter) — routes here
stay endpoint-only, per the port.py rule.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from ai_classification.api.auth import require_token
from ai_classification.seams.smax_webhook import handle_smax_event

router = APIRouter(prefix="/api/v1", tags=["webhooks"])


@router.post("/smax/webhook", dependencies=[Depends(require_token)])
def smax_webhook(payload: dict = Body(...)):
    """Receive one SMAX push: a NEW incident OR a status change.

    Dispatch (see seams/smax_webhook.py):
      - unknown source reference → enqueue for async classification → 202
      - known source reference   → status-only update of the existing row → 200
      - unusable payload         → 400 INVALID_PAYLOAD

    The raw SMAX status is stored verbatim (dynamic — any value is
    accepted); only the local active/resolved view is derived.
    """
    status_code, body = handle_smax_event(payload)
    return JSONResponse(status_code=status_code, content=body)
