import logging

from ai_classification.services.attachments.schema import (
    AttachmentLayers, AttObject, Signal, Where,
)


def _layers(**kw) -> AttachmentLayers:
    return AttachmentLayers.model_validate(kw)


def test_empty_lists_and_optional_fields_are_accepted():
    layers = _layers()
    assert layers.objects == [] and layers.signals == []
    assert layers.where == Where()
    assert layers.quality == "clear"


def test_unknown_extra_keys_are_dropped_not_fatal():
    layers = _layers(where={"portal": "Nusuk", "made_up_field": 123},
                     summary_en="x", bogus_top_level="y")
    assert layers.where.portal == "Nusuk"
    assert not hasattr(layers.where, "made_up_field")


def test_strings_are_trimmed_and_length_capped():
    layers = _layers(text="  " + "a" * 9000,
                     summary_en="s" * 500,
                     where={"page": "p" * 500})
    assert len(layers.text) == 8000
    assert len(layers.summary_en) == 300
    assert len(layers.where.page) == 300


def test_unreadable_keeps_only_summary_and_quality():
    layers = _layers(quality="unreadable", summary_en="too dark to read",
                     text="should be dropped", signals=[{"type": "error", "code": "E1"}],
                     objects=[{"type": "invoice", "id": "INV-1"}],
                     where={"page": "Wallet"})
    assert layers.text == "" and layers.signals == [] and layers.objects == []
    assert layers.where == Where()
    assert layers.summary_en == "too dark to read"


def test_grounded_code_is_kept_and_ungrounded_is_dropped(caplog):
    with caplog.at_level(logging.WARNING):
        layers = _layers(
            text="Error 10036015 shown on the pay button",
            signals=[{"type": "error", "code": "10036015"},   # present -> kept
                     {"type": "error", "code": "99999999"}],  # absent -> dropped
            objects=[{"type": "invoice", "id": "INV-55821"},  # absent -> dropped
                     {"type": "tx", "id": "10036015"}],       # present (normalized) -> kept
        )
    kept_codes = [s.code for s in layers.signals]
    kept_ids = [o.id for o in layers.objects]
    assert kept_codes == ["10036015", None]
    assert kept_ids == [None, "10036015"]
    assert sum("attachment_ungrounded_value" in r.message for r in caplog.records) == 2


def test_grounding_normalizes_separators_both_sides():
    # code "TX 778201" is grounded by text "tx-778201" after normalization
    layers = _layers(text="see tx-778201 here", signals=[{"type": "error", "code": "TX 778201"}])
    assert layers.signals[0].code == "TX 778201"


def test_attributes_are_string_capped():
    layers = _layers(objects=[{"type": "t", "attributes": {"k" * 400: "v" * 400}}])
    attr = layers.objects[0].attributes
    assert len(list(attr.keys())[0]) == 300 and len(list(attr.values())[0]) == 300
