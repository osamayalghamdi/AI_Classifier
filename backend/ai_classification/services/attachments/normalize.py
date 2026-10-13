"""Normalization for the attachment layers.

One function, used by the grounding check now and by `value_norm` (storage /
linking) later: the same normal form must collapse `TX-778201`, `tx 778201` and
`TX778201` to one value, and Arabic-Indic digits to their ASCII counterparts.
"""

from __future__ import annotations

_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_SEPARATORS = str.maketrans("", "", " \t\r\n-_")

# Arabic spelling unification for page matching (more aggressive than
# `normalize`, which is for codes/ids and must NOT fold Arabic spelling).
_DIACRITICS = str.maketrans("", "", (
    "\u064B\u064C\u064D\u064E\u064F\u0650\u0651\u0652"  # tashkeel
    "\u0640"                                            # tatweel
    "\u0670"                                            # superscript alef
))
_ALEF_FORMS = str.maketrans("أإآٱ", "اااا")
_TA_MARBUTA = str.maketrans("ة", "ه")
_ALEF_MAQSURA = str.maketrans("ى", "ي")


def normalize(value: str) -> str:
    """Uppercase, Arabic-Indic digits → ASCII, drop spaces/dashes/underscores."""
    return value.translate(_ARABIC_INDIC).translate(_SEPARATORS).upper()


def normalize_page(value: str) -> str:
    """Normal form for page matching: `normalize` plus Arabic spelling
    unification — strip diacritics and tatweel, fold alef forms to ا, ة→ه,
    ى→ي, and drop a leading definite article ال."""
    v = value.translate(_DIACRITICS).translate(_ALEF_FORMS)
    v = v.translate(_TA_MARBUTA).translate(_ALEF_MAQSURA)
    v = v.translate(_ARABIC_INDIC).translate(_SEPARATORS).upper()
    if v.startswith("ال"):
        v = v[2:]
    return v
