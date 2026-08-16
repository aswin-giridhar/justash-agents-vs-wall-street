"""Turn candidate estimates into the number we actually submit.

Two ideas from how a fund would think about this, neither of which a weighted
average captures.

**Optimise the objective we are scored on, not "be right on average".**

    metric score = min(5.0, |x - a| / max(|c - a|, floor))

For errors inside the cap this is linear in |x - a|, and expected absolute error is
minimised by the MEDIAN of the predictive distribution, not its mean. The cap makes
the loss even more robust, which pushes further toward the median. Where estimators
disagree sharply - and ours have disagreed by over 100% on some metrics - the mean
sits in an empty region between two clusters that no estimator considers plausible.
The median sits on evidence.

**Know where the crowd is.**

We are scored relative to the consensus mean, which we cannot see. But we can proxy
it: for a guided metric, consensus clusters near the company's guidance midpoint,
because that is the strongest public signal every analyst also has. Making that
proxy explicit turns "our number happens to differ from consensus" into a decision
with a stated reason, and lets us report the deviation we are taking.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from avws.estimators.base import Estimate
from avws.ledger import Fact
from avws.registry import Metric


@dataclass
class Decision:
    value: float
    method: str
    rationale: str
    consensus_proxy: float | None
    deviation_pct: float | None
    candidates: list[tuple[str, float, float]]  # method, value, weight
    spread_pct: float
    floor: float | None

    def describe(self) -> str:
        lines = [self.rationale]
        if self.consensus_proxy is not None:
            lines.append(
                f"consensus proxy {self.consensus_proxy:.6g}; we submit "
                f"{self.value:.6g}, a deviation of {self.deviation_pct:+.2%}"
            )
        else:
            lines.append("no consensus proxy available for this metric")
        return "\n  ".join(lines)


def consensus_proxy(metric: Metric, facts: list[Fact]) -> float | None:
    """Best available stand-in for where the analyst mean sits.

    Company guidance for the exact period is the strongest public anchor every
    analyst shares, so consensus clusters near its midpoint. Without guidance we
    have no defensible proxy and say so rather than inventing one.
    """
    mids = [f.value for f in facts
            if f.basis == "guidance_mid" and f.period == metric.period]
    if mids:
        return statistics.median(mids)

    lows = [f.value for f in facts
            if f.basis == "guidance_low" and f.period == metric.period]
    highs = [f.value for f in facts
             if f.basis == "guidance_high" and f.period == metric.period]
    if lows and highs:
        return (statistics.median(lows) + statistics.median(highs)) / 2
    return None


def choose(
    metric: Metric,
    candidates: list[Estimate],
    weights: dict[str, float],
    facts: list[Fact],
) -> Decision:
    """Pick the submitted value from the weighted candidate distribution."""
    usable = [e for e in candidates if e.confidence > 0.0]
    if not usable:
        raise ValueError(f"no usable candidate for {metric.key}")

    values = [e.value for e in usable]
    spread = (max(values) - min(values)) / (abs(statistics.mean(values)) or 1.0)

    # Build the weighted predictive distribution and take its weighted median.
    pairs = sorted(
        ((e.value, weights.get(e.method, 1.0 / len(usable))) for e in usable),
        key=lambda p: p[0],
    )
    total = sum(w for _v, w in pairs) or 1.0
    cumulative = 0.0
    median = pairs[-1][0]
    for value, weight in pairs:
        cumulative += weight / total
        if cumulative >= 0.5:
            median = value
            break

    weighted_mean = sum(v * w for v, w in pairs) / total

    if len(usable) == 1:
        chosen, method, why = values[0], usable[0].method, (
            "single candidate; submitted as produced"
        )
    elif spread > 0.15:
        chosen, method = median, "weighted_median"
        why = (
            f"candidates disagree by {spread:.1%}; submitted the weighted MEDIAN "
            f"{median:.6g} rather than the mean {weighted_mean:.6g}. Expected "
            f"absolute error under this scoring function is minimised at the "
            f"median, and with wide disagreement the mean falls between clusters "
            f"that no estimator considers plausible"
        )
    else:
        chosen, method = weighted_mean, "weighted_mean"
        why = (
            f"candidates agree within {spread:.1%}; submitted the weighted mean "
            f"{weighted_mean:.6g} (median {median:.6g} is materially the same)"
        )

    proxy = consensus_proxy(metric, facts)
    deviation = ((chosen - proxy) / abs(proxy)) if proxy else None
    floor = metric.floor(chosen) if chosen else None

    return Decision(
        value=chosen, method=method, rationale=why, consensus_proxy=proxy,
        deviation_pct=deviation,
        candidates=[(e.method, e.value, weights.get(e.method, 0.0)) for e in usable],
        spread_pct=spread, floor=floor,
    )
