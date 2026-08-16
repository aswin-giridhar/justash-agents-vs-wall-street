"""Final command: research four companies and write four forecast workbooks.

    python -m avws.run --all

Runs headlessly with no human input. Every stage is logged with a timestamp to
logs/run-<ts>.log, including the stages that found nothing, so the log evidences
what the system did rather than only what succeeded.

Order of work per company: extract facts for all three metrics, produce candidate
values, then run cross-metric identity checks (which need all three at once), then
gate each value, then write the workbook.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from avws import (
    calibration, consensus, decide, ledger, linkage, reconcile, series, signals,
    validate,
)
from avws.config import LOG_DIR, ROOT, ensure_dirs
from avws.corpus import build_index
from avws.estimators import buildup, guidance, seasonal
from avws import extract
from avws.extract import extract_facts
from avws.registry import Metric, metrics_for, tickers
from avws.report import write_report
from avws.workbook import write_workbook

_log_handle = None
_log_lock = threading.Lock()
_USE_SERIES_CACHE = True


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{stamp}  {message}"
    with _log_lock:
        print(line, flush=True)
        if _log_handle:
            _log_handle.write(line + "\n")
            _log_handle.flush()


def estimate_metric(metric: Metric) -> tuple[object, list, list]:
    """Extract, estimate and reconcile one metric. Returns (estimate, facts, all)."""
    log(f"  [{metric.key}] extracting evidence")
    facts, stats = extract_facts(metric)
    ledger.append_all(facts)
    log(f"  [{metric.key}] tables={stats.get('from_tables', 0)} "
        f"chunks={stats['chunks']} llm_returned={stats['returned']} "
        f"llm_kept={stats['kept']} rejected_unverifiable_quotes={stats['rejected_quotes']} "
        f"rejected_out_of_band={stats.get('rejected_out_of_band', 0)} "
        f"total_facts={len(facts)}")

    # A dedicated series extraction for the metric's own history. Opportunistic
    # harvesting finds components well and time series badly; this asks the question
    # directly instead of reconstructing it from row matches.
    series_facts, series_stats = series.fetch(metric, use_cache=_USE_SERIES_CACHE)
    ledger.append_all(series_facts)
    facts = facts + series_facts
    log(f"  [{metric.key}] series: returned={series_stats['returned']} "
        f"kept={series_stats['kept']} cached={series_stats.get('cached', False)} -> "
        + (", ".join(f"{f.period}={f.value:g}" for f in series_facts[-6:])
           or "none; falling back to harvested facts"))

    candidates = []

    # Post-guidance leading indicators. Guidance anchoring alone reproduces what
    # every team with the 8-K can do; the tilt is what moves us off the anchor with
    # cited evidence published after the guidance was issued.
    tilt = signals.measure(metric)
    log(f"  [{metric.key}] signal tilt {tilt.fraction:+.4f} "
        f"(cap {tilt.cap:.3f}) from {len(tilt.signals)} verified signals")
    for signal in tilt.signals[:4]:
        log(f"      signal {signal.direction} {signal.strength:.2f}: {signal.why[:110]}")

    # Guidance bias measured deliberately across the corpus rather than from
    # whatever pairs happened to surface while extracting this metric.
    calib = calibration.measure(metric, use_cache=_USE_SERIES_CACHE)
    log(f"  [{metric.key}] calibration: {calib.describe()}")

    anchor = guidance.estimate(
        metric.key, facts, metric.period,
        residual_pct=calib.residual if calib.measured else None,
        calibration_pairs=calib.pairs, calibration_detail=calib.detail,
    )
    if anchor:
        anchor = signals.apply(anchor, metric, tilt)
        candidates.append(anchor)
        log(f"  [{metric.key}] guidance_anchor -> {anchor.value:.6g} "
            f"(method {anchor.method}, confidence {anchor.confidence:.2f})")
    else:
        log(f"  [{metric.key}] guidance_anchor -> no guidance fact, skipped")

    if metric.key in buildup.COMPOSITIONS:
        composed = buildup.estimate(
            metric.key, facts, metric.period,
            documents=extract.guidance_documents(metric),
        )
        if composed:
            candidates.append(composed)
            if composed.confidence > 0:
                log(f"  [{metric.key}] build_up -> {composed.value:.6g}")
            else:
                log(f"  [{metric.key}] build_up -> unavailable: {composed.warnings}")
    else:
        log(f"  [{metric.key}] build_up -> no composition registered")

    fallback = seasonal.estimate(metric, facts)
    # Where there is no guidance to anchor on - Home Depot's and Deere's quarters -
    # the signal tilt is the only forward-looking input available, so it is applied
    # to the trend instead.
    if anchor is None:
        fallback = signals.apply(fallback, metric, tilt)
    candidates.append(fallback)
    log(f"  [{metric.key}] seasonal_trend -> {fallback.value:.6g} "
        f"(method {fallback.method})")

    blended = reconcile.combine(candidates, metric.key)

    # Decision layer: pick the value that minimises expected score rather than the
    # value that is "right on average", and record where consensus probably sits.
    weights = reconcile.weights_for(candidates)
    stated = consensus.find(metric, use_cache=_USE_SERIES_CACHE)
    log(f"  [{metric.key}] consensus: {stated.describe()}")
    decision = decide.choose(metric, candidates, weights, facts, stated=stated)
    blended.value = decision.value
    blended.derivation += f"\n  DECISION [{decision.method}]: {decision.describe()}"
    if decision.consensus_proxy is not None and abs(decision.deviation_pct) > 0.05:
        blended.warnings.append(
            f"deviating {decision.deviation_pct:+.1%} from the consensus proxy"
        )
    log(f"  [{metric.key}] decision -> {decision.value:.6g} via {decision.method} "
        f"(spread {decision.spread_pct:.1%}, consensus proxy "
        f"{decision.consensus_proxy if decision.consensus_proxy is not None else 'n/a'})")
    return blended, facts, candidates


def run_company(ticker: str) -> dict:
    metrics = metrics_for(ticker)
    log(f"[{ticker}] {metrics[0].company} - {metrics[0].period}")

    estimates, facts_by_metric, candidates_by_metric = {}, {}, {}
    # The three metrics of a company are independent until the identity checks,
    # which need all three at once, so they run concurrently up to that barrier.
    with ThreadPoolExecutor(max_workers=len(metrics)) as pool:
        futures = {pool.submit(estimate_metric, m): m for m in metrics}
        for future in as_completed(futures):
            metric = futures[future]
            blended, facts, candidates = future.result()
            estimates[metric.label] = blended
            facts_by_metric[metric.key] = facts
            candidates_by_metric[metric.label] = candidates

    values = {label: est.value for label, est in estimates.items()}

    # Linked P&L derivation. Estimating EPS independently of the operating profit
    # above it allows contradictions that a real analyst's linked model forbids by
    # construction: 44.5m of operating profit over 1,600m shares cannot support
    # 36.6 pence. Where the chain is complete we derive rather than estimate.
    # Metrics the company guides directly. Guidance outranks our own derivation of
    # the same number, so for those the linked chain is a check, not a substitution.
    guided_labels = {
        m.label for m in metrics
        if any(f.basis.startswith("guidance") and f.period == m.period
               for f in facts_by_metric.get(m.key, []))
    }
    values, derivations, linkage_notes = linkage.apply(
        ticker, metrics[0].company, values, guided_labels=guided_labels
    )
    for note in linkage_notes:
        log(f"  [{ticker}] LINKAGE {note}")
    for derivation in derivations:
        label = derivation.metric_key.split(":", 1)[1]
        estimates[label].value = derivation.derived_value
        estimates[label].method += "+derived"
        estimates[label].derivation += (
            f"\n  DERIVED FROM THE P&L: {derivation.arithmetic}"
            f"\n  independent estimate was {derivation.independent_value:.4g} "
            f"({derivation.divergence:.0%} divergence); the derived value is "
            f"submitted because it is forced consistent with operating profit, "
            f"share count and tax"
        )

    identity_issues = validate.check_identities(ticker, values, facts_by_metric)
    if identity_issues:
        for issue in identity_issues:
            log(f"  [{ticker}] IDENTITY {issue}")
    else:
        log(f"  [{ticker}] identity checks: no violations")

    findings_by_metric = {}
    for metric in metrics:
        est = estimates[metric.label]
        history = ledger.history(metric.key)
        issues = list(identity_issues)
        issues += validate.check_guidance_consistency(
            metric, est.value, facts_by_metric[metric.key]
        )
        est, findings = validate.gate(metric, est, history, issues)
        findings_by_metric[metric.label] = findings
        for finding in findings:
            log(f"  [{metric.key}] GATE {finding}")
        estimates[metric.label] = est

    final = {label: est.value for label, est in estimates.items()}
    path = write_workbook(ticker, final)
    log(f"[{ticker}] wrote {path.relative_to(ROOT)}")

    report = write_report(ticker, estimates, candidates_by_metric,
                          facts_by_metric, findings_by_metric, identity_issues)
    log(f"[{ticker}] wrote {report.relative_to(ROOT)}")

    return {
        "ticker": ticker,
        "period": metrics[0].period,
        "values": final,
        "derivations": {l: e.derivation for l, e in estimates.items()},
        "methods": {l: e.method for l, e in estimates.items()},
        "warnings": {l: e.warnings for l, e in estimates.items()},
        "findings": findings_by_metric,
        "identity_issues": identity_issues,
    }


def main(argv: list[str] | None = None) -> int:
    global _log_handle, _USE_SERIES_CACHE
    parser = argparse.ArgumentParser(description="Produce OpenStocks forecast workbooks.")
    parser.add_argument("--all", action="store_true", help="run all four companies")
    parser.add_argument("--ticker", choices=tickers(), help="run one company")
    parser.add_argument("--sequential", action="store_true",
                        help="disable concurrency; useful when reading the log live")
    parser.add_argument("--fresh", action="store_true",
                        help="ignore the cached historical series and re-extract it")
    args = parser.parse_args(argv)

    if not args.all and not args.ticker:
        parser.error("pass --all or --ticker")

    _USE_SERIES_CACHE = not args.fresh
    ensure_dirs()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    _log_handle = (LOG_DIR / f"run-{stamp}.log").open("w", encoding="utf-8")

    targets = tickers() if args.all else [args.ticker]
    from avws.config import REASONING_MODEL, TRANSCRIPTION_MODEL

    log(f"AVWS run start; companies={targets}")
    log(f"models: transcription={TRANSCRIPTION_MODEL} reasoning={REASONING_MODEL}; "
        f"series_cache={'off (--fresh)' if args.fresh else 'on'}")
    log("building corpus index")
    docs = build_index()
    log(f"corpus indexed: {len(docs)} documents")
    ledger.reset()
    log("evidence ledger reset")

    results, failures = [], []
    if args.sequential or len(targets) == 1:
        log("running companies sequentially")
        for ticker in targets:
            try:
                results.append(run_company(ticker))
            except Exception:  # noqa: BLE001
                # One company failing must not lose the other three. The failure is
                # recorded loudly; never converted into a silent empty result.
                failures.append(ticker)
                log(f"[{ticker}] FAILED\n{traceback.format_exc()}")
    else:
        # The final-run window is 45 minutes and a sequential pass takes most of it,
        # leaving no room to retry after a crash. Companies are independent up to
        # the shared ledger file, whose writes are locked.
        log(f"running {len(targets)} companies concurrently")
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            futures = {pool.submit(run_company, t): t for t in targets}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    results.append(future.result())
                except Exception:  # noqa: BLE001
                    failures.append(ticker)
                    log(f"[{ticker}] FAILED\n{traceback.format_exc()}")
    results.sort(key=lambda r: targets.index(r["ticker"]))

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "companies": results,
        "failed": failures,
    }
    (ROOT / "forecasts.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )

    from avws.llm import call_count

    log(f"run complete: {len(results)} companies written, {len(failures)} failed, "
        f"{call_count()} model calls")
    if failures:
        log(f"INCOMPLETE RUN - these companies produced no workbook: {failures}")
    _log_handle.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

