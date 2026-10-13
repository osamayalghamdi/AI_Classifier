"""Attachment understanding — universal layers for any file.

The reader turns any image/PDF attachment into one `AttachmentLayers` record
(where / objects / signals / text / summary). See CONTRACT.md.
"""

from .schema import AttachmentLayers, AttObject, Signal, Where  # noqa: F401
from .normalize import normalize, normalize_page  # noqa: F401
from .masking import mask_personal_data  # noqa: F401
from .provider import (  # noqa: F401
    AttachmentReader,
    MockReader,
    OcrThenLLMReader,
    VisionLLMReader,
)
from .extract import extract_attachment  # noqa: F401
