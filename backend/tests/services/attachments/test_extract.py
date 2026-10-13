from ai_classification.services.attachments.extract import extract_attachment
from ai_classification.services.attachments.schema import AttachmentLayers


class _RecordingReader:
    """Records every read call; returns an unreadable marker unless overridden."""

    def __init__(self):
        self.calls = []

    def read(self, data, mime_type, *, pages=None):
        self.calls.append((data, mime_type, pages))
        return AttachmentLayers(quality="clear", summary_en="ok")


class _BoomReader:
    def read(self, data, mime_type, *, pages=None):
        raise RuntimeError("provider down")


def test_non_image_mime_is_unreadable_without_a_provider_call():
    reader = _RecordingReader()
    layers = extract_attachment(b"not an image", "text/plain", reader=reader)
    assert layers.quality == "unreadable"
    assert reader.calls == []


def test_oversize_attachment_is_unreadable_without_a_provider_call():
    reader = _RecordingReader()
    layers = extract_attachment(b"x" * 100, "image/png", reader=reader, max_bytes=10)
    assert layers.quality == "unreadable"
    assert reader.calls == []


def test_same_bytes_are_cached_to_one_call():
    reader = _RecordingReader()
    extract_attachment(b"same", "image/png", reader=reader)
    extract_attachment(b"same", "image/png", reader=reader)
    assert len(reader.calls) == 1


def test_provider_failure_becomes_unreadable_not_an_exception():
    layers = extract_attachment(b"img", "image/png", reader=_BoomReader())
    assert layers.quality == "unreadable"


def test_garbage_pdf_is_unreadable_not_an_exception():
    # pypdfium2 will reject bytes that are not a PDF; the extractor must swallow it.
    layers = extract_attachment(b"not a real pdf", "application/pdf", reader=_RecordingReader())
    assert layers.quality == "unreadable"
