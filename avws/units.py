"""Number and unit normalisation.

The competition caps a metric at 5.0 while the floor band is 1.0, so a single
scale error costs more than several good forecasts earn. That asymmetry is why
this is a module with its own tests rather than a helper function.

The central rule: absent and malformed both resolve to None, never to 0.0. A
silent zero flows downstream and corrupts arithmetic without ever raising.
"""

from __future__ import annotations

import re

_CLEAN = re.compile(r"[,\s$£€]|(?:bps)|(?:%)", re.IGNORECASE)
_NUMERIC = re.compile(r"^-?\d*\.?\d+$")
# Em dash, en dash, hyphen alone and similar placeholders mean "no value".
_BLANKS = {"", "-", "--", "—", "–", "n/a", "na", "nm", "nil", "none"}

_SCALES = {
    "billion": 1000.0,
    "bn": 1000.0,
    "b": 1000.0,
    "million": 1.0,
    "m": 1.0,
    "mn": 1.0,
    "thousand": 0.001,
    "k": 0.001,
}


def parse_number(raw: str | float | int | None) -> float | None:
    """Parse a figure as it appears in a filing table.

    Handles currency symbols, thousands separators, percent signs, bps suffixes
    and parenthesised negatives. Returns None for placeholders and anything that
    is not a number.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    text = raw.strip()
    if text.lower() in _BLANKS:
        return None

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]

    text = _CLEAN.sub("", text).strip()
    if text.lower() in _BLANKS or not _NUMERIC.match(text):
        return None

    value = float(text)
    return -value if negative else value


def to_millions(value: float, unit_hint: str) -> float:
    """Rescale a figure to millions using a textual unit hint from its context."""
    hint = (unit_hint or "").strip().lower()
    for token, factor in _SCALES.items():
        if re.search(rf"\b{re.escape(token)}\b", hint):
            return value * factor
    return value


def pounds_to_pence(value: float) -> float:
    return value * 100.0


def looks_like_fraction_not_percent(value: float, history: list[float]) -> bool:
    """True when a percentage appears to have been entered as a fraction.

    0.045 where 4.5 was meant is the single most expensive error available under
    this scoring function, so it gets its own named check.
    """
    if not history:
        return False
    typical = max(abs(h) for h in history)
    return typical >= 0.5 and abs(value) < typical / 20
