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

from avws import ledger, reconcile, signals, validate
from avws.config import LOG_DIR, ROOT, ensure_dirs
from avws.corpus import build_index
from avws.estimators import buildup, guidance, seasonal
from avws.extract import extract_facts
from avws.registry import Metric, metrics_for, tickers
from avws.report import write_report
from avws.workbook import write_workbook

_log_handle = None
_log_lock = threading.Lock()


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

    candidates = []

    # Post-guidance leading indicators. Guidance anchoring alone reproduces what
    # every team with the 8-K can do; the tilt is what moves us off the anchor with
    # cited evidence published after the guidance was issued.
    tilt = signals.measure(metric)
    log(f"  [{metric.key}] signal tilt {tilt.fraction:+.4f} "
        f"(cap {tilt.cap:.3f}) from {len(tilt.signals)} verified signals")
    for signal in tilt.signals[:4]:
        log(f"      signal {signal.direction} {signal.strength:.2f}: {signal.why[:110]}")

    anchor = guidance.estimate(metric.key, facts, metric.period)
    if anchor:
        anchor = signals.apply(anchor, metric, tilt)
        candidates.append(anchor)
        log(f"  [{metric.key}] guidance_anchor -> {anchor.value:.6g} "
            f"(method {anchor.method}, confidence {anchor.confidence:.2f})")
    else:
        log(f"  [{metric.key}] guidance_anchor -> no guidance fact, skipped")

    if metric.key in buildup.COMPOSITIONS:
        composed = buildup.estimate(metric.key, facts, metric.period)
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
    log(f"  [{metric.key}] reconciled -> {blended.value:.6g} via {blended.method}")
    return blended, facts, candidates


def run_company(ticker: str) -> dict:
    metrics = metrics_for(ticker)
    log(f"[{ticker}] {metrics[0].company} - {metrics[0].period}")

    estimates, facts_by_metric, candidates_by_metric = {}, {}, {}
    for metric in metrics:
        blended, facts, candidates = estimate_metric(metric)
        estimates[metric.label] = blended
        facts_by_metric[metric.key] = facts
        candidates_by_metric[metric.label] = candidates

    values = {label: est.value for label, est in estimates.items()}
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
    global _log_handle
    parser = argparse.ArgumentParser(description="Produce OpenStocks forecast workbooks.")
    parser.add_argument("--all", action="store_true", help="run all four companies")
    parser.add_argument("--ticker", choices=tickers(), help="run one company")
    parser.add_argument("--sequential", action="store_true",
                        help="disable concurrency; useful when reading the log live")
    args = parser.parse_args(argv)

    if not args.all and not args.ticker:
        parser.error("pass --all or --ticker")

    ensure_dirs()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    _log_handle = (LOG_DIR / f"run-{stamp}.log").open("w", encoding="utf-8")

    targets = tickers() if args.all else [args.ticker]
    log(f"AVWS run start; companies={targets}")
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
