import json

from ai_classification.services.attachments.provider import _MOCK_MARKER, MockReader


def test_marker_branch_parses_self_describing_bytes():
    reader = MockReader()
    desc = {"summary_en": "hello", "quality": "clear", "text": "x"}
    layers = reader.read(_MOCK_MARKER + json.dumps(desc).encode(), "image/png")
    assert layers.summary_en == "hello"
    assert reader.calls == 1


def test_generic_branch_is_deterministic_and_grounded():
    reader = MockReader()
    a = reader.read(b"whatever", "image/png")
    b = reader.read(b"whatever", "image/png")
    assert a == b  # same bytes -> same layers
    # the generated code/id are present in the text, so grounding keeps them
    assert a.signals and a.signals[0].code
    assert a.objects and a.objects[0].id
    assert a.quality in {"clear", "partial", "unreadable"}
