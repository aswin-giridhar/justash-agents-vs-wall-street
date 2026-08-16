"""Focused historical-series extraction.

Everything else in the pipeline harvests facts opportunistically: table rows that
match a label, figures mentioned in prose. That is the right way to gather
*components* for a build-up, because you do not know in advance which line you will
need.

It is the wrong way to build the *time series of the metric itself*. Row labels are
ambiguous ("Operating profit" appears in three tables), columns are ambiguous
(current period, prior period, year-to-date, percentage change), and every fix for
one class of junk admits another. Three rounds of tightening bands produced Hays
EPS at 36 pence against a true level near 1.5, and Deere segment operating profit
at 378 against a true level in the low thousands.

So this module asks the question directly instead: for this exact metric, on this
exact basis, give me the reported value for each of the last N comparable periods,
each with a verbatim quote. One call, one purpose, strictly typed.

Comparability is enforced, not requested: a quarterly target accepts only the same
quarter of other years, and a full-year target only full years.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from avws import periods
from avws.config import CACHE_DIR, TRANSCRIPTION_MODEL
from avws.corpus import build_index, search
from avws.ledger import Fact
from avws.llm import complete
from avws.registry import Metric
from avws.table_facts import BANDS

SERIES_CACHE = CACHE_DIR / "series"

SERIES_SYSTEM = """You build the historical time series of ONE specific financial metric.

You are given a metric, its unit, and excerpts from a company's filings. Return the
company's REPORTED value of that exact metric for as many past periods as the
excerpts support.

Absolute rules:
1. Only the metric named. Not a similar line, not a component, not a segment of it,
   not a year-to-date figure when a quarter was asked for.
2. Match the BASIS exactly. If the metric says "adjusted" or "pre-exceptional",
   return the adjusted or pre-exceptional figure, never the statutory one. If it
   says "(GAAP)", return the GAAP figure, never the adjusted one.
3. Match the PERIOD TYPE exactly. If asked for a quarter, return quarters. If asked
   for a full year, return full years. Never mix them.
4. Convert to the requested unit. Billions to millions when millions are asked for.
   Percentages as percentage points (73.0, not 0.73). Pence as pence (6.2, not
   0.062). Thousands to millions when the filing reports in thousands.
5. Every value needs a `source_quote` copied EXACTLY from the excerpts.
6. Return nothing rather than something approximate. A missing period is
   recoverable; a wrong one silently corrupts every growth rate computed from it.

Period labels use the company's own fiscal convention: "FY2025Q3", "FY2025"."""

SERIES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["series"],
    "properties": {
        "series": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["period", "value", "source_quote", "confidence"],
                "properties": {
                    "period": {"type": "string"},
                    "value": {"type": "number"},
                    "source_quote": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        }
    },
}


# Metrics whose history does not live where the generic queries look. Deere reports
# segment results in a segment table and its earnings deck rather than the headline
# summary; Hays reports group net fees in a divisional table in the annual results.
# Both returned an empty series until pointed at the right documents.
EXTRA_QUERIES: dict[str, tuple[str, ...]] = {
    "DE:Production & Precision Ag operating profit": (
        "production and precision agriculture segment operating profit net sales",
        "segment results operating profit production precision ag small agriculture "
        "turf construction forestry",
    ),
    "DE:Worldwide net sales and revenues": (
        "worldwide net sales and revenues for the third quarter",
    ),
    "DE:Diluted EPS (GAAP)": (
        "net income attributable to Deere and Company per share diluted third quarter",
        "third quarter net income per diluted share results",
    ),
    "HAS:Net fees": (
        "group net fees for the year ended 30 June actual reported",
        "net fees Germany United Kingdom Ireland Australia New Zealand Rest of World "
        "group total",
        "turnover net fees Germany United Kingdom Ireland Australia New Zealand "
        "Rest of World Group note segment information",
        "net fees decreased year ended 30 June group net fees were",
    ),
    "HAS:Pre-exceptional operating profit": (
        "pre-exceptional operating profit for the year ended 30 June group",
    ),
    "HD:Net sales": (
        "net sales for the second quarter of fiscal increased",
    ),
}


def _normalise_ws(text: str) -> str:
    return " ".join(text.split()).lower()


def _cache_path(metric: Metric) -> Path:
    digest = hashlib.sha256(
        (metric.key + metric.period + SERIES_SYSTEM).encode()
    ).hexdigest()[:12]
    return SERIES_CACHE / f"{metric.ticker}-{digest}.json"


def fetch(metric: Metric, k: int = 10, use_cache: bool = True) -> tuple[list[Fact], dict]:
    """Return high-confidence series facts for the metric, plus stats.

    Results are cached because a company's reported history cannot change between
    runs. The cache key includes the prompt, so editing the prompt invalidates it
    rather than silently serving stale output.
    """
    cache_file = _cache_path(metric)
    if use_cache and cache_file.exists():
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        facts = [Fact(**row) for row in payload["facts"]]
        return facts, {**payload["stats"], "cached": True}

    build_index()
    target = periods.parse(metric.period)

    # Segment-level metrics live in segment tables and earnings decks, not in the
    # headline results summary, so the search is not restricted to filings. Deere's
    # Production & Precision Ag operating profit returned nothing at all until the
    # slide corpus was included.
    queries = [
        (f"{metric.label} results summary for the period", "FILING"),
        (f"{metric.label} compared with prior year same period", "FILING"),
        (f"selected financial data {metric.label} historical", "FILING"),
        (f"{metric.label} operating profit by segment net sales", None),
        (f"{metric.label}", None),
    ]
    queries += [(q, None) for q in EXTRA_QUERIES.get(metric.key, ())]
    chunks: list[tuple[str, str]] = []
    seen: set[str] = set()
    for query, doc_type in queries:
        for doc, _score, chunk in search(
            query, ticker=metric.ticker, doc_type=doc_type, since="2021-01-01", k=k
        ):
            key = f"{doc.path}:{hash(chunk)}"
            if key in seen:
                continue
            seen.add(key)
            chunks.append((f"{doc.path} | published {doc.published_at} | "
                           f"period {doc.period}", chunk))

    chunks = chunks[:30]
    if not chunks:
        return [], {"chunks": 0, "returned": 0, "kept": 0}

    period_kind = (
        f"quarterly, specifically Q{target.quarter} of each fiscal year"
        if target and target.quarter
        else "full fiscal years"
    )
    body = "\n\n".join(f"<<<{header}>>>\n{chunk}" for header, chunk in chunks)
    user = (
        f"Company: {metric.company} ({metric.ticker})\n"
        f"Metric: {metric.label}\n"
        f"Unit required: {metric.units}\n"
        f"Period type required: {period_kind}\n"
        f"Target period we are forecasting (do NOT return it): {metric.period}\n\n"
        f"Return the reported historical values of this metric.\n\n{body}"
    )

    payload = complete(SERIES_SYSTEM, user, SERIES_SCHEMA, schema_name="series",
                       model=TRANSCRIPTION_MODEL)
    raw = payload.get("series", [])

    haystack = _normalise_ws(body)
    band = BANDS.get(metric.key)
    basis = "adjusted" if any(
        marker in metric.label.lower()
        for marker in ("adjusted", "pre-exceptional", "underlying")
    ) else "reported"

    kept: list[Fact] = []
    for item in raw:
        quote = (item.get("source_quote") or "").strip()
        if not quote or _normalise_ws(quote)[:60] not in haystack:
            continue
        period = periods.parse(item.get("period", ""))
        if period is None:
            continue
        # Comparability enforced here, not merely asked for in the prompt.
        if target is not None and period.quarter != target.quarter:
            continue
        if target is not None and period == target:
            continue  # the target has no reported value yet
        value = float(item["value"])
        if band and not (band[0] <= abs(value) <= band[1]):
            continue
        kept.append(Fact(
            metric_key=metric.key, company=metric.company, period=str(period),
            value=value, unit=metric.units, basis=basis,
            source_doc=item.get("period", "series"), source_quote=quote,
            confidence=min(0.95, max(0.8, float(item.get("confidence", 0.9)))),
            label="historical series",
        ))

    # One value per period, highest confidence wins.
    best: dict[str, Fact] = {}
    for fact in kept:
        if fact.period not in best or fact.confidence > best[fact.period].confidence:
            best[fact.period] = fact
    ordered = sorted(best.values(), key=lambda f: periods.sort_key(f.period))

    stats = {"chunks": len(chunks), "returned": len(raw), "kept": len(ordered),
             "cached": False}

    SERIES_CACHE.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps({"stats": stats, "facts": [asdict(f) for f in ordered]}, indent=1),
        encoding="utf-8",
    )
    return ordered, stats
