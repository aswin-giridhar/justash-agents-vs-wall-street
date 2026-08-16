"""The validation gate.

Ordered: scale bands, then cross-metric identities, then an adversarial critic.
Each stage records what it found, including when it found nothing, so the run log
can show the gate actually ran rather than merely passed.

Design rule throughout: the gate never returns None and never blanks a value. A
missing forecast scores 5.0 - the same as being wrong by ten times the floor - so
a suspect number is always better than no number. When the gate cannot resolve a
concern it emits the best available estimate with a loud warning.
"""

from __future__ import annotations

from avws.estimators.base import Estimate
from avws.ledger import Fact
from avws.llm import complete
from avws.registry import Metric
from avws.units import looks_like_fraction_not_percent

CRITIC_SYSTEM = """You are an adversarial reviewer of a financial forecast.

Your job is to argue the number is WRONG. Assume it is wrong and look for the
reason. Default to finding a problem.

Concentrate on the failure modes that actually occur:
- wrong scale: thousands vs millions vs billions
- a percentage entered as a fraction (0.045 instead of 4.5) or the reverse
- GAAP where adjusted was required, or the reverse
- like-for-like growth used where actual growth was required
- the wrong fiscal period, or a prior-year figure carried forward by mistake
- currency confusion, including pounds where pence were required
- an identity that does not hold, such as margin x revenue not matching profit

Return the single strongest objection.

Set `is_concrete` to true ONLY if your objection names a specific figure, unit,
period or basis that could be checked against a document. Objections of the form
"this seems high" or "there is uncertainty" are NOT concrete; set false for those.
If after genuine effort you cannot find a concrete problem, say so honestly with
objection null and is_concrete false."""

CRITIC_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["objection", "is_concrete", "suggested_direction"],
    "properties": {
        "objection": {"type": ["string", "null"]},
        "is_concrete": {"type": "boolean"},
        "suggested_direction": {
            "type": "string",
            "enum": ["too_high", "too_low", "wrong_scale", "wrong_basis", "none"],
        },
    },
}


def check_scale(metric: Metric, value: float, history: list[float]) -> list[str]:
    """Flag values that are implausible against the metric's own history."""
    issues: list[str] = []
    if not history:
        return issues

    if metric.is_percentage and looks_like_fraction_not_percent(value, history):
        issues.append(
            f"scale: {value:g} looks like a fraction where percentage points were "
            f"required (history runs {min(history):g} to {max(history):g})"
        )
        return issues

    low, high = min(history), max(history)
    span_low = low / 4 if low > 0 else low * 4
    span_high = high * 4 if high > 0 else high / 4
    if not (span_low <= value <= span_high):
        issues.append(
            f"scale: {value:g} falls outside 4x the historical band "
            f"[{low:g}, {high:g}]"
        )
    return issues


def check_identities(
    ticker: str, values: dict[str, float], facts_by_metric: dict[str, list[Fact]]
) -> list[str]:
    """Cross-metric arithmetic consistency.

    Tolerances are deliberately wide. These checks exist to catch order-of-magnitude
    and basis errors, not to second-guess a forecast by a few percent.
    """
    issues: list[str] = []

    if ticker == "ADI":
        revenue = values.get("Revenue")
        margin = values.get("Adjusted gross margin")
        if revenue and margin:
            if not (55.0 <= margin <= 85.0):
                issues.append(
                    f"identity: ADI adjusted gross margin {margin:g}% is outside the "
                    f"55-85% range this business has operated in"
                )
            gross_profit = revenue * margin / 100.0
            if gross_profit > revenue:
                issues.append("identity: implied gross profit exceeds revenue")

    if ticker == "DE":
        total = values.get("Worldwide net sales and revenues")
        segment = values.get("Production & Precision Ag operating profit")
        if total and segment and segment >= total:
            issues.append(
                f"identity: segment operating profit {segment:g} is not less than "
                f"total net sales and revenues {total:g}"
            )

    if ticker == "HD":
        sales = values.get("Net sales")
        comp = values.get("Comparable sales, total company")
        if sales and comp is not None and abs(comp) > 25.0:
            issues.append(
                f"identity: comparable sales {comp:g}% is implausible for a mature "
                f"retailer absent an exceptional event"
            )

    if ticker == "HAS":
        fees = values.get("Net fees")
        profit = values.get("Pre-exceptional operating profit")
        if fees and profit:
            if profit > fees:
                issues.append(
                    f"identity: operating profit {profit:g} exceeds net fees "
                    f"{fees:g}, which is impossible - profit is fees less costs"
                )
            elif profit / fees > 0.25:
                issues.append(
                    f"identity: implied conversion rate {profit / fees:.1%} is far "
                    f"above the mid-single-digit range Hays has reported recently"
                )
    return issues


def critique(
    metric: Metric, estimate: Estimate, identity_issues: list[str]
) -> dict:
    quotes = "\n".join(
        f"- [{f.basis}] {f.period}: {f.source_quote[:200]}"
        for f in estimate.inputs[:8]
    ) or "- (no source facts attached)"
    user = (
        f"Metric: {metric.label} for {metric.company} ({metric.ticker}), "
        f"period {metric.period}\n"
        f"Required unit: {metric.units}\n"
        f"Proposed value: {estimate.value:g}\n\n"
        f"How it was derived:\n{estimate.derivation}\n\n"
        f"Source evidence:\n{quotes}\n\n"
        f"Automated identity checks reported: "
        f"{identity_issues if identity_issues else 'no violations'}"
    )
    try:
        return complete(CRITIC_SYSTEM, user, CRITIC_SCHEMA, schema_name="critique")
    except Exception as exc:  # noqa: BLE001
        # A critic failure must not take down the run, but it must be visible:
        # an unreviewed number is not the same as a reviewed one.
        return {
            "objection": f"critic unavailable: {type(exc).__name__}",
            "is_concrete": False,
            "suggested_direction": "none",
        }


def gate(
    metric: Metric,
    estimate: Estimate,
    history: list[float],
    identity_issues: list[str],
) -> tuple[Estimate, list[str]]:
    """Run the gate. Returns the estimate (possibly annotated) and its findings."""
    findings: list[str] = []
    findings.extend(check_scale(metric, estimate.value, history))
    findings.extend(identity_issues)

    verdict = critique(metric, estimate, identity_issues)
    objection = verdict.get("objection")
    if objection and verdict.get("is_concrete"):
        findings.append(f"critic: {objection}")
        estimate.warnings.append(f"critic raised a concrete objection: {objection}")
    elif objection:
        findings.append(f"critic (not concrete, no action): {objection}")
    else:
        findings.append("critic: no concrete objection found")

    estimate.warnings.extend(f for f in findings if f.startswith("scale:"))
    return estimate, findings
