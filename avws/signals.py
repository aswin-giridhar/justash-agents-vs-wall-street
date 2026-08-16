"""Post-guidance and macro signal extraction.

Guidance anchoring is the obvious move: every team with the 8-K will do it, and a
forecast that equals consensus scores exactly 1.0. The edge has to come from what
moves you off the anchor with evidence.

Two sources of that evidence exist inside the frozen corpus:

1. **Post-guidance documents.** Guidance is issued on one date; the corpus contains
   documents published after it. ADI guided Q3 on 20 May 2026 and there is a
   conference transcript from 2 June 2026. Deere's Q2 call was 21 May with an
   investor presentation on 26 May. That window is strictly newer information than
   the anchor.

2. **Leading indicators inside the results themselves.** Bookings, backlog, order
   rates, channel inventory, pricing and end-market demand lead reported revenue.
   ADI's own Q2 release notes "record bookings across our B2B markets of Industrial,
   Automotive, and Communications" - about the quarter we are forecasting.

The output is a bounded multiplicative tilt, not an independent forecast. It cannot
run away with the number: caps are deliberately tight because the scoring function
punishes a large miss ten times harder than it rewards a small win.
"""

from __future__ import annotations

from dataclasses import dataclass

from avws.corpus import build_index, filter_docs, search
from avws.llm import complete
from avws.registry import Metric

# Maximum tilt, as a fraction of the anchor. A signal read from management tone is
# real information but weak information; it should nudge a forecast, never drive it.
# Percentage metrics are tilted ADDITIVELY in percentage points; level metrics
# multiplicatively as a fraction. Multiplying a 73% margin by 1.01 is not the same
# claim as adding a point to it, and only the additive reading is meaningful.
MAX_TILT = {
    "revenue": 0.025,     # +/- 2.5% on a top-line figure
    "eps": 0.06,          # EPS carries operating leverage, so the same demand
                          # signal moves it further than revenue
    "percentage": 1.0,    # +/- 1.0 percentage point on a margin or growth rate
}

SIGNAL_SYSTEM = """You read company disclosure for LEADING INDICATORS of a coming quarter.

You are given documents published AFTER the company issued guidance for the target
period, plus recent results commentary. Identify signals that suggest the target
period will come in ABOVE or BELOW the guidance midpoint.

Count as signals:
- EXPLICIT POSITIONING AGAINST CONSENSUS. This is the strongest signal available and
  the most commonly overlooked. Companies say things like "at the top of the
  consensus range", "in line with market expectations", "ahead of consensus", "below
  the range". That is a direct statement about the sign of the surprise from the only
  party that already knows. Treat "at the top of the range" or "ahead of" as a strong
  ABOVE signal (strength 0.8-1.0), "below" as a strong BELOW signal, and "in line" as
  no signal at all. Where a company issued one such statement and later issued a
  DIFFERENT one, the LATER statement supersedes the earlier: an upgrade from "in line"
  to "top of the range" is a strong above signal.
- bookings, orders, backlog, book-to-bill, cancellations
- channel or dealer inventory building or depleting
- end-market demand commentary by segment
- pricing, cost, tariff and foreign-exchange commentary
- management tone changes between the guidance date and later appearances
- macro conditions the company itself names as driving its business

Do NOT count:
- restatements of the guidance itself
- generic strategy or long-term vision language
- anything about a period other than the target

For each signal give:
- `direction`: "above" or "below"
- `strength`: 0.0 to 1.0, where 1.0 is an explicit quantified statement about the
  target period and 0.2 is vague qualitative tone
- `quote`: copied EXACTLY from the supplied text
- `why`: one sentence connecting the signal to the target metric

Be sparing. Three well-evidenced signals beat ten weak ones. If the documents
contain no genuine leading indicator, return an empty list."""

SIGNAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["signals", "net_assessment"],
    "properties": {
        "signals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["direction", "strength", "quote", "why"],
                "properties": {
                    "direction": {"type": "string", "enum": ["above", "below"]},
                    "strength": {"type": "number"},
                    "quote": {"type": "string"},
                    "why": {"type": "string"},
                },
            },
        },
        "net_assessment": {"type": "string"},
    },
}


@dataclass
class Signal:
    direction: str
    strength: float
    quote: str
    why: str

    @property
    def signed(self) -> float:
        return self.strength if self.direction == "above" else -self.strength


@dataclass
class Tilt:
    fraction: float
    signals: list[Signal]
    assessment: str
    cap: float
    docs: list[str]

    @property
    def applied(self) -> bool:
        return abs(self.fraction) > 1e-9

    def describe(self) -> str:
        if not self.signals:
            return "post-guidance signal: none found; anchor left unadjusted"
        parts = [
            f"{'+' if s.signed > 0 else '-'}{abs(s.signed):.2f} {s.why}"
            for s in self.signals
        ]
        return (
            f"post-guidance signal tilt {self.fraction:+.4f} "
            f"(capped at +/-{self.cap:.3f}) from {len(self.signals)} signals: "
            + "; ".join(parts)
        )


def _cap_for(metric: Metric) -> float:
    if metric.is_percentage:
        return MAX_TILT["percentage"]
    if metric.is_eps:
        return MAX_TILT["eps"]
    return MAX_TILT["revenue"]


def _anchor_date(metric: Metric) -> str:
    """Publication date of the most recent results filing - the guidance date."""
    docs = filter_docs(ticker=metric.ticker, doc_type="FILING")
    dated = sorted((d.published_at for d in docs if d.published_at), reverse=True)
    return dated[0] if dated else "2026-01-01"


def measure(metric: Metric, max_chunks: int = 14) -> Tilt:
    """Read post-guidance disclosure and return a bounded tilt on the anchor."""
    build_index()
    cap = _cap_for(metric)
    anchor_date = _anchor_date(metric)

    queries = [
        # Consensus positioning first: it is the most explicit directional statement
        # a company ever makes, and the easiest to miss because it is one sentence
        # of prose rather than a number in a table.
        ("expect to be at the top of the consensus range in line with market "
         "expectations ahead of consensus", None),
        ("bookings orders backlog book-to-bill demand strengthening", None),
        ("end market demand outlook industrial automotive consumer conditions", None),
        ("pricing tariffs foreign exchange cost headwind tailwind", None),
        ("channel inventory dealer inventory destocking restocking", None),
    ]

    collected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for query, doc_type in queries:
        # Look back six months from the latest filing so we capture the results
        # commentary as well as anything published after it.
        for doc, _score, chunk in search(
            query, ticker=metric.ticker, doc_type=doc_type,
            since=_six_months_before(anchor_date), k=4,
        ):
            key = f"{doc.path}:{hash(chunk)}"
            if key in seen:
                continue
            seen.add(key)
            collected.append((f"{doc.path} | published {doc.published_at} | "
                              f"{doc.document_type}", chunk))

    collected = collected[:max_chunks]
    if not collected:
        return Tilt(0.0, [], "no post-guidance documents retrieved", cap, [])

    body = "\n\n".join(f"<<<{header}>>>\n{chunk}" for header, chunk in collected)
    user = (
        f"Company: {metric.company} ({metric.ticker})\n"
        f"Target metric: {metric.label} ({metric.units})\n"
        f"Target period: {metric.period}\n"
        f"Guidance issued on or around: {anchor_date}\n\n"
        f"Documents:\n{body}"
    )

    try:
        payload = complete(SIGNAL_SYSTEM, user, SIGNAL_SCHEMA, schema_name="signals")
    except Exception as exc:  # noqa: BLE001
        return Tilt(0.0, [], f"signal read failed: {type(exc).__name__}", cap, [])

    haystack = " ".join(chunk for _h, chunk in collected).lower()
    signals: list[Signal] = []
    for item in payload.get("signals", []):
        quote = (item.get("quote") or "").strip()
        # Same anti-fabrication rule as fact extraction: the quote must exist.
        if not quote or " ".join(quote.lower().split())[:60] not in " ".join(
            haystack.split()
        ):
            continue
        strength = max(0.0, min(1.0, float(item.get("strength", 0.0))))
        signals.append(Signal(item["direction"], strength, quote, item.get("why", "")))

    if not signals:
        return Tilt(0.0, [], payload.get("net_assessment", "no verifiable signals"),
                    cap, [h for h, _c in collected])

    # Mean signed strength, then scaled to the cap. Using the mean rather than the
    # sum means ten weak signals cannot outvote the cap by sheer count.
    mean_signed = sum(s.signed for s in signals) / len(signals)
    fraction = max(-cap, min(cap, mean_signed * cap))

    return Tilt(
        fraction=fraction,
        signals=signals,
        assessment=payload.get("net_assessment", ""),
        cap=cap,
        docs=[h for h, _c in collected],
    )


def apply(estimate, metric: Metric, tilt: Tilt):
    """Return a new Estimate with the tilt applied, or the original if none."""
    from avws.estimators.base import Estimate

    if estimate is None or not tilt.applied:
        if estimate is not None and tilt.signals is not None:
            estimate.derivation += f"\n  {tilt.describe()}"
        return estimate

    if metric.is_percentage:
        value = estimate.value + tilt.fraction
        arithmetic = (
            f"{estimate.value:.6g} {metric.units} {tilt.fraction:+.3f}pp "
            f"post-guidance signal = {value:.6g}"
        )
    else:
        value = estimate.value * (1 + tilt.fraction)
        arithmetic = (
            f"{estimate.value:.6g} x (1 {tilt.fraction:+.4f} post-guidance signal) "
            f"= {value:.6g}"
        )

    quotes = "\n    ".join(
        f'{s.direction} ({s.strength:.2f}): "{s.quote[:150]}"' for s in tilt.signals
    )
    return Estimate(
        metric_key=estimate.metric_key,
        value=value,
        method=f"{estimate.method}+signal",
        derivation=(
            f"{estimate.derivation}\n  {arithmetic}\n  {tilt.describe()}\n    {quotes}"
        ),
        assumptions={**estimate.assumptions, "signal_tilt": tilt.fraction},
        inputs=estimate.inputs,
        confidence=min(0.9, estimate.confidence + 0.05),
        warnings=list(estimate.warnings),
    )


def _six_months_before(date: str) -> str:
    try:
        year, month, day = (int(p) for p in date.split("-"))
    except ValueError:
        return "2025-01-01"
    month -= 6
    if month <= 0:
        month += 12
        year -= 1
    return f"{year:04d}-{month:02d}-{day:02d}"
