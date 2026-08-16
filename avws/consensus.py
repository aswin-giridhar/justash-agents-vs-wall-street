"""Extract the consensus the company itself quotes.

We are scored against a consensus mean we cannot see, and the guidance midpoint is
only a proxy for it. But companies frequently state the analyst consensus outright,
because they are managing expectations against it:

    "FY26 pre-exceptional operating profit is expected to be in line with market
     consensus expectations of GBP 45.2m"

    "we currently expect FY 2026 pre-exceptional operating profit will be at the top
     of the GBP 37 million - GBP 46 million range"

That is not a proxy. It is the number, and where a RANGE is given it also yields the
one thing a guidance midpoint can never provide: **dispersion**. A wide range means
analysts disagree, which means Wall Street's error is likely larger, which means the
denominator we are divided by is larger and the metric is easier to win. A narrow
range means the opposite.

Companies also state where in that range they expect to land, which is a direct
statement about the sign of the surprise from the only party that already knows.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from avws.config import CACHE_DIR
from avws.corpus import build_index, search
from avws.llm import complete
from avws.registry import Metric

CONSENSUS_CACHE = CACHE_DIR / "consensus"

CONSENSUS_SYSTEM = """You find where a company states the ANALYST CONSENSUS for a metric.

Companies manage expectations against consensus and often quote it directly:
"in line with market consensus of GBP 45.2m", "the company-compiled consensus range
is $3.10 to $3.40", "analysts expect".

Return only what the text actually states.
- `value` is the consensus point estimate if one is given, else null.
- `low` and `high` are the consensus RANGE if one is given, else null.
- `position` is where the company says it will land within that range:
  "top", "above", "middle", "in_line", "bottom", "below", or "unstated".
- `period` must be the period the consensus refers to.
- `quote` must be copied EXACTLY from the supplied text.

IMPORTANT: when a company says it expects to land somewhere within "the range", and
that range is one of analyst or market expectations rather than a range the company
itself issued as guidance, that IS a consensus statement. Capture it. For example:

  "we currently expect FY 2026 pre-exceptional operating profit will be at the top
   of the GBP 37 million - GBP 46 million range"

is a consensus range of 37 to 46 with position "top". So is "in line with market
consensus expectations of GBP 45.2m" (value 45.2, position "in_line").

Return an empty list only if the text genuinely states no analyst or market
expectation figure. A company's own numeric guidance for a metric, with no reference
to analyst or market expectations, is NOT a consensus statement."""

CONSENSUS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["statements"],
    "properties": {
        "statements": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["period", "value", "low", "high", "position", "quote"],
                "properties": {
                    "period": {"type": "string"},
                    "value": {"type": ["number", "null"]},
                    "low": {"type": ["number", "null"]},
                    "high": {"type": ["number", "null"]},
                    "position": {
                        "type": "string",
                        "enum": ["top", "above", "middle", "in_line",
                                 "bottom", "below", "unstated"],
                    },
                    "quote": {"type": "string"},
                },
            },
        }
    },
}

# Where the company says it will land, as a fraction of the way from the consensus
# midpoint toward the relevant end of the range. "Top of the range" is a strong claim
# and gets most of the way there; "above" without a range gets a modest nudge.
POSITION_WEIGHT = {
    "top": 0.85, "above": 0.5, "middle": 0.0, "in_line": 0.0,
    "bottom": -0.85, "below": -0.5, "unstated": 0.0,
}


@dataclass
class Consensus:
    metric_key: str
    value: float | None = None
    low: float | None = None
    high: float | None = None
    position: str = "unstated"
    quote: str = ""

    @property
    def found(self) -> bool:
        return self.value is not None or (self.low is not None and self.high is not None)

    @property
    def midpoint(self) -> float | None:
        """The consensus level itself.

        A stated VALUE wins over the midpoint of a stated range. Hays published
        both - "company compiled consensus is GBP 43.5m" alongside a GBP 37-46m
        range - and those are different numbers: 43.5 is the consensus, while the
        range midpoint of 41.5 is merely the centre of the spread. We are scored
        against the mean, so the mean is what we want; the range tells us dispersion.
        """
        if self.value is not None:
            return self.value
        if self.low is not None and self.high is not None:
            return (self.low + self.high) / 2
        return None

    @property
    def dispersion(self) -> float | None:
        """Half-width of the consensus range as a fraction of its midpoint.

        A proxy for how much analysts disagree, and therefore for how large Wall
        Street's error is likely to be - which is the denominator we are divided by.
        """
        mid = self.midpoint
        if self.low is None or self.high is None or not mid:
            return None
        return (self.high - self.low) / 2 / abs(mid)

    def implied_target(self) -> float | None:
        """Where the company says it will land, expressed as a number."""
        mid = self.midpoint
        if mid is None:
            return None
        weight = POSITION_WEIGHT.get(self.position, 0.0)
        if not weight:
            return mid
        if self.low is not None and self.high is not None:
            half = (self.high - self.low) / 2
            return mid + weight * half
        # No range published, so nudge by a modest fraction of the level itself.
        return mid * (1 + weight * 0.03)

    def describe(self) -> str:
        if not self.found:
            return "no stated consensus found"
        parts = []
        if self.low is not None and self.high is not None:
            parts.append(f"range {self.low:g}-{self.high:g} (midpoint {self.midpoint:g}"
                         f", dispersion {self.dispersion:.1%})")
        elif self.value is not None:
            parts.append(f"stated at {self.value:g}")
        if self.position != "unstated":
            parts.append(f"company says it lands: {self.position}")
            target = self.implied_target()
            if target is not None:
                parts.append(f"implied target {target:.6g}")
        return "; ".join(parts)


def _cache_path(metric: Metric):
    digest = hashlib.sha256((metric.key + CONSENSUS_SYSTEM).encode()).hexdigest()[:12]
    return CONSENSUS_CACHE / f"{metric.ticker}-{digest}.json"


def find(metric: Metric, use_cache: bool = True) -> Consensus:
    cache_file = _cache_path(metric)
    if use_cache and cache_file.exists():
        return Consensus(**json.loads(cache_file.read_text(encoding="utf-8")))

    build_index()
    chunks, seen = [], set()
    for query in (
        f"market consensus expectations for {metric.label} company compiled",
        f"in line with consensus range analysts expect {metric.label}",
        f"at the top of the consensus range {metric.label} we currently expect",
    ):
        for doc, _s, chunk in search(query, ticker=metric.ticker, since="2025-01-01", k=6):
            key = f"{doc.path}:{hash(chunk)}"
            if key in seen:
                continue
            seen.add(key)
            chunks.append(f"<<<{doc.path} | published {doc.published_at} | "
                          f"period {doc.period}>>>\n{chunk}")

    result = Consensus(metric_key=metric.key)
    if chunks:
        body = "\n\n".join(chunks[:16])
        haystack = " ".join(body.split()).lower()
        payload = complete(
            CONSENSUS_SYSTEM,
            f"Company: {metric.company} ({metric.ticker})\n"
            f"Metric: {metric.label} ({metric.units})\n"
            f"Target period: {metric.period}\n\n{body}",
            CONSENSUS_SCHEMA, schema_name="consensus",
        )
        # Prefer a statement about the target period; the newest wins, because a
        # later statement supersedes an earlier one.
        best = None
        for item in payload.get("statements", []):
            quote = " ".join((item.get("quote") or "").split()).lower()
            if not quote or quote[:45] not in haystack:
                continue
            if metric.period.replace("FY", "") not in item.get("period", "").replace("FY", ""):
                continue
            best = item
        if best:
            result = Consensus(
                metric_key=metric.key, value=best.get("value"),
                low=best.get("low"), high=best.get("high"),
                position=best.get("position", "unstated"), quote=best.get("quote", ""),
            )

    CONSENSUS_CACHE.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(asdict(result), indent=1), encoding="utf-8")
    return result
