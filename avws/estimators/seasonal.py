"""Seasonal-trend fallback.

This estimator exists to guarantee that no metric is ever blank. A missing
forecast scores 5.0 under the competition rules - the same as being wrong by ten
times the floor - so an always-available estimator is worth more than its
sophistication suggests.

The anchor is the **same quarter of the prior year**, not the most recent quarter.
These businesses are strongly seasonal: Home Depot sells more in spring, Deere's
equipment sales follow the agricultural cycle. An earlier version extrapolated
from the latest quarter and forecast ADI Q3 revenue at $4,964m against company
guidance of $3,900m, because it carried Q2's level into a Q3 slot. Anchoring on
the prior-year same quarter and applying recent year-on-year growth removes that
error by construction.
"""

from __future__ import annotations

import statistics

from avws import periods
from avws.estimators.base import Estimate
from avws.ledger import Fact
from avws.registry import Metric


def _ordered_actuals(facts: list[Fact]) -> list[Fact]:
    actuals = [f for f in facts if f.basis in ("reported", "adjusted")]
    deduped: dict[str, Fact] = {}
    for fact in actuals:
        key = str(periods.parse(fact.period) or fact.period)
        existing = deduped.get(key)
        # Prefer the more precise figure when a period appears twice: press
        # releases round ("$3.62 billion") where statement tables do not (3,623).
        if existing is None or len(f"{fact.value:g}") > len(f"{existing.value:g}"):
            deduped[key] = fact
    return sorted(deduped.values(), key=lambda f: periods.sort_key(f.period))


def _recent_yoy_growth(actuals: list[Fact]) -> tuple[float | None, list[str]]:
    """Median year-on-year growth across every prior-year pair we can form."""
    by_period = {str(periods.parse(f.period)): f for f in actuals
                 if periods.parse(f.period)}
    growths: list[float] = []
    detail: list[str] = []
    for key, fact in by_period.items():
        period = periods.parse(key)
        prior = by_period.get(str(period.prior_year()))
        if prior and prior.value:
            growth = (fact.value - prior.value) / abs(prior.value)
            growths.append(growth)
            detail.append(f"{prior.period}->{fact.period} {growth:+.1%}")
    if not growths:
        return None, detail
    return statistics.median(growths), detail


def estimate(metric: Metric, facts: list[Fact]) -> Estimate:
    actuals = _ordered_actuals(facts)

    if not actuals:
        return Estimate(
            metric_key=metric.key, value=0.0, method="seasonal_trend",
            derivation="no historical actuals available; emitted zero as a "
                       "last-resort placeholder",
            confidence=0.0,
            warnings=["no history: value is a placeholder, not an estimate"],
        )

    target = periods.parse(metric.period)
    values = [f.value for f in actuals]

    # Percentages are levels, not compounding quantities: a 73% margin does not
    # "grow" onto a prior margin. Prefer the same quarter last year if we have it.
    if metric.is_percentage:
        prior_year = next(
            (f for f in actuals
             if target and periods.parse(f.period) == target.prior_year()),
            None,
        )
        window = values[-4:]
        recent_mean = statistics.mean(window)
        if prior_year:
            value = (prior_year.value + recent_mean) / 2
            derivation = (
                f"mean of prior-year same period {prior_year.period} "
                f"({prior_year.value:g}) and the last {len(window)} reported levels "
                f"(mean {recent_mean:.4g}) = {value:.4g} "
                f"(percentages averaged as levels, never compounded)"
            )
        else:
            value = recent_mean
            derivation = (
                f"mean of last {len(window)} reported levels "
                f"({', '.join(f'{v:g}' for v in window)}) = {value:.4g}"
            )
        return Estimate(
            metric_key=metric.key, value=value, method="seasonal_trend",
            derivation=derivation,
            assumptions={"observations": float(len(values))},
            inputs=actuals[-4:], confidence=0.35,
        )

    # Level metrics: anchor on the same quarter one year before the TARGET period.
    anchor = next(
        (f for f in actuals
         if target and periods.parse(f.period) == target.prior_year()),
        None,
    )
    growth, growth_detail = _recent_yoy_growth(actuals)

    if anchor and growth is not None:
        value = anchor.value * (1 + growth)
        derivation = (
            f"prior-year same period {anchor.period} ({anchor.value:g}) x "
            f"(1 + median YoY growth {growth:+.2%}) = {value:.4g}\n"
            f"  growth observations: {', '.join(growth_detail)}"
        )
        confidence = 0.4
    elif anchor:
        value = anchor.value
        derivation = (
            f"prior-year same period {anchor.period} ({anchor.value:g}) carried "
            f"forward flat; no year-on-year growth pair available = {value:.4g}"
        )
        confidence = 0.25
    else:
        latest = actuals[-1]
        value = latest.value
        derivation = (
            f"no prior-year figure for {metric.period}; carried latest actual "
            f"{latest.period} ({latest.value:g}) forward unchanged = {value:.4g}"
        )
        confidence = 0.15

    warnings = []
    if confidence <= 0.25:
        warnings.append(
            "seasonal fallback had no prior-year anchor; treat as weak evidence"
        )

    return Estimate(
        metric_key=metric.key, value=value, method="seasonal_trend",
        derivation=derivation,
        assumptions={"observations": float(len(values)),
                     "yoy_growth": growth if growth is not None else 0.0},
        inputs=[f for f in (anchor,) if f] or actuals[-2:],
        confidence=confidence, warnings=warnings,
    )
