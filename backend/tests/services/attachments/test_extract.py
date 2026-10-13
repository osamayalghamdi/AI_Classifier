import pytest

from ai_classification.services.attachments import extract as extract_mod
from ai_classification.services.attachments.extract import extract_attachment
from ai_classification.services.attachments.schema import AttachmentLayers


@pytest.fixture(autouse=True)
def _clear_cache():
    extract_mod._cache.clear()
    yield
    extract_mod._cache.clear()


class _RecordingReader:
    def __init__(self):
        self.calls = []

    def read(self, data, mime_type, *, pages=None):
        self.calls.append((data, mime_type, pages))
        return AttachmentLayers(quality="clear", summary_en="ok")


class _BoomReader:
    """Always raises — simulates a provider timeout/error."""

    def __init__(self):
        self.calls = 0

    def read(self, data, mime_type, *, pages=None):
        self.calls += 1
        raise RuntimeError("provider down")


class _UnreadableReader:
    """Returns a GENUINE unreadable record (the file itself is unreadable)."""

    def __init__(self):
        self.calls = 0

    def read(self, data, mime_type, *, pages=None):
        self.calls += 1
        return AttachmentLayers(quality="unreadable", summary_en="too dark")


def test_non_image_mime_is_unreadable_without_a_provider_call():
    reader = _RecordingReader()
    layers = extract_attachment(b"not an image", "text/plain", reader=reader)
    assert layers.quality == "unreadable"
    assert layers.status == "ok"  # terminal, deterministic — not a transient failure
    assert reader.calls == []


def test_oversize_attachment_is_unreadable_without_a_provider_call():
    reader = _RecordingReader()
    layers = extract_attachment(b"x" * 100, "image/png", reader=reader, max_bytes=10)
    assert layers.quality == "unreadable"
    assert layers.status == "ok"
    assert reader.calls == []


def test_successful_extraction_is_cached():
    reader = _RecordingReader()
    extract_attachment(b"same", "image/png", reader=reader)
    extract_attachment(b"same", "image/png", reader=reader)
    assert len(reader.calls) == 1


def test_genuine_unreadable_is_cached():
    # quality=unreadable from the model is a SUCCESS (the file is unreadable),
    # so it is cached — it is not a provider failure.
    reader = _UnreadableReader()
    extract_attachment(b"dark", "image/png", reader=reader)
    extract_attachment(b"dark", "image/png", reader=reader)
    assert reader.calls == 1


def test_provider_failure_is_not_cached_and_is_retried():
    reader = _BoomReader()
    first = extract_attachment(b"img", "image/png", reader=reader)
    second = extract_attachment(b"img", "image/png", reader=reader)
    assert first.status == "failed"
    assert second.status == "failed"
    assert reader.calls == 2  # the second call hit the provider again


def test_cache_key_invalidates_on_model_change(monkeypatch):
    from types import SimpleNamespace
    reader = _RecordingReader()
    monkeypatch.setattr(extract_mod, "settings", SimpleNamespace(
        attachment_reader_mode="vision_llm",
        attachment_vision_model="model-a",
        attachment_max_bytes=10485760,
    ))
    extract_attachment(b"x", "image/png", reader=reader)
    monkeypatch.setattr(extract_mod, "settings", SimpleNamespace(
        attachment_reader_mode="vision_llm",
        attachment_vision_model="model-b",
        attachment_max_bytes=10485760,
    ))
    extract_attachment(b"x", "image/png", reader=reader)
    assert len(reader.calls) == 2


def test_cache_key_invalidates_on_prompt_version_change(monkeypatch):
    reader = _RecordingReader()
    monkeypatch.setattr(extract_mod, "ATTACHMENT_PROMPT_VERSION", "v1")
    extract_attachment(b"y", "image/png", reader=reader)
    monkeypatch.setattr(extract_mod, "ATTACHMENT_PROMPT_VERSION", "v2")
    extract_attachment(b"y", "image/png", reader=reader)
    assert len(reader.calls) == 2


def test_garbage_pdf_is_unreadable_not_an_exception():
    layers = extract_attachment(b"not a real pdf", "application/pdf", reader=_RecordingReader())
    assert layers.quality == "unreadable"
    assert layers.status == "failed"  # render failed -> transient failure, retried
