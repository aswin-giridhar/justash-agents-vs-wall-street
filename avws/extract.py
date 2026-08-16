"""Turn documents into ledger facts.

The extraction prompt forbids estimation. The model transcribes figures that are
physically present in the supplied text and nothing else. That restriction is what
keeps the forecasting judgement in reviewable arithmetic rather than inside an
opaque model call.

Fabrication is caught mechanically, not by trust: every returned fact must carry a
`source_quote` that actually appears in the text we supplied. Quotes that do not
match are dropped and counted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from avws.corpus import Doc, build_index, search
from avws.ledger import Fact
from avws.llm import complete
from avws.registry import Metric

_WS = re.compile(r"\s+")

EXTRACTION_SYSTEM = """You transcribe financial figures from company filings.

Rules, in order of importance:
1. You NEVER estimate, infer, forecast, or compute. You only transcribe figures
   that appear verbatim in the supplied text.
2. Every figure you return must include `source_quote`: a short span copied
   EXACTLY from the supplied text, containing the figure. If you cannot copy an
   exact span, do not return the figure.
3. `basis` must be correct and is the most common source of error:
   - "reported"      a published GAAP/statutory figure for a completed period
   - "adjusted"      a non-GAAP, adjusted, underlying or pre-exceptional figure
   - "guidance_mid"  the midpoint or central value of company guidance
   - "guidance_low"  the low end of a guidance range
   - "guidance_high" the high end of a guidance range
   A company reporting both GAAP and adjusted EPS gives you two DIFFERENT facts.
4. `value` is a plain number. Convert billions to millions when the requested unit
   is a millions unit ($3.9 billion -> 3900). Percentages are percentage points
   (73.0, not 0.73). Pence are pence (6.2, not 0.062).
5. `period` uses the company's own fiscal labelling, e.g. "FY2026Q2", "FY2025".
6. Be careful with like-for-like versus actual growth. They are different figures
   and must be labelled differently in `label`.
7. If the text contains no figure matching the request, return an empty list."""

FACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["facts"],
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "label", "value", "unit", "basis", "period",
                    "source_quote", "doc_ref", "confidence",
                ],
                "properties": {
                    "label": {"type": "string"},
                    "value": {"type": "number"},
                    "unit": {"type": "string"},
                    "basis": {
                        "type": "string",
                        "enum": [
                            "reported", "adjusted", "guidance_mid",
                            "guidance_low", "guidance_high",
                        ],
                    },
                    "period": {"type": "string"},
                    "source_quote": {"type": "string"},
                    "doc_ref": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        }
    },
}


@dataclass(frozen=True)
class Probe:
    query: str
    doc_type: str | None = None
    since: str | None = None
    until: str | None = None
    k: int = 4
    intent: str = "history"


def _normalise(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def plan_for(metric: Metric) -> list[Probe]:
    """Retrieval probes for one metric.

    Three intents: past actuals (history and scale bands), company guidance
    (the anchor), and the components a build-up needs.
    """
    label = metric.label
    probes = [
        Probe(f"{label} results for the quarter", "FILING", since="2024-01-01",
              k=6, intent="history"),
        Probe(f"{label} outlook guidance we expect forecasting", "FILING",
              since="2025-06-01", k=4, intent="guidance"),
        # Calibration: older guidance statements, so GuidanceAnchor can measure how
        # this company has historically landed against its own outlook rather than
        # assuming the midpoint is unbiased.
        Probe(f"outlook we are forecasting we expect {label}", "FILING",
              since="2023-01-01", until="2026-05-31", k=8, intent="calibration"),
    ]

    extra: dict[str, list[Probe]] = {
        "HD": [
            Probe("comparable sales total company US fiscal guidance total sales growth",
                  "FILING", since="2025-06-01", k=4, intent="component"),
            Probe("adjusted diluted earnings per share fiscal 2026 guidance growth",
                  "FILING", since="2025-06-01", k=3, intent="component"),
        ],
        "ADI": [
            Probe("outlook third quarter revenue adjusted EPS operating margin",
                  "FILING", since="2026-05-01", k=3, intent="guidance"),
            Probe("adjusted gross margin percentage adjusted operating margin",
                  "FILING", since="2025-01-01", k=5, intent="component"),
        ],
        "HAS": [
            Probe("net fees by division Germany United Kingdom Australia Rest of World",
                  "FILING", since="2024-06-01", k=5, intent="component"),
            Probe("quarterly net fee growth actual like-for-like by division",
                  "FILING", since="2025-09-01", k=6, intent="component"),
            Probe("pre-exceptional operating profit consensus range conversion rate",
                  "FILING", since="2025-06-01", k=4, intent="guidance"),
            Probe("basic earnings per share pre-exceptional weighted average shares",
                  "FILING", since="2024-06-01", k=4, intent="component"),
        ],
        "DE": [
            Probe("net income forecast fiscal 2026 outlook full year guidance",
                  "FILING", since="2025-10-01", k=4, intent="guidance"),
            Probe("production and precision agriculture net sales operating profit segment",
                  "FILING", since="2025-01-01", k=6, intent="component"),
        ],
    }
    return probes + extra.get(metric.ticker, [])


def _gather_chunks(metric: Metric) -> list[tuple[Doc, str, str]]:
    """Run the plan and return unique (doc, chunk, intent) triples."""
    build_index()
    seen: set[tuple[str, int]] = set()
    out: list[tuple[Doc, str, str]] = []
    for probe in plan_for(metric):
        for doc, _score, chunk in search(
            probe.query, ticker=metric.ticker, doc_type=probe.doc_type,
            since=probe.since, until=probe.until, k=probe.k,
        ):
            key = (doc.path, hash(chunk))
            if key in seen:
                continue
            seen.add(key)
            out.append((doc, chunk, probe.intent))
    return out


def extract_facts(metric: Metric, max_chunks: int = 18) -> tuple[list[Fact], dict]:
    """Extract ledger facts for one metric via two independent paths.

    Path one is deterministic table-row matching (no model call). Path two is
    model transcription over retrieved chunks. They are merged because they fail
    differently: retrieval can rank the right table off the page, and a row-label
    match cannot read prose guidance. Running both is more reliable than either.
    """
    from avws.table_facts import harvest

    deterministic = harvest(metric)

    chunks = _gather_chunks(metric)[:max_chunks]
    if not chunks:
        return deterministic, {
            "chunks": 0, "returned": 0, "kept": 0, "rejected_quotes": 0,
            "from_tables": len(deterministic),
        }

    corpus_text = "\n\n".join(
        f"<<<DOC {i}: {doc.path} | published {doc.published_at} | "
        f"{doc.document_type} | period {doc.period}>>>\n{chunk}"
        for i, (doc, chunk, _intent) in enumerate(chunks)
    )

    user = (
        f"Company: {metric.company} ({metric.ticker})\n"
        f"Target metric: {metric.label}\n"
        f"Target unit: {metric.units}\n"
        f"Target period: {metric.period}\n\n"
        "Transcribe every figure in the documents below that is this metric, a "
        "prior-period value of this metric, company guidance for it, or a direct "
        "component of it. Set doc_ref to the DOC path shown in the header.\n\n"
        f"{corpus_text}"
    )

    payload = complete(EXTRACTION_SYSTEM, user, FACT_SCHEMA, schema_name="facts")
    raw = payload.get("facts", [])

    haystack = _normalise(corpus_text)
    doc_by_path = {doc.path: doc for doc, _c, _i in chunks}

    from avws.table_facts import normalise as normalise_value

    kept: list[Fact] = []
    rejected = 0
    out_of_band = 0
    for item in raw:
        quote = (item.get("source_quote") or "").strip()
        if not quote or _normalise(quote) not in haystack:
            rejected += 1
            continue

        try:
            value = float(item["value"])
        except (KeyError, TypeError, ValueError):
            rejected += 1
            continue

        # Basis points are a unit that looks like a number 100x too large. Home
        # Depot writes "foreign exchange rates negatively impacted total company
        # comparable sales by approximately 40 basis points"; transcribed as-is
        # that becomes -40 percentage points instead of -0.4.
        lowered = quote.lower()
        if metric.is_percentage and ("basis point" in lowered or "bps" in lowered):
            value = value / 100.0

        # Same plausibility bands the deterministic path uses. Applying them only
        # there left the model path unguarded, which is how -40% comparable sales
        # reached the ledger.
        checked = normalise_value(metric, value)
        if checked is None:
            out_of_band += 1
            continue
        value = checked[0]
        ref = item.get("doc_ref", "")
        doc = doc_by_path.get(ref) or next(
            (d for p, d in doc_by_path.items() if ref and ref in p), None
        )
        try:
            kept.append(
                Fact(
                    metric_key=metric.key,
                    company=metric.company,
                    period=item.get("period") or metric.period,
                    value=value,
                    unit=item.get("unit") or metric.units,
                    basis=item["basis"],
                    source_doc=doc.path if doc else (ref or "unknown"),
                    source_quote=quote,
                    confidence=float(item.get("confidence", 0.8)),
                    label=item.get("label", ""),
                )
            )
        except (ValueError, KeyError, TypeError):
            rejected += 1

    merged = deterministic + kept
    return merged, {
        "chunks": len(chunks),
        "returned": len(raw),
        "kept": len(kept),
        "rejected_quotes": rejected,
        "rejected_out_of_band": out_of_band,
        "from_tables": len(deterministic),
    }
