"""Attachment understanding — universal layers for any file.

The reader turns any image/PDF attachment into one `AttachmentLayers` record
(where / objects / signals / text / summary). See CONTRACT.md.
"""

from .schema import AttachmentLayers, AttObject, Signal, Where  # noqa: F401
from .normalize import normalize  # noqa: F401
from .provider import AttachmentReader, OcrThenLLMReader, VisionLLMReader  # noqa: F401
from .extract import extract_attachment  # noqa: F401
