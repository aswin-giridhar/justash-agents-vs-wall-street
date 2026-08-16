"""Guidance anchor.

When a company publishes its own outlook, that is the highest-information input
available. ADI's Q2 release stated: "we are forecasting revenue of $3.9 billion,
+/- $100 million ... adjusted EPS to be $3.30, +/- $0.15".

The forecasting question is therefore not "what will revenue be" but "where inside
that range do they land". Companies are systematically biased against their own
guidance - ADI's same release noted Q2 came in "above the high end of our outlook".
So the estimator anchors on the midpoint and applies a residual **measured** from
how this company has historically landed versus its own guidance, rather than
assuming the midpoint is unbiased.

With too few paired observations the residual is set to zero and that fact is
recorded as an assumption, so a thin calibration is visible rather than hidden.
"""

from __future__ import annotations

import statistics

from avws.estimators.base import Estimate
from avws.ledger import Fact

MIN_PAIRS_FOR_RESIDUAL = 3


def _mid(facts: list[Fact], period: str) -> Fact | None:
    mids = [f for f in facts if f.basis == "guidance_mid" and f.period == period]
    if mids:
        return max(mids, key=lambda f: f.confidence)
    lows = [f for f in facts if f.basis == "guidance_low" and f.period == period]
    highs = [f for f in facts if f.basis == "guidance_high" and f.period == period]
    if lows and highs:
        low, high = lows[0], highs[0]
        return Fact(
            metric_key=low.metric_key, company=low.company, period=period,
            value=(low.value + high.value) / 2, unit=low.unit, basis="guidance_mid",
            source_doc=low.source_doc,
            source_quote=f"{low.source_quote} / {high.source_quote}",
            confidence=min(low.confidence, high.confidence),
        )
    return None


def measure_residual(facts: list[Fact]) -> tuple[float, int, list[str]]:
    """Median fractional gap between actual outturn and prior guidance midpoint.

    Returns (residual, pair_count, detail_lines). Positive means the company has
    historically beaten its own midpoint.
    """
    actuals = {
        f.period: f.value
        for f in facts
        if f.basis in ("reported", "adjusted")
    }
    residuals: list[float] = []
    detail: list[str] = []
    for fact in facts:
        if fact.basis != "guidance_mid":
            continue
        actual = actuals.get(fact.period)
        if actual is None or fact.value == 0:
            continue
        gap = (actual - fact.value) / abs(fact.value)
        residuals.append(gap)
        detail.append(f"{fact.period}: guided {fact.value:g} -> actual {actual:g} "
                      f"({gap:+.2%})")
    if len(residuals) < MIN_PAIRS_FOR_RESIDUAL:
        return 0.0, len(residuals), detail
    return statistics.median(residuals), len(residuals), detail


def estimate(
    metric_key: str,
    facts: list[Fact],
    period: str,
    residual_pct: float | None = None,
    calibration_pairs: int | None = None,
    calibration_detail: list[str] | None = None,
) -> Estimate | None:
    anchor = _mid(facts, period)
    if anchor is None:
        return None

    detail: list[str] = []
    pairs = 0
    if residual_pct is None:
        residual_pct, pairs, detail = measure_residual(facts)
    elif calibration_detail is not None:
        pairs, detail = calibration_pairs or 0, list(calibration_detail)

    value = anchor.value * (1.0 + residual_pct)
    if residual_pct:
        derivation = (
            f"guidance midpoint {anchor.value:g} {anchor.unit} "
            f"x (1 + {residual_pct:+.4f} measured residual over {pairs} prior "
            f"guided periods) = {value:.4g}"
        )
    else:
        derivation = (
            f"guidance midpoint {anchor.value:g} {anchor.unit} taken unadjusted "
            f"({pairs} paired observations, fewer than {MIN_PAIRS_FOR_RESIDUAL} "
            f"needed to measure a residual) = {value:.4g}"
        )

    warnings = []
    if pairs < MIN_PAIRS_FOR_RESIDUAL:
        warnings.append(
            f"guidance residual not calibrated ({pairs} paired observations)"
        )

    return Estimate(
        metric_key=metric_key,
        value=value,
        method="guidance_anchor",
        derivation=derivation + ("\n  " + "\n  ".join(detail) if detail else ""),
        assumptions={"residual_pct": residual_pct, "calibration_pairs": float(pairs)},
        inputs=[anchor],
        confidence=0.85 if pairs >= MIN_PAIRS_FOR_RESIDUAL else 0.7,
        warnings=warnings,
    )
