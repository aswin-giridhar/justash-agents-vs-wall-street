"""Build-up estimator: identity composition.

This is the estimator that does actual financial modelling. Each supported metric
has a *composition* - a named set of components and a formula relating them. The
language model's only job is to source each component from the evidence ledger and
say which fact it came from. The arithmetic is Python.

That split is the point. "The model said 3958" is unreviewable. "H1 actual 464.6
plus H2 prior-year 507.8 grown at -4.0% = 952.1" can be checked line by line by
someone who has never seen the code.

Compositions are registered for the four metrics where a defensible identity
exists. The remaining eight metrics fall through to the guidance anchor or the
seasonal fallback; that gap is disclosed rather than hidden.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from avws.estimators.base import Estimate
from avws.ledger import Fact
from avws.llm import complete

SOURCING_SYSTEM = """You source named components for a financial model.

You are given (a) a list of components the model needs and (b) an evidence ledger
of facts already extracted from company filings, each with a verbatim quote.

Rules:
1. Source each component from the ledger where possible. Set `fact_index` to the
   index of the fact you used, or -1 if you had to reason from several facts.
2. If a component genuinely cannot be sourced or reasonably inferred from the
   ledger, set `available` to false. Do NOT invent a number to fill a gap - a
   missing component is recoverable, a fabricated one is not.
3. `reasoning` must be one sentence naming the figures you used.
4. Respect the requested unit exactly. Percentages are percentage points.
5. Distinguish like-for-like growth from actual growth. They are different
   quantities. Actual growth includes divested and closed operations; like-for-like
   excludes them. If a component asks for actual growth, do not supply a
   like-for-like figure - mark it unavailable instead."""


@dataclass(frozen=True)
class Component:
    """A named model assumption.

    `lo` and `hi` bound it. Components are assumptions, and an unconstrained
    assumption is the same hazard as an unconstrained fact: asked for Q3's share of
    Deere's annual segment operating profit, the model returned 55.3% - a quarter
    cannot be over half a year - and the composition produced a figure twice what
    the guidance implies. Bounds are stated where a defensible range exists.
    """

    name: str
    description: str
    unit: str
    lo: float | None = None
    hi: float | None = None

    def check(self, value: float) -> str | None:
        if self.lo is not None and value < self.lo:
            return f"{self.name}={value:g} below the plausible floor {self.lo:g}"
        if self.hi is not None and value > self.hi:
            return f"{self.name}={value:g} above the plausible ceiling {self.hi:g}"
        return None


@dataclass(frozen=True)
class Composition:
    description: str
    components: tuple[Component, ...]
    formula: Callable[[dict[str, float]], float]
    render: Callable[[dict[str, float], float], str]


COMPOSITIONS: dict[str, Composition] = {
    "HAS:Net fees": Composition(
        description=(
            "Hays FY2026 ended 30 June 2026, so the year is closed. Half-one is a "
            "reported actual; only half-two needs estimating from disclosed "
            "divisional growth."
        ),
        components=(
            Component("h1_fy26_net_fees", "Reported actual Group net fees for H1 "
                      "FY2026 (six months to 31 December 2025)", "GBPm"),
            Component("h2_fy25_net_fees", "Reported actual Group net fees for H2 "
                      "FY2025 (six months to 30 June 2025)", "GBPm"),
            Component("h2_fy26_actual_growth", "Year-on-year growth of Group net "
                      "fees in H2 FY2026 on an ACTUAL (not like-for-like) basis, "
                      "implied by the Q3 and Q4 FY2026 trading updates", "%"),
        ),
        formula=lambda c: c["h1_fy26_net_fees"]
        + c["h2_fy25_net_fees"] * (1 + c["h2_fy26_actual_growth"] / 100.0),
        render=lambda c, v: (
            f"H1 FY26 actual {c['h1_fy26_net_fees']:g} + "
            f"H2 FY25 {c['h2_fy25_net_fees']:g} x "
            f"(1 + {c['h2_fy26_actual_growth']:g}% actual growth) = {v:.4g} GBPm"
        ),
    ),
    "ADI:Adjusted gross margin": Composition(
        description=(
            "ADI guides adjusted operating margin but not adjusted gross margin. "
            "Gross margin is recovered by adding back the adjusted operating "
            "expense ratio, which is stable quarter to quarter."
        ),
        components=(
            Component("guided_adj_operating_margin", "Company-guided adjusted "
                      "operating margin for the target quarter", "%"),
            Component("recent_adj_opex_ratio", "Adjusted operating expenses as a "
                      "percentage of revenue in the most recent reported quarter, "
                      "i.e. adjusted gross margin % minus adjusted operating "
                      "margin %", "%"),
        ),
        formula=lambda c: c["guided_adj_operating_margin"] + c["recent_adj_opex_ratio"],
        render=lambda c, v: (
            f"guided adjusted operating margin {c['guided_adj_operating_margin']:g}% "
            f"+ adjusted opex ratio {c['recent_adj_opex_ratio']:g}% = {v:.4g}%"
        ),
    ),
    "HD:Net sales": Composition(
        description=(
            "Home Depot total sales growth decomposes into comparable-store growth "
            "plus the contribution of non-comparable square footage and any "
            "acquisition or foreign-exchange effect."
        ),
        components=(
            Component("prior_year_net_sales", "Reported net sales in the same "
                      "quarter of the prior fiscal year", "USDm"),
            Component("expected_comp_sales_growth", "Expected comparable sales "
                      "growth for the target quarter", "%"),
            Component("non_comp_contribution", "Contribution to total sales growth "
                      "from non-comparable sources: new stores, acquisitions and "
                      "foreign exchange", "%"),
        ),
        formula=lambda c: c["prior_year_net_sales"]
        * (1 + (c["expected_comp_sales_growth"] + c["non_comp_contribution"]) / 100.0),
        render=lambda c, v: (
            f"prior-year net sales {c['prior_year_net_sales']:g} x "
            f"(1 + comp {c['expected_comp_sales_growth']:g}% + non-comp "
            f"{c['non_comp_contribution']:g}%) = {v:.4g} USDm"
        ),
    ),
    "HD:Comparable sales, total company_drivers": Composition(
        description=(
            "Driver decomposition. Home Depot reports comparable sales as the product "
            "of two drivers it discloses separately: average ticket and customer "
            "transactions. Modelling them apart is what an analyst does, because they "
            "move for different reasons - ticket tracks commodity prices and big-ticket "
            "demand, transactions track footfall - and a forecast that gets the right "
            "answer from offsetting wrong drivers is not a forecast worth trusting."
        ),
        components=(
            Component("expected_ticket_growth", "Expected year-on-year growth in "
                      "comparable average ticket for the target quarter, from the "
                      "recent trend and management commentary", "%",
                      lo=-15.0, hi=15.0),
            Component("expected_transaction_growth", "Expected year-on-year growth in "
                      "comparable customer transactions for the target quarter", "%",
                      lo=-15.0, hi=15.0),
        ),
        # Comparable sales compound the two drivers rather than summing them.
        formula=lambda c: (
            (1 + c["expected_ticket_growth"] / 100.0)
            * (1 + c["expected_transaction_growth"] / 100.0) - 1
        ) * 100.0,
        render=lambda c, v: (
            f"(1 + ticket {c['expected_ticket_growth']:g}%) x (1 + transactions "
            f"{c['expected_transaction_growth']:g}%) - 1 = {v:.4g}% comparable sales"
        ),
    ),
    "HD:Adjusted diluted EPS": Composition(
        description=(
            "Home Depot guides full-year adjusted diluted EPS growth rather than "
            "quarterly EPS. The quarter is recovered by applying the quarter's "
            "historical share of the annual figure, which is stable for a retailer "
            "with a consistent seasonal pattern."
        ),
        components=(
            Component("fy25_adjusted_diluted_eps", "Reported actual FY2025 full-year "
                      "adjusted diluted EPS", "USD/share"),
            Component("fy26_guided_adjusted_eps_growth", "Company guidance for FY2026 "
                      "adjusted diluted EPS growth", "%"),
            Component("q2_share_of_annual_adjusted_eps", "Q2's historical share of "
                      "full-year adjusted diluted EPS, from prior years", "%", lo=8.0, hi=45.0),
        ),
        formula=lambda c: (
            c["fy25_adjusted_diluted_eps"]
            * (1 + c["fy26_guided_adjusted_eps_growth"] / 100.0)
            * c["q2_share_of_annual_adjusted_eps"] / 100.0
        ),
        render=lambda c, v: (
            f"FY25 adjusted EPS {c['fy25_adjusted_diluted_eps']:g} x (1 + "
            f"{c['fy26_guided_adjusted_eps_growth']:g}% guided growth) x Q2 share "
            f"{c['q2_share_of_annual_adjusted_eps']:g}% = {v:.4g} USD/share"
        ),
    ),
    "HAS:Pre-exceptional operating profit": Composition(
        description=(
            "Hays FY2026 is closed. H1 pre-exceptional operating profit is a reported "
            "actual; only H2 needs estimating. On 10 July management stated it expects "
            "FY26 pre-exceptional operating profit at the TOP of the consensus range, "
            "following a return to strong year-on-year growth in the second half."
        ),
        components=(
            Component("h1_fy26_operating_profit", "Reported actual pre-exceptional "
                      "operating profit for H1 FY2026 (six months to 31 December "
                      "2025)", "GBPm"),
            Component("h2_fy25_operating_profit", "Reported actual pre-exceptional "
                      "operating profit for H2 FY2025 (six months to 30 June 2025)",
                      "GBPm"),
            Component("h2_fy26_growth", "Year-on-year growth of pre-exceptional "
                      "operating profit in H2 FY2026, implied by management's "
                      "statement of a return to strong second-half growth and "
                      "landing at the top of the consensus range", "%"),
        ),
        formula=lambda c: c["h1_fy26_operating_profit"]
        + c["h2_fy25_operating_profit"] * (1 + c["h2_fy26_growth"] / 100.0),
        render=lambda c, v: (
            f"H1 FY26 actual {c['h1_fy26_operating_profit']:g} + H2 FY25 "
            f"{c['h2_fy25_operating_profit']:g} x (1 + {c['h2_fy26_growth']:g}% H2 "
            f"growth) = {v:.4g} GBPm"
        ),
    ),
    "HAS:Pre-exceptional basic EPS": Composition(
        description=(
            "Standard earnings bridge: operating profit less net finance charges, "
            "taxed at the effective rate, divided by weighted average basic shares. "
            "Reported in pence."
        ),
        components=(
            Component("fy26_operating_profit", "Expected FY2026 pre-exceptional "
                      "operating profit", "GBPm"),
            Component("fy26_net_finance_charge", "Expected FY2026 net finance charge "
                      "as a positive number to be subtracted", "GBPm"),
            Component("fy26_effective_tax_rate", "Expected FY2026 effective tax rate "
                      "on pre-exceptional profit", "%", lo=0.0, hi=45.0),
            Component("weighted_average_basic_shares", "Weighted average number of "
                      "basic shares outstanding", "millions"),
        ),
        formula=lambda c: (
            (c["fy26_operating_profit"] - c["fy26_net_finance_charge"])
            * (1 - c["fy26_effective_tax_rate"] / 100.0)
            / c["weighted_average_basic_shares"]
        ) * 100.0,
        render=lambda c, v: (
            f"({c['fy26_operating_profit']:g} operating profit - "
            f"{c['fy26_net_finance_charge']:g} net finance) x "
            f"(1 - {c['fy26_effective_tax_rate']:g}% tax) / "
            f"{c['weighted_average_basic_shares']:g}m shares x 100 = {v:.4g} pence"
        ),
    ),
    "HD:Comparable sales, total company": Composition(
        description=(
            "Home Depot reaffirmed full-year comparable sales guidance and has "
            "reported Q1. If the full year is to land on guidance, the remaining "
            "three quarters must average what the arithmetic requires - a constraint "
            "the company itself has committed to."
        ),
        components=(
            Component("fy26_guided_comp_sales_growth", "The MIDPOINT of company "
                      "guidance for FY2026 total company comparable sales growth. "
                      "If guidance is a range such as 'approximately 1.0%' or "
                      "'0.5% to 1.5%', use the central value, NOT the high end.",
                      "%"),
            Component("q1_fy26_actual_comp_sales", "Reported actual Q1 FY2026 total "
                      "company comparable sales growth", "%"),
        ),
        formula=lambda c: (
            4 * c["fy26_guided_comp_sales_growth"] - c["q1_fy26_actual_comp_sales"]
        ) / 3.0,
        render=lambda c, v: (
            f"(4 x FY guidance midpoint {c['fy26_guided_comp_sales_growth']:g}% - "
            f"Q1 actual {c['q1_fy26_actual_comp_sales']:g}%) / 3 remaining quarters "
            f"= {v:.4g}% required average for Q2-Q4 (equal-quarter weighting; Q2 is "
            f"seasonally larger, so the true Q2 requirement is slightly lower)"
        ),
    ),
    "DE:Worldwide net sales and revenues": Composition(
        description=(
            "Deere does not guide quarterly revenue, but its full-year outlook "
            "states expected net sales changes by segment. Applying the implied "
            "full-year change to the prior-year quarter is better founded than "
            "extrapolating a trend, because two of the last three years were "
            "double-digit declines and a third is a claim the outlook does not make."
        ),
        components=(
            Component("prior_year_q3_net_sales", "Reported worldwide net sales and "
                      "revenues in Q3 of the prior fiscal year", "USDm"),
            Component("fy26_guided_net_sales_change", "MIDPOINT of the full-year "
                      "FY2026 change in worldwide net sales and revenues implied by "
                      "the company's segment outlook. Deere guides each segment "
                      "separately - for example Production & Precision Ag 'down "
                      "5-10%' - so weight the segment guides by their FY2025 net "
                      "sales to get the group figure. A guide of 'down 5-10%' means "
                      "-7.5 for that segment.", "%"),
        ),
        formula=lambda c: c["prior_year_q3_net_sales"]
        * (1 + c["fy26_guided_net_sales_change"] / 100.0),
        render=lambda c, v: (
            f"prior-year Q3 {c['prior_year_q3_net_sales']:g} x (1 + "
            f"{c['fy26_guided_net_sales_change']:g}% guided FY change) = {v:.4g} USDm"
        ),
    ),
    "DE:Diluted EPS (GAAP)": Composition(
        description=(
            "Deere guides full-year net income rather than quarterly EPS. Quarterly "
            "EPS is recovered by applying the quarter's historical share of annual "
            "net income and dividing by diluted shares."
        ),
        components=(
            Component("fy26_guided_net_income", "Company-guided FY2026 net income "
                      "attributable to Deere & Company", "USDm"),
            Component("q3_share_of_annual_net_income", "Q3's historical share of "
                      "full-year net income, from prior years", "%", lo=8.0, hi=40.0),
            Component("diluted_shares", "Weighted average diluted shares outstanding",
                      "millions"),
        ),
        formula=lambda c: (
            c["fy26_guided_net_income"] * c["q3_share_of_annual_net_income"] / 100.0
        ) / c["diluted_shares"],
        render=lambda c, v: (
            f"FY guided net income {c['fy26_guided_net_income']:g} x Q3 share "
            f"{c['q3_share_of_annual_net_income']:g}% / {c['diluted_shares']:g}m "
            f"diluted shares = {v:.4g} USD/share"
        ),
    ),
    "DE:Production & Precision Ag operating profit": Composition(
        description=(
            "Deere reports segment results, so the segment's operating profit is "
            "its net sales multiplied by its operating margin."
        ),
        components=(
            Component("fy25_segment_net_sales", "Reported FY2025 full-year "
                      "Production & Precision Ag segment net sales", "USDm"),
            Component("fy26_segment_sales_change", "MIDPOINT of the company's FY2026 "
                      "outlook for the change in Production & Precision Ag segment "
                      "net sales. A guide of 'down 5-10%' means -7.5.", "%"),
            Component("fy26_segment_operating_margin", "MIDPOINT of the company's "
                      "FY2026 outlook for Production & Precision Ag segment "
                      "operating margin. A guide of '11-13%' means 12.", "%", lo=0.0, hi=45.0),
            Component("q3_share_of_segment_annual_profit", "Q3's historical share of "
                      "the full-year Production & Precision Ag operating profit, "
                      "from prior years", "%", lo=8.0, hi=40.0),
        ),
        formula=lambda c: (
            c["fy25_segment_net_sales"] * (1 + c["fy26_segment_sales_change"] / 100.0)
            * c["fy26_segment_operating_margin"] / 100.0
            * c["q3_share_of_segment_annual_profit"] / 100.0
        ),
        render=lambda c, v: (
            f"FY25 segment sales {c['fy25_segment_net_sales']:g} x (1 + "
            f"{c['fy26_segment_sales_change']:g}% guided change) x "
            f"{c['fy26_segment_operating_margin']:g}% guided margin x "
            f"{c['q3_share_of_segment_annual_profit']:g}% Q3 share = {v:.4g} USDm"
        ),
    ),
}


def _schema(components: tuple[Component, ...]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["components"],
        "properties": {
            "components": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "value", "available", "fact_index", "reasoning"],
                    "properties": {
                        "name": {"type": "string",
                                 "enum": [c.name for c in components]},
                        "value": {"type": "number"},
                        "available": {"type": "boolean"},
                        "fact_index": {"type": "integer"},
                        "reasoning": {"type": "string"},
                    },
                },
            }
        },
    }


def estimate(
    metric_key: str,
    facts: list[Fact],
    period: str,
    documents: str = "",
    variant: str = "",
) -> Estimate | None:
    """Run a composition. `variant` selects an alternative decomposition of the same
    metric - "_drivers" builds comparable sales from ticket and transactions rather
    than from the implied full-year guide, giving two independent routes to the same
    number instead of one."""
    composition = COMPOSITIONS.get(metric_key + variant)
    if composition is None or (not facts and not documents):
        return None

    ledger_text = "\n".join(
        f"[{i}] {f.basis:<14} {f.period:<10} {f.value:>12g} {f.unit:<6} "
        f"{f.label or ''} | {f.source_quote[:220]}"
        for i, f in enumerate(facts)
    )
    wanted = "\n".join(
        f"- {c.name} ({c.unit}): {c.description}" for c in composition.components
    )
    user = (
        f"Target metric: {metric_key} for period {period}\n"
        f"Model note: {composition.description}\n\n"
        f"Components required:\n{wanted}\n\n"
        f"Evidence ledger:\n{ledger_text}"
    )
    if documents:
        # Components are inputs of arbitrary type - a guided percentage change, a
        # margin range, a share count - and the ledger holds only values in the
        # metric's own unit, so a guide of "down 5-10%" has nowhere to live in it.
        # Deere publishes its whole FY2026 outlook that way, which is why every
        # Deere composition reported its components unsourced until the sourcing
        # step could read the documents as well as the ledger.
        user += (
            "\n\nSource documents (use these for components the ledger cannot hold, "
            "such as guided percentage changes and guidance ranges; quote from here "
            "exactly as you would from the ledger):\n" + documents
        )

    payload = complete(
        SOURCING_SYSTEM, user, _schema(composition.components),
        schema_name="components",
    )

    by_name = {c.name: c for c in composition.components}
    sourced: dict[str, float] = {}
    used: list[Fact] = []
    notes: list[str] = []
    rejected: list[str] = []
    for item in payload.get("components", []):
        if not item.get("available"):
            continue
        spec = by_name.get(item["name"])
        value = float(item["value"])
        problem = spec.check(value) if spec else None
        if problem:
            rejected.append(problem)
            continue
        sourced[item["name"]] = value
        notes.append(f"{item['name']} = {value:g} ({item['reasoning']})")
        index = item.get("fact_index", -1)
        if isinstance(index, int) and 0 <= index < len(facts):
            used.append(facts[index])

    required = {c.name for c in composition.components}
    if not required.issubset(sourced):
        missing = sorted(required - set(sourced))
        # Zero confidence rather than None: the reconciler drops it, but the
        # evidence report can still explain why the build-up did not fire.
        detail = f"composition incomplete; unsourced components: {missing}"
        if rejected:
            detail += f"; rejected as implausible: {rejected}"
        return Estimate(
            metric_key=metric_key, value=0.0, method="build_up",
            derivation=detail, confidence=0.0,
            warnings=[f"build-up unavailable, missing components: {missing}"]
            + [f"component rejected: {r}" for r in rejected],
        )

    value = composition.formula(sourced)
    return Estimate(
        metric_key=metric_key,
        value=value,
        method="build_up" + (variant.replace("_", "_") if variant else ""),
        derivation=composition.render(sourced, value)
        + "\n  " + "\n  ".join(notes),
        assumptions=sourced,
        inputs=used,
        confidence=0.8,
    )

