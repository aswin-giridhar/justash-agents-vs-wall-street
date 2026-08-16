"""Replay the estimator chain over historical periods whose answers are known.

The competition scores us against a Wall Street benchmark we cannot see, so we
cannot measure the thing we are actually graded on. What we *can* measure is the
statistic the scoring function is built from: how often our absolute error falls
inside the denominator floor.

    metric score = min(5.0, |team miss| / max(|Wall St miss|, floor))

If our error is below the floor, that metric scores under 1.0 whatever Wall Street
did. "Floor-band hit rate" is therefore the most decision-relevant number we can
compute without the benchmark, and it is what this harness reports.

Leakage control: for a target period P the harness passes only facts whose own
period precedes P, plus guidance issued for P (which by definition is published
before P closes). The known residual leak is restatements - a figure for an
earlier period revised in a later filing carries the later filing's knowledge.
That is disclosed rather than papered over.
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass

from avws import periods, reconcile
from avws.config import CACHE_DIR
from avws.estimators import guidance, seasonal
from avws.ledger import Fact, facts_for
from avws.registry import Metric, get_metric, load_metrics

# A target needs at least this many earlier observations before it can be predicted,
# so the estimator has something to work from rather than being scored on a guess.
MIN_PRIOR_OBSERVATIONS = 2


@dataclass
class Outcome:
    period: str
    actual: float
    predicted: float
    method: str
    error: float
    floor: float
    within_floor: bool


@dataclass
class Result:
    metric_key: str
    outcomes: list[Outcome]

    @property
    def n(self) -> int:
        return len(self.outcomes)

    @property
    def mae(self) -> float:
        return statistics.mean(o.error for o in self.outcomes) if self.outcomes else 0.0

    @property
    def median_error(self) -> float:
        return (statistics.median(o.error for o in self.outcomes)
                if self.outcomes else 0.0)

    @property
    def floor_hit_rate(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(o.within_floor for o in self.outcomes) / len(self.outcomes)

    def as_dict(self) -> dict:
        return {
            "metric": self.metric_key,
            "n": self.n,
            "mae": round(self.mae, 4),
            "median_error": round(self.median_error, 4),
            "floor_hit_rate": round(self.floor_hit_rate, 3),
            "outcomes": [
                {
                    "period": o.period, "actual": o.actual,
                    "predicted": round(o.predicted, 4), "method": o.method,
                    "error": round(o.error, 4), "floor": round(o.floor, 4),
                    "within_floor": o.within_floor,
                }
                for o in self.outcomes
            ],
        }


def _actual_by_period(facts: list[Fact]) -> dict[str, float]:
    """One actual per period, taken ONLY from the focused series extraction.

    An earlier version drew targets from the whole ledger, which includes
    opportunistically row-matched facts. Scoring forecasts against unreliable
    targets made the harness measure noise: it reported MAE of 31,275 on a metric
    near 45,000 while the median error was 1,406, the signature of a handful of
    mis-attributed periods. A probe that cannot separate a good forecast from a bad
    one is not evidence, so the targets are restricted to the series facts, which
    are extracted for exactly this purpose and carry verified quotes.
    """
    out: dict[str, float] = {}
    for fact in facts:
        if fact.label != "historical series":
            continue
        if fact.basis not in ("reported", "adjusted"):
            continue
        key = str(periods.parse(fact.period) or fact.period)
        existing = out.get(key)
        if existing is None or len(f"{fact.value:g}") > len(f"{existing:g}"):
            out[key] = fact.value
    return out


def run(metric: Metric, facts: list[Fact] | None = None) -> Result:
    facts = facts if facts is not None else facts_for(metric.key)
    actuals = _actual_by_period(facts)
    ordered = sorted(actuals, key=periods.sort_key)

    outcomes: list[Outcome] = []
    for index, target in enumerate(ordered):
        if index < MIN_PRIOR_OBSERVATIONS:
            continue
        target_period = periods.parse(target)
        if target_period is None:
            continue

        visible = [
            f for f in facts
            if (periods.parse(f.period) is not None
                and periods.parse(f.period) < target_period)
            or (f.basis.startswith("guidance")
                and periods.parse(f.period) == target_period)
        ]
        if not visible:
            continue

        # A metric whose period label differs from the target is fine here; the
        # estimators read metric.period, so give them a target-shifted view.
        shifted = Metric(
            key=metric.key, company=metric.company, ticker=metric.ticker,
            period=str(target_period), label=metric.label, units=metric.units,
            output_file=metric.output_file,
        )

        candidates = []
        anchor = guidance.estimate(metric.key, visible, str(target_period))
        if anchor:
            candidates.append(anchor)
        candidates.append(seasonal.estimate(shifted, visible))

        try:
            blended = reconcile.combine(candidates, metric.key)
        except ValueError:
            continue

        actual = actuals[target]
        error = abs(blended.value - actual)
        floor = shifted.floor(actual)
        outcomes.append(Outcome(
            period=target, actual=actual, predicted=blended.value,
            method=blended.method, error=error, floor=floor,
            within_floor=error < floor,
        ))

    return Result(metric_key=metric.key, outcomes=outcomes)


def run_all() -> list[Result]:
    return [run(metric) for metric in load_metrics()]


def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Replay the estimator chain over known historical periods."
    )
    parser.add_argument("--metric", help="single metric key, e.g. 'ADI:Revenue'")
    args = parser.parse_args()

    results = [run(get_metric(args.metric))] if args.metric else run_all()

    print(f"{'metric':<48} {'n':>3} {'MAE':>10} {'median':>10} {'floor hit':>10}")
    print("-" * 84)
    for result in results:
        if not result.n:
            print(f"{result.metric_key:<48} {'-':>3} {'no usable history':>32}")
            continue
        print(f"{result.metric_key:<48} {result.n:>3} {result.mae:>10.3f} "
              f"{result.median_error:>10.3f} {result.floor_hit_rate:>9.0%}")

    scored = [r for r in results if r.n]
    if scored:
        overall = statistics.mean(r.floor_hit_rate for r in scored)
        print("-" * 84)
        print(f"{'mean floor-band hit rate across metrics':<48} {overall:>33.0%}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / "backtest.json").write_text(
        json.dumps([r.as_dict() for r in results], indent=1), encoding="utf-8"
    )
    print(f"\nwrote {CACHE_DIR / 'backtest.json'}")


if __name__ == "__main__":
    _cli()
