"""Mask personal data in attachment transcriptions.

Deliberately conservative — it must NEVER mask error codes or technical IDs
(e.g. `10036015`, `E-REF-9`, `INV-55821`, `TX-778201`). Two rule kinds:

  * pattern-based (context-free): emails and phone numbers — these cannot
    collide with error codes or IDs;
  * label-based ("by context"): names, national ID / iqama and passport
    numbers — only the value immediately after a label such as "رقم الهوية" /
    "National ID" / "الاسم" / "Name" is masked, leaving the label intact.

Nothing here is a guarantee: a masked transcript is still personal data and
must not be treated as safe to publish. Names are masked up to three words,
stopping before a following field label (e.g. "رقم", "الهوية", "phone"), and a
document number that is not label-prefixed is left in place — both are
deliberate conservatism, not bugs.
"""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Saudi mobile 05XXXXXXXX / +966 5XXXXXXXX, plus generic +... phone numbers.
_PHONE_RE = re.compile(
    r"(?<![\d])"
    r"(?:(?:\+?966[\s\-]*)?(?:0)?5\d{8}|(?:\+[\d][\d\s\-()]{7,15}\d))"
    r"(?![\d])"
)

# Label + value: the label (group 1) is kept, the value (group 2) is masked.
_DOC_LABEL = (
    r"(?:رقم\s*(?:الهوية|الإقامة|الاقامة|الجواز)|الهوية\s*الوطنية|جواز\s*السفر|"
    r"national\s*id|iqama|residence\s*(?:id|permit|number)|passport(?:\s*(?:no|number))?)"
)
_DOC_RE = re.compile(r"(" + _DOC_LABEL + r")\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9\-]{1,40})")

_NAME_LABEL = (
    r"(?:الاسم(?:\s*(?:الثلاثي|الرباعي|الأول|الكامل))?|اسم\s*(?:المستفيد|العميل)?|"
    r"full\s*name|customer\s*name|beneficiary\s*name|name)"
)
# A name ends before the next field label (Arabic or English).
_STOP_WORDS = (
    r"رقم|الهوية|الإقامة|الاقامة|الجواز|جواز|الاسم|اسم|البريد|هاتف|عنوان|جوال|"
    r"name|phone|email|address|passport|iqama|national|dob|date|mobile"
)
_NAME_RE = re.compile(
    r"(" + _NAME_LABEL + r")\s*[:：]?\s*"
    r"([\u0600-\u06FFA-Za-z]+"
    r"(?:\s+(?!(?:" + _STOP_WORDS + r")\b)[\u0600-\u06FFA-Za-z]+){0,2})"
    r"(?![\d\-])"
)


def mask_personal_data(text: str) -> str:
    if not text:
        return text
    out = _EMAIL_RE.sub("[EMAIL]", text)
    out = _PHONE_RE.sub("[PHONE]", out)
    out = _DOC_RE.sub(r"\1 [DOC-ID]", out)
    out = _NAME_RE.sub(r"\1 [NAME]", out)
    return out
