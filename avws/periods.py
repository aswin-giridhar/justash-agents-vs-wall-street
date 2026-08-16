"""Fiscal period parsing.

Companies label periods inconsistently - "FY2026Q3", "Q3 2026", "FY 2026",
"fiscal 2026 second quarter". Comparing these as raw strings is what produced the
first end-to-end crash, and would silently mis-align a year-on-year comparison if
it had not crashed. Parse once, compare structurally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATTERNS = (
    re.compile(r"FY\s*(?P<year>\d{4})\s*Q(?P<q>[1-4])", re.IGNORECASE),
    re.compile(r"Q(?P<q>[1-4])\s*(?:FY)?\s*(?P<year>\d{4})", re.IGNORECASE),
    re.compile(r"FY\s*(?P<year>\d{4})", re.IGNORECASE),
    re.compile(r"(?P<year>\d{4})"),
)


@dataclass(frozen=True, order=True)
class Period:
    year: int
    quarter: int  # 0 means a full year

    @property
    def is_full_year(self) -> bool:
        return self.quarter == 0

    def prior_year(self) -> Period:
        return Period(self.year - 1, self.quarter)

    def __str__(self) -> str:
        return f"FY{self.year}" + (f"Q{self.quarter}" if self.quarter else "")


def parse(label: str) -> Period | None:
    if not label:
        return None
    text = label.strip()
    for pattern in _PATTERNS:
        match = pattern.search(text)
        if match:
            groups = match.groupdict()
            year = int(groups["year"])
            quarter = int(groups["q"]) if groups.get("q") else 0
            if 1990 <= year <= 2100:
                return Period(year, quarter)
    return None


def same_quarter_prior_year(target: str, candidate: str) -> bool:
    a, b = parse(target), parse(candidate)
    if a is None or b is None:
        return False
    return b == a.prior_year()


def sort_key(label: str) -> tuple[int, int]:
    period = parse(label)
    return (period.year, period.quarter) if period else (0, 0)
