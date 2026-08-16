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

# How many backtested periods each metric exercised the guidance anchor on, rather
# than falling through to the seasonal path. Reporting this stops the headline hit
# rate being read as a statement about the whole system when it describes one path.
_COVERAGE: dict[str, tuple[int, int]] = {}


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


def _historical_guidance(metric: Metric) -> dict[str, float]:
    """Guidance the company issued for past periods, from the calibration pairs.

    Without this the harness could only ever exercise the seasonal fallback: for a
    historical target there is no guidance fact in the ledger, so the guidance
    anchor never fired and the reported hit rate described our weakest path rather
    than the system. The calibration module already extracts verified
    guidance-and-outcome pairs, so the guidance is available - it simply was not
    being fed back in.
    """
    from avws import calibration

    out: dict[str, float] = {}
    try:
        calib = calibration.measure(metric)
    except Exception:  # noqa: BLE001 - the backtest must never take down a run
        return out
    for line in calib.detail:
        # "FY2025Q3: guided 2880 -> actual 2880 (+0.00%)"
        try:
            period, rest = line.split(":", 1)
            guided = float(rest.split("guided", 1)[1].split("->")[0].strip())
        except (ValueError, IndexError):
            continue
        parsed = periods.parse(period)
        if parsed is not None:
            out[str(parsed)] = guided
    return out


def run(metric: Metric, facts: list[Fact] | None = None) -> Result:
    facts = facts if facts is not None else facts_for(metric.key)
    actuals = _actual_by_period(facts)
    ordered = sorted(actuals, key=periods.sort_key)
    guided_by_period = _historical_guidance(metric)

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

        # Reinstate the guidance the company actually issued for this period, so the
        # anchor path is exercised rather than silently skipped.
        guided = guided_by_period.get(str(target_period))
        if guided is not None:
            visible = visible + [Fact(
                metric_key=metric.key, company=metric.company,
                period=str(target_period), value=guided, unit=metric.units,
                basis="guidance_mid", source_doc="calibration pair",
                source_quote=f"guidance of {guided:g} issued for {target_period}",
                confidence=0.85,
            )]

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

    if outcomes:
        anchored = sum("guidance" in o.method for o in outcomes)
        _COVERAGE[metric.key] = (anchored, len(outcomes))

    return Result(metric_key=metric.key, outcomes=outcomes)


def run_guidance_path(metric: Metric) -> Result:
    """Backtest the GUIDANCE ANCHOR specifically, leave-one-out.

    The seasonal-path harness above could rarely exercise the anchor, because it
    needed a clean series target and the guidance issued beforehand to exist for the
    same period, and our two extraction passes produce those for different metrics.

    A calibration pair is already exactly what this test needs: a period, the
    guidance available before it, and the actual outturn. Running over those makes
    coverage complete by construction.

    Leave-one-out is the honest form: when scoring period P the residual is measured
    from every pair EXCEPT P, so the harness never sees the answer it is graded on.
    Scoring P with a residual that includes P would be measuring how well a number
    fits itself.
    """
    from avws import calibration

    try:
        calib = calibration.measure(metric)
    except Exception:  # noqa: BLE001
        return Result(metric_key=metric.key, outcomes=[])

    pairs: list[tuple[str, float, float]] = []
    for line in calib.detail or []:
        # Format: "FY2025Q3: guided 2880 -> actual 2900 (+0.69%)". The word "actual"
        # sits between the arrow and the number, which the first version of this
        # parser fed straight into float() - so every pair was silently discarded
        # and the harness reported "no usable history" for all twelve metrics.
        try:
            period, rest = line.split(":", 1)
            guided = float(rest.split("guided", 1)[1].split("->")[0].strip())
            after = rest.split("->", 1)[1].replace("actual", "")
            actual = float(after.split("(")[0].strip())
        except (ValueError, IndexError):
            continue
        pairs.append((period.strip(), guided, actual))

    outcomes: list[Outcome] = []
    for index, (period, guided, actual) in enumerate(pairs):
        others = [p for j, p in enumerate(pairs) if j != index]
        if not others:
            continue
        residuals = [(a - g) / abs(g) for _p, g, a in others if g]
        residual = statistics.median(residuals) if residuals else 0.0

        predicted = guided * (1 + residual)
        error = abs(predicted - actual)
        floor = metric.floor(actual)
        outcomes.append(Outcome(
            period=period, actual=actual, predicted=predicted,
            method="guidance_anchor_loo", error=error, floor=floor,
            within_floor=error < floor,
        ))

    if outcomes:
        _COVERAGE[metric.key] = (len(outcomes), len(outcomes))
    return Result(metric_key=metric.key, outcomes=outcomes)


def run_all() -> list[Result]:
    return [run(metric) for metric in load_metrics()]


def run_all_guidance() -> list[Result]:
    return [run_guidance_path(metric) for metric in load_metrics()]


def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Replay the estimator chain over known historical periods."
    )
    parser.add_argument("--metric", help="single metric key, e.g. 'ADI:Revenue'")
    parser.add_argument("--path", choices=["seasonal", "guidance"], default="seasonal",
                        help="which estimator path to exercise")
    args = parser.parse_args()

    runner = run_guidance_path if args.path == "guidance" else run
    if args.metric:
        results = [runner(get_metric(args.metric))]
    else:
        results = run_all_guidance() if args.path == "guidance" else run_all()
    print(f"path under test: {args.path}\n")

    print(f"{'metric':<48} {'n':>3} {'MAE':>10} {'median':>10} {'floor hit':>10} "
          f"{'guided':>8}")
    print("-" * 94)
    for result in results:
        if not result.n:
            print(f"{result.metric_key:<48} {'-':>3} {'no usable history':>32}")
            continue
        anchored, total = _COVERAGE.get(result.metric_key, (0, result.n))
        print(f"{result.metric_key:<48} {result.n:>3} {result.mae:>10.3f} "
              f"{result.median_error:>10.3f} {result.floor_hit_rate:>9.0%} "
              f"{anchored}/{total:>6}")

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

