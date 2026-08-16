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
    name: str
    description: str
    unit: str


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
    "DE:Production & Precision Ag operating profit": Composition(
        description=(
            "Deere reports segment results, so the segment's operating profit is "
            "its net sales multiplied by its operating margin."
        ),
        components=(
            Component("segment_net_sales", "Expected Production & Precision Ag "
                      "segment net sales for the target quarter", "USDm"),
            Component("segment_operating_margin", "Expected Production & Precision "
                      "Ag segment operating margin for the target quarter", "%"),
        ),
        formula=lambda c: c["segment_net_sales"] * c["segment_operating_margin"] / 100.0,
        render=lambda c, v: (
            f"segment net sales {c['segment_net_sales']:g} x operating margin "
            f"{c['segment_operating_margin']:g}% = {v:.4g} USDm"
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


def estimate(metric_key: str, facts: list[Fact], period: str) -> Estimate | None:
    composition = COMPOSITIONS.get(metric_key)
    if composition is None or not facts:
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

    payload = complete(
        SOURCING_SYSTEM, user, _schema(composition.components),
        schema_name="components",
    )

    sourced: dict[str, float] = {}
    used: list[Fact] = []
    notes: list[str] = []
    for item in payload.get("components", []):
        if not item.get("available"):
            continue
        sourced[item["name"]] = float(item["value"])
        notes.append(f"{item['name']} = {item['value']:g} ({item['reasoning']})")
        index = item.get("fact_index", -1)
        if isinstance(index, int) and 0 <= index < len(facts):
            used.append(facts[index])

    required = {c.name for c in composition.components}
    if not required.issubset(sourced):
        missing = sorted(required - set(sourced))
        # Zero confidence rather than None: the reconciler drops it, but the
        # evidence report can still explain why the build-up did not fire.
        return Estimate(
            metric_key=metric_key, value=0.0, method="build_up",
            derivation=f"composition incomplete; unsourced components: {missing}",
            confidence=0.0,
            warnings=[f"build-up unavailable, missing components: {missing}"],
        )

    value = composition.formula(sourced)
    return Estimate(
        metric_key=metric_key,
        value=value,
        method="build_up",
        derivation=composition.render(sourced, value)
        + "\n  " + "\n  ".join(notes),
        assumptions=sourced,
        inputs=used,
        confidence=0.8,
    )
