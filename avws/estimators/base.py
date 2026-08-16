"""The common estimate type.

`derivation` is the load-bearing field. It is a human-readable arithmetic trace
such as:

    guidance midpoint 3900.0 USDm x (1 + 0.0150 measured residual) = 3958.5

It is what appears in the evidence report and what a judge reads when asking how
a number was produced. An estimate whose derivation reads "the model said so" has
failed the brief regardless of its accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from avws.ledger import Fact


@dataclass
class Estimate:
    metric_key: str
    value: float
    method: str
    derivation: str
    assumptions: dict[str, float] = field(default_factory=dict)
    inputs: list[Fact] = field(default_factory=list)
    confidence: float = 0.5
    warnings: list[str] = field(default_factory=list)

    def cite(self) -> list[str]:
        seen: list[str] = []
        for fact in self.inputs:
            if fact.source_doc not in seen:
                seen.append(fact.source_doc)
        return seen
