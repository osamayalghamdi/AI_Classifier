"""Extraction orchestration: bytes + mime → AttachmentLayers.

Gates on mime and size, renders PDFs with pypdfium2 (capped at
ATTACHMENT_MAX_PDF_PAGES), caches by sha256 for the process lifetime (the DB
cache is Phase 2), and — the key rule — never raises for a bad attachment: it
returns an `unreadable` record instead, so an extraction problem can never take
the ingest path down.
"""

from __future__ import annotations

import hashlib
import logging

from ai_classification.shared.config import settings

from .provider import AttachmentReader, build_reader
from .schema import AttachmentLayers

_log = logging.getLogger("attachments")

_IMAGE_PREFIXES = ("image/",)
_PDF_MIME = "application/pdf"

# Same bytes → same layers. In-memory for the run (the DB cache is Phase 2).
_cache: dict[str, AttachmentLayers] = {}


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

    digest = hashlib.sha256(data).hexdigest()
    if digest in _cache:
        return _cache[digest]

    if not mime.startswith(_IMAGE_PREFIXES) and mime != _PDF_MIME:
        layers = AttachmentLayers(quality="unreadable",
                                  summary_en=f"unsupported file type ({mime or 'unknown'})")
    elif len(data) > limit:
        layers = AttachmentLayers(quality="unreadable",
                                  summary_en="attachment exceeds the size limit")
    else:
        layers = _call_reader(data, mime, reader)
    _cache[digest] = layers
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
        return AttachmentLayers(quality="unreadable", summary_en=str(exc))
    except Exception as exc:  # noqa: BLE001 — a bad attachment must not stop the ticket
        _log.warning("attachment extraction failed: %s", exc)
        return AttachmentLayers(quality="unreadable", summary_en="extraction failed")
