"""OCR enrichment service — attachment → OCR endpoint → incident record text.

The OCR engine itself is NOT here: it runs as its own service with its own
endpoint (`OCR_SERVICE_URL`, see `ocr/`). This package only talks to it.
"""

from ai_classification.services.ocr.client import (
    OcrOutcome,
    OcrReport,
    attachment_bytes,
    attachment_name,
    ocr_endpoint,
    transcribe,
    transcribe_attachments,
)

__all__ = [
    "OcrOutcome",
    "OcrReport",
    "attachment_bytes",
    "attachment_name",
    "ocr_endpoint",
    "transcribe",
    "transcribe_attachments",
]
