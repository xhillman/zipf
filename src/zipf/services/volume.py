"""Search volume. Tier 1, paid.

Cache-aware batching (spec D13). Labs charges a $0.012 base plus $0.00012 per
row, so the expensive mistake is many small calls, not one big one. This service
asks the ``keyword`` projection which terms are already fresh and buys only the
remainder, in as few calls as the vendor allows.

Planning is separate from spending. ``plan`` reads local tables and costs
nothing; ``enqueue`` queues the work; the runner spends. That split is what lets
the CLI price a request, confirm it, and still honour R5.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from zipf.clock import now, to_iso
from zipf.jobs import queue
from zipf.pricing import PriceEstimate
from zipf.services.cluster import Cluster, cluster_rows
from zipf.sources.dataforseo import labs

#: How long a measured volume is trusted. Matches the capability TTL: the
#: upstream only updates monthly, so a shorter window buys the same number back.
VOLUME_TTL = timedelta(days=30)

#: Capabilities whose responses count as a real measurement. A keyword that only
#: ever came from autocomplete has been *discovered*, not measured, and its null
#: volume is an absence rather than an answer.
MEASURING_CAPABILITIES = (labs.SEARCH_VOLUME, labs.RANKED_KEYWORDS, labs.DOMAIN_INTERSECTION)


@dataclass(frozen=True)
class VolumePlan:
    """What a volume request would cost, and why."""

    requested: list[str]
    cached: list[str]
    stale: list[str]
    batches: list[list[str]]
    estimate: PriceEstimate

    @property
    def is_free(self) -> bool:
        return not self.stale


def normalise(keywords: Iterable[str]) -> list[str]:
    """Strip, lowercase, drop blanks, and de-duplicate while keeping order.

    Matches what ``fetch`` does to params, so the plan counts the same keywords
    the cache will be keyed on.
    """
    seen: dict[str, None] = {}
    for keyword in keywords:
        cleaned = keyword.strip().lower()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


def fresh_keywords(conn: sqlite3.Connection, keywords: Sequence[str]) -> set[str]:
    """Which of ``keywords`` already hold a measurement inside the TTL.

    Joins to ``raw_response`` because ``keyword.updated_at`` is also set by free
    discovery. Without the join, a keyword autocomplete merely suggested would
    look freshly measured and its volume would never be bought.
    """
    if not keywords:
        return set()

    cutoff = to_iso(now() - VOLUME_TTL)
    placeholders = ",".join("?" for _ in keywords)
    capability_slots = ",".join("?" for _ in MEASURING_CAPABILITIES)

    rows = conn.execute(
        f"SELECT k.keyword FROM keyword k "
        f"JOIN raw_response r ON r.id = k.raw_id "
        f"WHERE k.keyword IN ({placeholders}) "
        f"AND r.capability IN ({capability_slots}) "
        f"AND k.updated_at >= ?",
        (*keywords, *MEASURING_CAPABILITIES, cutoff),
    ).fetchall()
    return {row["keyword"] for row in rows}


def plan(conn: sqlite3.Connection, keywords: Iterable[str], *, force: bool = False) -> VolumePlan:
    """Price a volume request without spending anything."""
    requested = normalise(keywords)
    cached = [] if force else sorted(fresh_keywords(conn, requested))
    cached_set = set(cached)
    stale = [keyword for keyword in requested if keyword not in cached_set]

    size = labs.MAX_KEYWORDS_PER_CALL
    batches = [stale[i : i + size] for i in range(0, len(stale), size)]

    total = round(sum(labs.price_search_volume({"keywords": batch}).usd for batch in batches), 5)
    estimate = PriceEstimate(usd=total, tier=1, queue="none", rows=len(stale))

    return VolumePlan(
        requested=requested, cached=cached, stale=stale, batches=batches, estimate=estimate
    )


def enqueue(conn: sqlite3.Connection, volume_plan: VolumePlan) -> list[int]:
    """Queue one job per batch. Returns the job ids. Spends nothing (R5)."""
    return [
        queue.enqueue(
            conn,
            labs.SEARCH_VOLUME,
            {"keywords": batch},
            estimated_cost=labs.price_search_volume({"keywords": batch}).usd,
        )
        for batch in volume_plan.batches
    ]


def read_rows(conn: sqlite3.Connection, keywords: Sequence[str]) -> list[dict[str, Any]]:
    """Read whatever the projection currently holds, one row per keyword."""
    if not keywords:
        return []

    placeholders = ",".join("?" for _ in keywords)
    rows = conn.execute(
        f"SELECT keyword, volume, cpc, competition, updated_at FROM keyword "
        f"WHERE keyword IN ({placeholders}) "
        f"ORDER BY CASE WHEN volume IS NULL THEN 1 ELSE 0 END, volume DESC, keyword",
        tuple(keywords),
    ).fetchall()
    return [dict(row) for row in rows]


def read(
    conn: sqlite3.Connection, keywords: Sequence[str], limit: int | None = None
) -> list[Cluster]:
    """Volume results with restatements of one query collapsed together.

    Clustering is display only. The purchase is deliberately *not* deduplicated:
    the marginal cost of a redundant row is $0.00012 against a $0.012 per-call
    base, so collapsing the batch would save fractions of a cent while throwing
    away data for keywords the caller explicitly asked about.
    """
    clusters = cluster_rows(read_rows(conn, keywords))
    return clusters[:limit] if limit else clusters
