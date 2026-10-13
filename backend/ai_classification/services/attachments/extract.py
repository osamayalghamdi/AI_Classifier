"""Extraction orchestration: bytes + mime → AttachmentLayers.

Gates on mime and size, renders PDFs with pypdfium2 (capped at
ATTACHMENT_MAX_PDF_PAGES), and — the key rule — never raises for a bad
attachment: it returns a `status="failed"` record instead, so an extraction
problem can never take the ingest path down.

Caching: only *successful* extractions (``status="ok"``) are cached. A
provider timeout, error or bad-JSON response is ``status="failed"`` and is
retried next time. The cache key is sha256 + reader mode + model + prompt
version, so changing the prompt or model never reuses stale layers.
"""

from __future__ import annotations

import hashlib
import logging

from ai_classification.shared.config import settings

from .prompts import ATTACHMENT_PROMPT_VERSION
from .provider import AttachmentReader, build_reader
from .schema import AttachmentLayers

_log = logging.getLogger("attachments")

_IMAGE_PREFIXES = ("image/",)
_PDF_MIME = "application/pdf"

# In-memory for the process lifetime (the DB cache is Phase 2).
_cache: dict[str, AttachmentLayers] = {}


def _cache_key(data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    return (f"{digest}|{settings.attachment_reader_mode}"
            f"|{settings.attachment_vision_model}|{ATTACHMENT_PROMPT_VERSION}")


def _render_pdf(data: bytes, max_pages: int) -> list[bytes]:
    """Render up to `max_pages` pages to PNG bytes. pypdfium2 is a pure wheel
    (bundled pdfium, no system packages) — pinned in pyproject.toml."""
    import io

    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    pages: list[bytes] = []
    for i in range(min(len(pdf), max_pages)):
        bitmap = pdf[i].render(scale=1.5)
        buf = io.BytesIO()
        bitmap.to_pil().save(buf, format="PNG")
        pages.append(buf.getvalue())
    pdf.close()
    return pages


def extract_attachment(data: bytes, mime_type: str, *,
                       reader: AttachmentReader | None = None,
                       max_bytes: int | None = None) -> AttachmentLayers:
    """Reduce one attachment to its layers. Never raises."""
    limit = max_bytes if max_bytes is not None else settings.attachment_max_bytes
    mime = (mime_type or "").lower()

    key = _cache_key(data)
    if key in _cache:
        return _cache[key]

    if not mime.startswith(_IMAGE_PREFIXES) and mime != _PDF_MIME:
        layers = AttachmentLayers(quality="unreadable",
                                  summary_en=f"unsupported file type ({mime or 'unknown'})")
    elif len(data) > limit:
        layers = AttachmentLayers(quality="unreadable",
                                  summary_en="attachment exceeds the size limit")
    else:
        layers = _call_reader(data, mime, reader)

    # Only successful extractions are cached; a failure must be retried next time.
    if layers.status == "ok":
        _cache[key] = layers
    return layers


def _call_reader(data: bytes, mime: str, reader: AttachmentReader | None) -> AttachmentLayers:
    try:
        r = reader or build_reader()
        if mime == _PDF_MIME:
            pages = _render_pdf(data, settings.attachment_max_pdf_pages)
            return r.read(data, mime, pages=pages)
        return r.read(data, mime)
    except NotImplementedError as exc:
        _log.warning("attachment reader unavailable: %s", exc)
        return AttachmentLayers(quality="unreadable", status="failed", summary_en=str(exc))
    except Exception as exc:  # noqa: BLE001 — a bad attachment must not stop the ticket
        _log.warning("attachment extraction failed: %s", exc)
        return AttachmentLayers(quality="unreadable", status="failed",
                                summary_en="extraction failed")
