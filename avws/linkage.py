"""Linked P&L derivation.

The structural weakness of estimating twelve numbers independently and then
checking them for consistency is that consistency is *checked* rather than
*structural*. A sell-side analyst does not estimate EPS. They build one linked
model - revenue, gross margin, operating expenses, operating profit, net finance,
tax, share count - and EPS falls out of it. An EPS inconsistent with the operating
profit above it is not merely caught; it is unrepresentable.

Our system produced Hays pre-exceptional basic EPS of 36.6 pence alongside
pre-exceptional operating profit of GBP 44.5m. With roughly 1,600m shares in issue
those two numbers cannot both be true: 44.5m of operating profit supports about
1.3 pence, not 36.6. Independent estimation allowed the contradiction; a linked
model forbids it.

This module derives the derivable metrics from their drivers, and reports both the
derived value and the independent estimate so the gap is visible rather than
silently resolved.
"""

from __future__ import annotations

from dataclasses import dataclass

from avws.ledger import Fact
from avws.llm import complete
from avws.registry import Metric, metrics_for

DRIVER_SYSTEM = """You extract the profit-and-loss drivers needed to derive earnings per share.

You are given filings excerpts for one company and one fiscal period. Return the
drivers listed, using the most recent reliable figure for each.

Rules:
1. Every value needs a `source_quote` copied EXACTLY from the excerpts.
2. If a driver is genuinely absent, set `available` to false. Do NOT invent one.
   A missing driver is recoverable; a fabricated one corrupts the whole derivation.
3. Share counts are in MILLIONS of shares.
4. Tax rate is a percentage (24.5 means 24.5%).
5. Net finance charge is a POSITIVE number representing a cost to be subtracted.
   If the company earns net finance income, return it as a negative number.
6. Prefer the basis the metric requires: pre-exceptional or adjusted figures where
   the company reports them, statutory otherwise. Say which in `basis_note`."""

DRIVER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["drivers"],
    "properties": {
        "drivers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "value", "available", "source_quote", "basis_note"],
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": [
                            "weighted_average_basic_shares_m",
                            "weighted_average_diluted_shares_m",
                            "net_finance_charge",
                            "effective_tax_rate_pct",
                            "minority_interest",
                        ],
                    },
                    "value": {"type": "number"},
                    "available": {"type": "boolean"},
                    "source_quote": {"type": "string"},
                    "basis_note": {"type": "string"},
                },
            },
        }
    },
}


@dataclass
class Derivation:
    metric_key: str
    derived_value: float
    independent_value: float
    arithmetic: str
    drivers: dict[str, float]

    @property
    def divergence(self) -> float:
        base = abs(self.independent_value) or 1.0
        return abs(self.derived_value - self.independent_value) / base


def fetch_drivers(ticker: str, company: str) -> tuple[dict[str, float], list[str]]:
    """Extract share count, tax rate and net finance charge once per company."""
    from avws.corpus import build_index, search

    build_index()
    chunks = []
    seen = set()
    for query in (
        "weighted average number of shares basic diluted earnings per share",
        "net finance charge interest payable receivable",
        "effective tax rate income tax expense profit before tax",
    ):
        for doc, _s, chunk in search(query, ticker=ticker, doc_type="FILING",
                                    since="2024-06-01", k=5):
            key = f"{doc.path}:{hash(chunk)}"
            if key in seen:
                continue
            seen.add(key)
            chunks.append(f"<<<{doc.path} | {doc.published_at} | {doc.period}>>>\n{chunk}")

    if not chunks:
        return {}, ["no driver documents retrieved"]

    body = "\n\n".join(chunks[:18])
    haystack = " ".join(body.split()).lower()
    payload = complete(
        DRIVER_SYSTEM,
        f"Company: {company} ({ticker})\n\nExcerpts:\n{body}",
        DRIVER_SCHEMA, schema_name="drivers",
    )

    drivers: dict[str, float] = {}
    notes: list[str] = []
    for item in payload.get("drivers", []):
        if not item.get("available"):
            continue
        quote = " ".join((item.get("source_quote") or "").split()).lower()
        if not quote or quote[:50] not in haystack:
            notes.append(f"{item['name']}: quote not verifiable, discarded")
            continue
        drivers[item["name"]] = float(item["value"])
        notes.append(f"{item['name']} = {item['value']:g} ({item.get('basis_note','')})")
    return drivers, notes


def derive_eps(
    ticker: str, values: dict[str, float], drivers: dict[str, float]
) -> Derivation | None:
    """Derive the company's EPS metric from its operating profit and drivers.

    Only implemented where the company's own reported metric set makes the chain
    complete. Hays is the clean case: we forecast pre-exceptional operating profit
    and the EPS metric is on the same pre-exceptional basis, so the bridge is exact.
    """
    if ticker != "HAS":
        return None

    operating_profit = values.get("Pre-exceptional operating profit")
    independent = values.get("Pre-exceptional basic EPS")
    shares = drivers.get("weighted_average_basic_shares_m")
    if operating_profit is None or independent is None or not shares:
        return None

    finance = drivers.get("net_finance_charge", 0.0)
    tax_rate = drivers.get("effective_tax_rate_pct", 25.0)

    profit_before_tax = operating_profit - finance
    profit_after_tax = profit_before_tax * (1 - tax_rate / 100.0)
    derived = profit_after_tax / shares * 100.0  # GBPm -> pence per share

    arithmetic = (
        f"({operating_profit:.4g} operating profit - {finance:.4g} net finance) "
        f"x (1 - {tax_rate:.4g}% tax) / {shares:.6g}m basic shares x 100 "
        f"= {derived:.4g} pence"
    )
    return Derivation(
        metric_key=f"{ticker}:Pre-exceptional basic EPS",
        derived_value=derived,
        independent_value=independent,
        arithmetic=arithmetic,
        drivers={
            "operating_profit": operating_profit,
            "net_finance_charge": finance,
            "effective_tax_rate_pct": tax_rate,
            "weighted_average_basic_shares_m": shares,
        },
    )


def apply(ticker: str, company: str, values: dict[str, float]) -> tuple[
    dict[str, float], list[Derivation], list[str]
]:
    """Replace derivable metrics with their derived values.

    Returns the corrected values, the derivations performed, and log notes. The
    independent estimate is retained inside the Derivation so the evidence report
    can show both and the size of the disagreement.
    """
    metrics = {m.label for m in metrics_for(ticker)}
    if ticker != "HAS" or "Pre-exceptional basic EPS" not in metrics:
        return values, [], ["no linked derivation registered for this company"]

    drivers, notes = fetch_drivers(ticker, company)
    derivation = derive_eps(ticker, values, drivers)
    if derivation is None:
        return values, [], notes + ["linked derivation unavailable: drivers missing"]

    corrected = dict(values)
    corrected["Pre-exceptional basic EPS"] = derivation.derived_value
    notes.append(
        f"EPS derived from the P&L: {derivation.arithmetic}; independent estimate "
        f"was {derivation.independent_value:.4g} "
        f"({derivation.divergence:.0%} divergence)"
    )
    return corrected, [derivation], notes
