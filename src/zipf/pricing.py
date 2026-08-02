"""Price declaration.

Cost is computed from the tier and the requested depth *before* a call is made.
It is never discovered from an invoice, and never inferred from a response.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Queue = Literal["none", "standard", "live"]


@dataclass(frozen=True)
class PriceEstimate:
    """What a call is expected to cost, and how it will be run."""

    usd: float
    tier: int
    queue: Queue = "none"
    rows: int | None = None

    @property
    def is_free(self) -> bool:
        return self.usd == 0.0

    def describe(self) -> str:
        rows = f"~{self.rows:,} rows" if self.rows is not None else "unknown rows"
        cost = "free" if self.is_free else f"${self.usd:.4f}"
        return f"tier {self.tier} · {rows} · {cost} · {self.queue} queue"


def free(tier: int = 0, rows: int | None = None) -> PriceEstimate:
    """A tier-0 call. Still priced, still recorded, so a source that starts
    charging needs no new plumbing."""
    return PriceEstimate(usd=0.0, tier=tier, queue="none", rows=rows)


class Plan(Protocol):
    """What every paid service's plan exposes, whatever else it carries.

    Two members, both of which the four plan types already had before anything
    named them. ``VolumePlan`` also carries batches and a stale list, and the
    rank plans carry an age; none of that is a purchase decision's business.

    Lives here rather than in either shell because both take the same decision
    over the same plans, and a protocol in one shell would make the other import
    it across a boundary that is meant to stay closed.
    """

    @property
    def is_free(self) -> bool:
        """Whether this request is already answered by data we own."""
        ...

    @property
    def estimate(self) -> PriceEstimate: ...
