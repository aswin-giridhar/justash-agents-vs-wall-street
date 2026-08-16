"""Measure how a company lands against its own guidance.

Companies are systematically biased against their published outlook. ADI's Q2 2026
release says its own quarter came in "above the high end of our outlook". That bias
is a measurable quantity, not a hunch - but measuring it needs a decent sample.

The guidance anchor was previously calibrated on whatever guidance facts happened to
be picked up while extracting the target metric: zero to two paired observations,
which is noise wearing the costume of a calibration. This module goes looking for
the pairs deliberately, across the whole corpus, and caches the result.

If fewer than MIN_PAIRS survive, it returns a zero residual and says so. A
calibration that cannot be measured must not be pretended.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass

from avws import periods
from avws.config import CACHE_DIR
from avws.corpus import build_index, search
from avws.llm import complete
from avws.registry import Metric

CALIBRATION_CACHE = CACHE_DIR / "calibration"
MIN_PAIRS = 3

PAIR_SYSTEM = """You collect a company's PAST GUIDANCE and what it actually reported.

For one named metric, find pairs where the company:
  (a) published an outlook, forecast or guidance figure for a future period, AND
  (b) later reported the actual result for that same period.

Return one row per period with both numbers. Rules:
1. `guided` is the MIDPOINT of the guidance if a range was given.
2. `actual` is the reported result for that same period, on the same basis.
3. Both need a verbatim quote. `guidance_quote` for the outlook,
   `actual_quote` for the reported result.
4. Same basis for both. Do not pair adjusted guidance with a GAAP actual.
5. Same period type. Do not pair a full-year outlook with a quarterly result.
6. Return only pairs you can evidence from the text. Fewer correct pairs are far
   more useful than more approximate ones - this measurement calibrates every
   forecast that follows."""

PAIR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pairs"],
    "properties": {
        "pairs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["period", "guided", "actual",
                             "guidance_quote", "actual_quote"],
                "properties": {
                    "period": {"type": "string"},
                    "guided": {"type": "number"},
                    "actual": {"type": "number"},
                    "guidance_quote": {"type": "string"},
                    "actual_quote": {"type": "string"},
                },
            },
        }
    },
}


@dataclass
class Calibration:
    metric_key: str
    residual: float
    pairs: int
    detail: list[str]
    dispersion: float
    beat_rate: float

    @property
    def measured(self) -> bool:
        return self.pairs >= MIN_PAIRS

    def describe(self) -> str:
        if not self.measured:
            return (f"guidance bias not measured: only {self.pairs} verified "
                    f"guidance/actual pairs (need {MIN_PAIRS}); residual set to zero")
        return (f"guidance bias {self.residual:+.2%} (median of {self.pairs} pairs, "
                f"dispersion {self.dispersion:.2%}, beat rate {self.beat_rate:.0%})")


def _cache_path(metric: Metric):
    digest = hashlib.sha256((metric.key + PAIR_SYSTEM).encode()).hexdigest()[:12]
    return CALIBRATION_CACHE / f"{metric.ticker}-{digest}.json"


def measure(metric: Metric, use_cache: bool = True) -> Calibration:
    cache_file = _cache_path(metric)
    if use_cache and cache_file.exists():
        return Calibration(**json.loads(cache_file.read_text(encoding="utf-8")))

    build_index()
    chunks, seen = [], set()
    # Deliberately NOT restricted to filings, and reaching back further. Most
    # metrics previously yielded fewer than three verified pairs, which is noise
    # wearing the costume of a calibration. Guidance is issued in press releases but
    # discussed - and compared with the outturn - on earnings calls, so excluding
    # transcripts threw away half the available evidence.
    for query in (
        f"outlook for the next quarter we are forecasting {metric.label}",
        f"we expect {metric.label} guidance range midpoint",
        f"{metric.label} came in above below our outlook guidance",
        f"{metric.label} results compared with our prior outlook",
        f"{metric.label} above the high end of our outlook exceeded guidance",
        f"versus the guidance we gave last quarter {metric.label} delivered",
    ):
        for doc, _s, chunk in search(query, ticker=metric.ticker,
                                     since="2019-01-01", k=10):
            key = f"{doc.path}:{hash(chunk)}"
            if key in seen:
                continue
            seen.add(key)
            chunks.append(f"<<<{doc.path} | {doc.published_at} | "
                          f"period {doc.period}>>>\n{chunk}")

    result = Calibration(metric.key, 0.0, 0, [], 0.0, 0.0)
    if chunks:
        body = "\n\n".join(chunks[:36])
        haystack = " ".join(body.split()).lower()
        payload = complete(
            PAIR_SYSTEM,
            f"Company: {metric.company} ({metric.ticker})\n"
            f"Metric: {metric.label} ({metric.units})\n\n{body}",
            PAIR_SCHEMA, schema_name="calibration",
        )

        residuals, detail = [], []
        for item in payload.get("pairs", []):
            guided, actual = item.get("guided"), item.get("actual")
            if not guided or actual is None or periods.parse(item.get("period", "")) is None:
                continue
            # Both quotes must be verifiable, or the pair is discarded entirely.
            ok = all(
                " ".join((item.get(field) or "").split()).lower()[:45] in haystack
                for field in ("guidance_quote", "actual_quote")
            )
            if not ok:
                continue
            gap = (actual - guided) / abs(guided)
            if abs(gap) > 0.6:
                continue  # a 60%+ gap is a mis-paired basis, not a guidance miss
            residuals.append(gap)
            detail.append(f"{item['period']}: guided {guided:g} -> actual "
                          f"{actual:g} ({gap:+.2%})")

        if residuals:
            result = Calibration(
                metric_key=metric.key,
                residual=statistics.median(residuals),
                pairs=len(residuals),
                detail=detail,
                dispersion=(statistics.pstdev(residuals) if len(residuals) > 1 else 0.0),
                beat_rate=sum(r > 0 for r in residuals) / len(residuals),
            )

    if not result.measured:
        result = Calibration(metric.key, 0.0, result.pairs, result.detail,
                             result.dispersion, result.beat_rate)

    CALIBRATION_CACHE.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(result.__dict__, indent=1), encoding="utf-8")
    return result
