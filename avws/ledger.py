"""The evidence ledger: an append-only record of facts extracted from documents.

Two constraints are enforced by the type rather than by discipline:

1. `basis` is required and closed. Analysts distinguish reported from adjusted
   from guided figures obsessively, because conflating them produces confident
   nonsense - ADI's Q2 GAAP diluted EPS was $2.40 while adjusted was $3.09, a 29%
   gap in the same quarter. A fact that does not declare its basis cannot exist.

2. `source_quote` is required and non-empty. An unsourced number is a bug. This
   is what makes the chain from filing to submitted figure auditable.

The ledger holds facts only. It never holds a forecast.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from avws.config import LEDGER_PATH

BASES = frozenset({
    "reported",     # a figure the company actually published for a past period
    "adjusted",     # non-GAAP / pre-exceptional variant of a published figure
    "guidance_mid", # midpoint of company guidance for a future period
    "guidance_low",
    "guidance_high",
    "derived",      # computed by us from other facts; derivation must be recorded
})


@dataclass(frozen=True)
class Fact:
    metric_key: str
    company: str
    period: str
    value: float
    unit: str
    basis: str
    source_doc: str
    source_quote: str
    confidence: float = 0.8
    label: str = ""
    recorded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        if self.basis not in BASES:
            raise ValueError(
                f"unknown basis {self.basis!r}; must be one of {sorted(BASES)}"
            )
        if not self.source_quote or not self.source_quote.strip():
            raise ValueError("source_quote is required: an unsourced fact is a bug")
        if not self.source_doc or not self.source_doc.strip():
            raise ValueError("source_doc is required")
        if self.value is None or self.value != self.value:
            raise ValueError(f"non-finite fact value: {self.value!r}")


def _path() -> Path:
    return LEDGER_PATH


def reset() -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


_write_lock = threading.Lock()


def append(fact: Fact) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # The final run processes companies concurrently, so appends are serialised.
    # Interleaved partial lines would corrupt the ledger the whole audit rests on.
    with _write_lock, path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(fact), ensure_ascii=False) + "\n")


def append_all(facts: list[Fact]) -> None:
    for fact in facts:
        append(fact)


def all_facts() -> list[Fact]:
    path = _path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(Fact(**json.loads(line)))
    return out


def facts_for(metric_key: str) -> list[Fact]:
    return [f for f in all_facts() if f.metric_key == metric_key]


def history(metric_key: str) -> list[float]:
    """Past reported values for a metric, oldest first. Used for scale bands,
    seasonality and the backtest."""
    facts = [f for f in facts_for(metric_key) if f.basis in ("reported", "adjusted")]
    facts.sort(key=lambda f: f.period)
    return [f.value for f in facts]
