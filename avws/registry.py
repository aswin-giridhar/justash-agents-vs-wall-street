"""The 12 target metrics.

Labels are read from challenge/companies.json rather than hardcoded. The workbook
templates are keyed on those exact strings, so a typo anywhere in our code would
produce a structurally invalid submission.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache

from avws.config import CHALLENGE_DIR


@dataclass(frozen=True)
class Metric:
    key: str
    company: str
    ticker: str
    period: str
    label: str
    units: str
    output_file: str

    @property
    def is_percentage(self) -> bool:
        return self.units.strip() == "%"

    @property
    def is_eps(self) -> bool:
        return "share" in self.units.lower() or self.units.strip() == "GBp"

    def floor(self, actual: float) -> float:
        """Competition denominator floor for this metric given a reported result."""
        from avws.config import MONEY_FLOOR_FRACTION, PERCENTAGE_FLOOR_PP

        if self.is_percentage:
            return PERCENTAGE_FLOOR_PP
        return max(abs(actual) * MONEY_FLOOR_FRACTION, 1e-9)


@lru_cache(maxsize=1)
def load_metrics() -> tuple[Metric, ...]:
    data = json.loads((CHALLENGE_DIR / "companies.json").read_text(encoding="utf-8"))
    metrics = []
    for company in data["companies"]:
        ticker = company["ticker"].split(":")[-1]
        for m in company["metrics"]:
            metrics.append(
                Metric(
                    key=f"{ticker}:{m['label']}",
                    company=company["company"],
                    ticker=ticker,
                    period=company["period"],
                    label=m["label"],
                    units=m["units"],
                    output_file=company["outputFile"],
                )
            )
    return tuple(metrics)


def metrics_for(ticker: str) -> list[Metric]:
    return [m for m in load_metrics() if m.ticker == ticker]


def get_metric(key: str) -> Metric:
    for m in load_metrics():
        if m.key == key:
            return m
    raise KeyError(f"unknown metric key: {key}")


def tickers() -> list[str]:
    seen: list[str] = []
    for m in load_metrics():
        if m.ticker not in seen:
            seen.append(m.ticker)
    return seen
