"""Normalization for the attachment layers.

One function, used by the grounding check now and by `value_norm` (storage /
linking) later: the same normal form must collapse `TX-778201`, `tx 778201` and
`TX778201` to one value, and Arabic-Indic digits to their ASCII counterparts.
"""

from __future__ import annotations

_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_SEPARATORS = str.maketrans("", "", " \t\r\n-_")


def normalize(value: str) -> str:
    """Uppercase, Arabic-Indic digits → ASCII, drop spaces/dashes/underscores."""
    return value.translate(_ARABIC_INDIC).translate(_SEPARATORS).upper()
