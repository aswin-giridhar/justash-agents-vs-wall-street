"""Deterministic fact extraction from filing tables.

`tables.py` gave us structured rows; this module turns them into ledger facts
without a model call. It exists because the language model missed ADI's adjusted
gross margin entirely on the first end-to-end run, even though the 8-K contains

    | Adjusted gross margin percentage | 73.0 | % | 69.4 | % | 360 bps |

as a clean table row. Retrieval ranking and model attention are both probabilistic;
a row label match is not. Running both paths and merging is strictly more reliable
than either alone, and costs no tokens.

Column convention in these filings: the first numeric cell is the current period
and the second is the prior-year comparative. Both are recorded, which doubles the
history available for year-on-year growth and scale bands.
"""

from __future__ import annotations

from avws import periods
from avws.corpus import Doc, filter_docs
from avws.ledger import Fact
from avws.registry import Metric
from avws.tables import find_rows, parse_pipe_tables, row_numbers

# Row labels as they actually appear in each company's statements, mapped to our
# metric keys. Order matters: the first alias that matches a row wins.
ALIASES: dict[str, tuple[str, ...]] = {
    "HD:Net sales": ("Net sales",),
    "HD:Adjusted diluted EPS": (
        "Adjusted diluted earnings per share", "Adjusted diluted EPS",
    ),
    "HD:Comparable sales, total company": (
        "Comparable sales", "Total company comparable sales",
    ),
    "ADI:Revenue": ("Revenue",),
    "ADI:Adjusted diluted EPS": ("Adjusted diluted earnings per share",),
    "ADI:Adjusted gross margin": ("Adjusted gross margin percentage",),
    "HAS:Net fees": ("Net fees",),
    "HAS:Pre-exceptional basic EPS": (
        "Basic earnings per share", "Basic EPS",
    ),
    "HAS:Pre-exceptional operating profit": (
        "Operating profit", "Pre-exceptional operating profit",
    ),
    "DE:Worldwide net sales and revenues": (
        "Total net sales and revenues", "Worldwide net sales and revenues",
    ),
    "DE:Diluted EPS (GAAP)": (
        "Net income per share - diluted", "Diluted earnings per share",
        "Per share - diluted",
    ),
    # Deere publishes a segment sales row and a segment operating-profit row whose
    # labels both begin "Production & precision ag". The operating-profit table is
    # reached via the LLM path; the alias here is kept narrow so the sales row does
    # not masquerade as profit.
    "DE:Production & Precision Ag operating profit": (
        "Production & precision ag", "Production and precision ag",
    ),
}

_ADJUSTED_MARKERS = ("adjusted", "pre-exceptional", "underlying", "non-gaap")

# Plausible bands in the metric's own reporting unit. These are explicit modelling
# assumptions, documented rather than implicit, and they do two jobs:
#
#   1. Reject stray cells. Statement rows carry percentage-change columns, note
#      references and header years; without a band, "2026" becomes a fact.
#   2. Detect and repair scale errors. ADI's 10-Q reports revenue in THOUSANDS
#      ($3,623,465) while our required unit is USDm. Left alone that is a 1000x
#      error, which under this scoring function costs the capped 5.0 - more than
#      several good forecasts earn back.
BANDS: dict[str, tuple[float, float]] = {
    "HD:Net sales": (20_000, 80_000),
    "HD:Adjusted diluted EPS": (0.5, 15.0),
    "HD:Comparable sales, total company": (-25.0, 25.0),
    "ADI:Revenue": (1_000, 12_000),
    "ADI:Adjusted diluted EPS": (0.3, 15.0),
    "ADI:Adjusted gross margin": (50.0, 90.0),
    "HAS:Net fees": (400, 1_600),
    "HAS:Pre-exceptional basic EPS": (0.1, 30.0),
    "HAS:Pre-exceptional operating profit": (5.0, 400.0),
    "DE:Worldwide net sales and revenues": (4_000, 25_000),
    "DE:Diluted EPS (GAAP)": (0.3, 20.0),
    "DE:Production & Precision Ag operating profit": (100, 5_000),
}

# Scale factors tried when a value falls outside its band. Filings report in
# units, thousands or millions depending on the statement.
_RESCALE = (1.0, 1e-3, 1e-6, 1e3)

# How much wider the fact-admission band is than the forecast-guard band, for money
# and EPS metrics only. Ten times either way still catches a 1000x scale error while
# admitting divisional, half-year and full-year components of the headline figure.
FACT_BAND_WIDENING = 10.0

# Percentage readings below this magnitude are almost certainly decimal fractions
# rather than percentage points. A genuine 0.05% comparable-sales figure is possible
# but vanishingly rare; a 0.05 that means 5% is common.
PERCENT_FRACTION_FLOOR = 0.2


def _basis_for(metric: Metric, row_label: str) -> str:
    text = f"{metric.label} {row_label}".lower()
    return "adjusted" if any(m in text for m in _ADJUSTED_MARKERS) else "reported"


def _looks_like_a_year(value: float) -> bool:
    return value == int(value) and 1990 <= value <= 2100


def normalise(metric: Metric, value: float) -> tuple[float, str] | None:
    """Bring a raw cell into the metric's reporting unit, or reject it.

    Returns (value, note) or None. The note records any rescaling applied so the
    evidence report shows it rather than hiding a silent correction.

    The band here admits *facts*, which include the components a build-up needs, so
    it is wider than the band that later guards the *forecast* in validate.py. Hays
    group net fees run near 972 GBPm while its divisional components run 116-355;
    a single band sized for the group rejected the very parts that sum to it.

    Money metrics widen by 10x either way. Percentages do not: a margin's components
    are still margins, and widening there would readmit the basis-point error that
    put Home Depot comparable sales at -40 percentage points.
    """
    if value == 0:
        return None

    band = BANDS.get(metric.key)
    if band is None:
        return (value, "")

    low, high = band
    if not metric.is_percentage:
        low, high = low / FACT_BAND_WIDENING, high * FACT_BAND_WIDENING
    elif abs(value) < PERCENT_FRACTION_FLOOR:
        # A percentage arriving as a decimal fraction. Home Depot comparable sales
        # came out at 0.073% because the estimator averaged 0.004 and 0.1504 -
        # readings of 0.4% and 15.04% expressed as fractions, both of which sit
        # inside a +/-25 band and so passed unchallenged. Reported comparable sales
        # and margins are never this close to zero; reject rather than guess which
        # convention was meant.
        return None
    # Column headers in these statements are bare years, and several metric bands
    # span 1990-2100, so "2026" would otherwise be recorded as a Deere segment
    # profit. Real reported figures in that range essentially always carry a
    # decimal; exact integers in the year range are rejected.
    if _looks_like_a_year(value):
        return None

    # Rescaling is only meaningful for money metrics, where filings genuinely
    # report in units, thousands or millions. A percentage is already in its unit:
    # "repairing" a -40% reading into -0.04% would invent a number rather than
    # correct one. Percentages are accepted or rejected, never rescaled.
    factors = (1.0,) if metric.is_percentage else _RESCALE

    for factor in factors:
        scaled = value * factor
        if low <= abs(scaled) <= high or (low <= scaled <= high):
            note = "" if factor == 1.0 else f"rescaled by {factor:g} into {metric.units}"
            return (scaled, note)
    return None


def harvest(metric: Metric, since: str = "2023-01-01", max_docs: int = 40) -> list[Fact]:
    aliases = ALIASES.get(metric.key)
    if not aliases:
        return []

    docs: list[Doc] = [
        d for d in filter_docs(ticker=metric.ticker, doc_type="FILING", since=since)
    ]
    docs.sort(key=lambda d: d.published_at, reverse=True)

    facts: list[Fact] = []
    for doc in docs[:max_docs]:
        try:
            text = doc.full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tables = parse_pipe_tables(text)
        if not tables:
            continue

        for alias in aliases:
            rows = find_rows(tables, alias)
            if not rows:
                continue
            for row in rows[:2]:
                head = next((c for c in row if c.strip()), "").lower()
                # Deere's segment sales table shares a label prefix with the
                # operating-profit table; the qualifier distinguishes them.
                if "sales and marketing" in head:
                    continue

                raw = row_numbers(row)
                # A results row is (current, prior) with at most a change column.
                # Long rows are multi-year series whose columns we cannot attribute
                # to periods, and a fact attached to the wrong period is worse than
                # no fact at all.
                if len(raw) > 5:
                    continue

                normalised = [
                    n for n in (normalise(metric, v) for v in raw) if n is not None
                ]
                if not normalised:
                    continue
                quote = " | ".join(cell for cell in row if cell.strip())[:300]
                doc_period = periods.parse(doc.period)
                basis = _basis_for(metric, alias)

                value, note = normalised[0]
                facts.append(Fact(
                    metric_key=metric.key, company=metric.company,
                    period=str(doc_period) if doc_period else doc.period,
                    value=value, unit=metric.units, basis=basis,
                    source_doc=doc.path, source_quote=quote,
                    confidence=0.75,
                    label=f"table row: {alias}" + (f" [{note}]" if note else ""),
                ))
                if len(normalised) >= 2 and doc_period:
                    prior_value, prior_note = normalised[1]
                    facts.append(Fact(
                        metric_key=metric.key, company=metric.company,
                        period=str(doc_period.prior_year()),
                        value=prior_value, unit=metric.units, basis=basis,
                        source_doc=doc.path, source_quote=quote,
                        confidence=0.6,
                        label=f"table row (prior-year column): {alias}"
                        + (f" [{prior_note}]" if prior_note else ""),
                    ))
            break  # first matching alias wins for this document

    return _dedupe(facts)


def _dedupe(facts: list[Fact]) -> list[Fact]:
    """Collapse identical (period, basis, value) readings from repeated filings.

    The same figure appears in the 8-K, the press release and the 10-Q. Keeping
    every copy inflates the ledger and biases any median taken across it.
    """
    seen: dict[tuple[str, str, float], Fact] = {}
    for fact in facts:
        key = (fact.period, fact.basis, round(fact.value, 6))
        if key not in seen or fact.confidence > seen[key].confidence:
            seen[key] = fact
    return sorted(seen.values(), key=lambda f: (periods.sort_key(f.period), f.basis))
