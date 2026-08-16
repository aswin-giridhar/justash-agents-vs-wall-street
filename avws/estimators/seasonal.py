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


def _ordered_actuals(facts: list[Fact], target, metric: Metric | None = None) -> list[Fact]:
    """Actuals COMPARABLE WITH THE TARGET, deduplicated, oldest first.

    Comparability is the whole point. A full year and a single quarter are different
    quantities, and Q2 and Q3 are different quantities, so pooling them produces
    nonsense growth rates. An earlier version did exactly that and the adversarial
    critic caught it on seven of twelve metrics - "the median of a mixed set",
    "you mixed quarters", "the -64.9% input is an H1 vs H1 change". Deere's segment
    operating profit came out at 441 against a true level in the low thousands.

    So: a full-year target admits only full-year actuals; a quarterly target admits
    only the SAME quarter of other years.
    """
    from avws.table_facts import BANDS

    band = BANDS.get(metric.key) if metric is not None else None

    # A focused series extraction, when one succeeded, supersedes opportunistically
    # harvested facts entirely. Those are gathered by row-label matching, which is
    # right for finding a build-up's components and wrong for the metric's own time
    # series - ambiguous labels and columns put Hays EPS at 36 pence against a true
    # level near 1.5. Mixing the two sources would reintroduce exactly that noise.
    series = [f for f in facts if f.label == "historical series"]
    if series:
        facts = series

    deduped: dict[str, Fact] = {}
    for fact in facts:
        if fact.basis not in ("reported", "adjusted"):
            continue
        period = periods.parse(fact.period)
        if period is None:
            continue
        if target is not None and period.quarter != target.quarter:
            continue
        # The TIGHT band, not the widened fact-admission band. A build-up needs the
        # components of a figure and those are smaller than it; a trend estimate is
        # of the figure itself, so anything outside its own plausible range is a
        # different quantity that happened to match a row label. Without this the
        # estimator anchored Deere segment operating profit on a 13.6 and returned
        # 0.16 against a true level in the low thousands.
        if band and not (band[0] <= abs(fact.value) <= band[1]):
            continue
        key = str(period)
        existing = deduped.get(key)
        # Prefer the more precise figure when a period appears twice: press
        # releases round ("$3.62 billion") where statement tables do not (3,623).
        if existing is None or len(f"{fact.value:g}") > len(f"{existing.value:g}"):
            deduped[key] = fact
    return sorted(deduped.values(), key=lambda f: periods.sort_key(f.period))


def _recent_yoy_growth(actuals: list[Fact]) -> tuple[float | None, list[str]]:
    """Year-on-year growth from CONSECUTIVE pairs only, most recent weighted.

    Extracted series have gaps - Deere's segment operating profit had FY2020Q3,
    FY2021Q3, FY2024Q3 and FY2025Q3 with two years missing. A ratio between 2021
    and 2024 is not an annual growth rate, and averaging it with real ones produced
    forecasts that were wrong by a factor of two.

    Only genuine adjacent-year pairs count. The median of the three most recent is
    used rather than the whole history, because a five-year-old growth rate says
    little about the coming quarter.
    """
    by_period = {str(periods.parse(f.period)): f for f in actuals
                 if periods.parse(f.period)}
    pairs: list[tuple[int, float, str]] = []
    for key, fact in by_period.items():
        period = periods.parse(key)
        prior = by_period.get(str(period.prior_year()))
        if prior and prior.value:
            growth = (fact.value - prior.value) / abs(prior.value)
            pairs.append((period.year, growth,
                          f"{prior.period}->{fact.period} {growth:+.1%}"))
    if not pairs:
        return None, []
    pairs.sort(key=lambda p: -p[0])
    recent = pairs[:3]
    return (statistics.median(g for _y, g, _d in recent),
            [d for _y, _g, d in recent])


def estimate(metric: Metric, facts: list[Fact]) -> Estimate:
    target = periods.parse(metric.period)
    actuals = _ordered_actuals(facts, target, metric)

    if not actuals:
        return Estimate(
            metric_key=metric.key, value=0.0, method="seasonal_trend",
            derivation="no historical actuals available; emitted zero as a "
                       "last-resort placeholder",
            confidence=0.0,
            warnings=["no history: value is a placeholder, not an estimate"],
        )

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

    # Fall back to the most recent same-period figure we do have, and compound the
    # growth across the actual gap. Carrying a two-year-old figure forward flat put
    # Deere's FY2024Q3 EPS of 6.29 into a FY2026Q3 slot untouched.
    latest = actuals[-1]
    latest_period = periods.parse(latest.period)
    gap = (target.year - latest_period.year) if (target and latest_period) else 1

    if anchor and growth is not None:
        value = anchor.value * (1 + growth)
        derivation = (
            f"prior-year same period {anchor.period} ({anchor.value:g}) x "
            f"(1 + YoY growth {growth:+.2%}) = {value:.4g}\n"
            f"  growth from consecutive pairs: {', '.join(growth_detail)}"
        )
        confidence = 0.4
    elif anchor:
        value = anchor.value
        derivation = (
            f"prior-year same period {anchor.period} ({anchor.value:g}) carried "
            f"forward flat; no consecutive year-on-year pair available = {value:.4g}"
        )
        confidence = 0.25
    elif growth is not None and gap >= 1:
        value = latest.value * (1 + growth) ** gap
        derivation = (
            f"no {metric.period} prior-year figure; most recent comparable "
            f"{latest.period} ({latest.value:g}) compounded over {gap} year(s) at "
            f"{growth:+.2%} = {value:.4g}\n"
            f"  growth from consecutive pairs: {', '.join(growth_detail)}"
        )
        confidence = 0.25
    else:
        value = latest.value
        derivation = (
            f"no prior-year figure and no growth pair for {metric.period}; carried "
            f"{latest.period} ({latest.value:g}) forward unchanged = {value:.4g}"
        )
        confidence = 0.12

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
