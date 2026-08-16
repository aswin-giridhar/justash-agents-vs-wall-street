"""Revision breadth, measured from the corpus rather than from a data vendor.

Analyst revision breadth - whether estimates are being raised or cut, and how
consistently - is one of the most robust published predictors of the direction of an
earnings surprise. Funds buy it from vendors. We cannot, but a usable version is
recoverable from the filings themselves.

Two signals are available offline:

1. **Restated consensus over time.** Companies quote consensus when managing
   expectations against it, and they do so repeatedly. Hays published "company
   compiled consensus for FY26 pre-exceptional operating profit is GBP 43.5m" in July
   against an earlier reference to GBP 45.2m. The direction of that drift is revision
   breadth, straight from the primary source.

2. **The company's own guidance path.** Successive outlooks for the same period -
   raised, narrowed, cut, reaffirmed - are management's revisions, and analysts follow
   them closely. A guidance raise partway through a year is a strong positive signal;
   a cut is a strong negative one.

Both are read as a DIRECTION and a strength, never as a level, because the level is
already handled by the guidance anchor. The output is a small bounded tilt, on the
same principle as the post-guidance signals: real information, weak information.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from avws.config import CACHE_DIR
from avws.corpus import build_index, search
from avws.llm import complete
from avws.registry import Metric

REVISION_CACHE = CACHE_DIR / "revision"

# Deliberately smaller than the post-guidance signal cap. A revision trend is a
# second-order signal about the same period the anchor already covers, so it may
# nudge the forecast and must never drive it.
MAX_TILT = {"revenue": 0.015, "eps": 0.035, "percentage": 0.5}

REVISION_SYSTEM = """You track how expectations for ONE metric and ONE period have MOVED over time.

You are given excerpts published at different dates, all about the same company.
Find statements that let you see a REVISION: the same forward-looking number being
restated later at a different level.

Two kinds count:
- consensus restated at successive dates ("consensus is 45.2m" then later "43.5m")
- the company's own guidance for the same period being raised, cut, narrowed or
  reaffirmed ("we now expect", "we are raising", "we have lowered", "unchanged")

For each observation give the publication date you can see in the document header,
the level stated, whether it is "consensus" or "guidance", and a verbatim quote.

Return at most one observation per date. Return an empty list if you cannot see the
same number restated at two different dates - a single statement is a level, not a
revision, and is already handled elsewhere."""

REVISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["observations"],
    "properties": {
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["published", "level", "kind", "quote"],
                "properties": {
                    "published": {"type": "string"},
                    "level": {"type": "number"},
                    "kind": {"type": "string", "enum": ["consensus", "guidance"]},
                    "quote": {"type": "string"},
                },
            },
        }
    },
}


@dataclass
class Revision:
    metric_key: str
    direction: str = "flat"       # "up", "down", "flat"
    magnitude: float = 0.0        # fractional change from first to last observation
    observations: int = 0
    detail: list[str] | None = None
    cap: float = 0.0
    tilt: float = 0.0

    @property
    def measured(self) -> bool:
        return self.observations >= 2

    def describe(self) -> str:
        if not self.measured:
            return (f"revision breadth not measured: {self.observations} dated "
                    f"observation(s), need 2")
        return (f"expectations revised {self.direction} by {self.magnitude:+.2%} "
                f"across {self.observations} dated observations; tilt {self.tilt:+.4f}")


def _cap_for(metric: Metric) -> float:
    if metric.is_percentage:
        return MAX_TILT["percentage"]
    if metric.is_eps:
        return MAX_TILT["eps"]
    return MAX_TILT["revenue"]


def _cache_path(metric: Metric):
    digest = hashlib.sha256((metric.key + REVISION_SYSTEM).encode()).hexdigest()[:12]
    return REVISION_CACHE / f"{metric.ticker}-{digest}.json"


def measure(metric: Metric, use_cache: bool = True) -> Revision:
    cache_file = _cache_path(metric)
    if use_cache and cache_file.exists():
        return Revision(**json.loads(cache_file.read_text(encoding="utf-8")))

    build_index()
    cap = _cap_for(metric)
    chunks, seen = [], set()
    for query in (
        f"we now expect {metric.label} raising lowering our outlook for the year",
        f"consensus for {metric.label} is currently expectations have moved",
        f"updated outlook {metric.label} compared with our previous guidance",
        f"reaffirm unchanged narrowed guidance {metric.label}",
    ):
        for doc, _s, chunk in search(query, ticker=metric.ticker, since="2025-06-01", k=6):
            key = f"{doc.path}:{hash(chunk)}"
            if key in seen:
                continue
            seen.add(key)
            chunks.append(f"<<<{doc.path} | published {doc.published_at} | "
                          f"period {doc.period}>>>\n{chunk}")

    result = Revision(metric_key=metric.key, cap=cap, detail=[])
    if chunks:
        body = "\n\n".join(chunks[:18])
        haystack = " ".join(body.split()).lower()
        payload = complete(
            REVISION_SYSTEM,
            f"Company: {metric.company} ({metric.ticker})\n"
            f"Metric: {metric.label} ({metric.units})\n"
            f"Period of interest: {metric.period}\n\n{body}",
            REVISION_SCHEMA, schema_name="revision",
        )

        points = []
        for item in payload.get("observations", []):
            quote = " ".join((item.get("quote") or "").split()).lower()
            if not quote or quote[:45] not in haystack:
                continue
            level = item.get("level")
            published = (item.get("published") or "").strip()
            if level is None or not published:
                continue
            points.append((published, float(level), item.get("kind", "consensus")))

        # One observation per date, oldest first, so the drift is chronological.
        by_date = {}
        for published, level, kind in points:
            by_date[published] = (level, kind)
        ordered = sorted(by_date.items())

        if len(ordered) >= 2 and ordered[0][1][0]:
            first, last = ordered[0][1][0], ordered[-1][1][0]
            magnitude = (last - first) / abs(first)
            direction = "up" if magnitude > 0.005 else "down" if magnitude < -0.005 else "flat"
            # Scaled to the cap, then clamped. A 10% revision is a strong signal but
            # still only earns the cap.
            tilt = max(-cap, min(cap, magnitude * 5 * cap))
            result = Revision(
                metric_key=metric.key, direction=direction, magnitude=magnitude,
                observations=len(ordered),
                detail=[f"{d}: {v[1]} {v[0]:g}" for d, v in ordered],
                cap=cap, tilt=tilt if direction != "flat" else 0.0,
            )

    REVISION_CACHE.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(asdict(result), indent=1), encoding="utf-8")
    return result


def apply(estimate, metric: Metric, revision: Revision):
    """Apply the revision tilt. Percentages tilt additively, levels multiplicatively."""
    from avws.estimators.base import Estimate

    if estimate is None or not revision.measured or not revision.tilt:
        if estimate is not None:
            estimate.derivation += f"\n  {revision.describe()}"
        return estimate

    if metric.is_percentage:
        value = estimate.value + revision.tilt
        arithmetic = (f"{estimate.value:.6g} {revision.tilt:+.3f}pp revision-breadth "
                      f"tilt = {value:.6g}")
    else:
        value = estimate.value * (1 + revision.tilt)
        arithmetic = (f"{estimate.value:.6g} x (1 {revision.tilt:+.4f} "
                      f"revision-breadth tilt) = {value:.6g}")

    return Estimate(
        metric_key=estimate.metric_key, value=value,
        method=f"{estimate.method}+revision",
        derivation=(f"{estimate.derivation}\n  {arithmetic}\n  "
                    f"{revision.describe()}\n    "
                    + "\n    ".join(revision.detail or [])),
        assumptions={**estimate.assumptions, "revision_tilt": revision.tilt},
        inputs=estimate.inputs, confidence=estimate.confidence,
        warnings=list(estimate.warnings),
    )
