"""Universal attachment layers — the single contract every attachment is reduced to.

Every field is optional and every list may be empty; the model must never invent
a value to fill a field. Validation (code, never the prompt) enforces:

  * unknown extra keys are dropped, not fatal;
  * strings are trimmed and length-capped (text <= 8000, everything else <= 300);
  * `quality="unreadable"` keeps only `summary_en` + `quality`;
  * GROUNDING: a `signals[].code` or `objects[].id` that does not appear in the
    transcription (after normalization) is dropped and logged as
    `attachment_ungrounded_value` — the anti-hallucination guard.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .normalize import normalize

_log = logging.getLogger("attachments")

MAX_TEXT = 8000
MAX_SHORT = 300


def _cap(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value.strip()[:limit] or None


class Where(BaseModel):
    """Where in (or outside) the platform the attachment is."""

    model_config = ConfigDict(extra="ignore")

    portal: str | None = None
    page: str | None = None
    page_en: str | None = None
    section: str | None = None
    url: str | None = None
    external_source: str | None = None

    @field_validator("portal", "page", "page_en", "section", "url", "external_source")
    @classmethod
    def _cap_short(cls, value):
        return _cap(value, MAX_SHORT)


class AttObject(BaseModel):
    """One business object shown in the attachment. `type` is open vocabulary."""

    model_config = ConfigDict(extra="ignore")

    type: str = ""
    id: str | None = None
    label_as_shown: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)

    @field_validator("type", "label_as_shown", "id")
    @classmethod
    def _cap_short(cls, value):
        return _cap(value, MAX_SHORT)

    @field_validator("attributes")
    @classmethod
    def _cap_attrs(cls, value):
        return {str(k)[:MAX_SHORT]: str(v)[:MAX_SHORT] for k, v in value.items()}


class Signal(BaseModel):
    """Anything abnormal or decision-relevant. An error is just one kind."""

    model_config = ConfigDict(extra="ignore")

    type: str = ""
    code: str | None = None
    text: str | None = None
    text_en: str | None = None
    about: str | None = None

    @field_validator("type", "code", "text", "text_en", "about")
    @classmethod
    def _cap_short(cls, value):
        return _cap(value, MAX_SHORT)


class AttachmentLayers(BaseModel):
    """The full record for one attachment."""

    model_config = ConfigDict(extra="ignore")

    where: Where = Field(default_factory=Where)
    objects: list[AttObject] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    text: str = ""
    summary_en: str = ""
    quality: Literal["clear", "partial", "unreadable"] = "clear"
    relation_hint: str | None = None

    @field_validator("text")
    @classmethod
    def _cap_text(cls, value):
        return value.strip()[:MAX_TEXT]

    @field_validator("summary_en", "relation_hint")
    @classmethod
    def _cap_short(cls, value):
        return _cap(value, MAX_SHORT)

    @model_validator(mode="after")
    def _apply_rules(self) -> "AttachmentLayers":
        if self.quality == "unreadable":
            # Keep only the summary and the quality flag; the rest is noise.
            self.where = Where()
            self.objects = []
            self.signals = []
            self.text = ""
            self.relation_hint = None
            return self

        norm_text = normalize(self.text)
        if not norm_text:
            # Nothing to ground against: drop any codes/ids the model invented.
            self.objects = [o.model_copy(update={"id": None}) for o in self.objects]
            self.signals = [s.model_copy(update={"code": None}) for s in self.signals]
            return self

        self.objects = [_ground_object(o, norm_text) for o in self.objects]
        self.signals = [_ground_signal(s, norm_text) for s in self.signals]
        return self


def _ground_object(obj: AttObject, norm_text: str) -> AttObject:
    if obj.id and normalize(obj.id) not in norm_text:
        _log.warning("attachment_ungrounded_value: object id %r dropped", obj.id)
        return obj.model_copy(update={"id": None})
    return obj


def _ground_signal(sig: Signal, norm_text: str) -> Signal:
    if sig.code and normalize(sig.code) not in norm_text:
        _log.warning("attachment_ungrounded_value: signal code %r dropped", sig.code)
        return sig.model_copy(update={"code": None})
    return sig
