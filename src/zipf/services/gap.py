"""Competitor keyword gap. Tier 1, paid.

The question is "what does a competitor rank for that I do not". DataForSEO
answers it directly: ``domain_intersection`` with ``intersections=False`` returns
keywords the first target ranks for and the second does not.

That is why ``target1`` is the competitor and ``target2`` is your own domain.
Reversing them silently answers the opposite question, so the two are named
rather than positional at every call site.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from zipf import capabilities
from zipf.errors import InvalidRequestError
from zipf.jobs import queue
from zipf.pricing import PriceEstimate
from zipf.services import browse, freshness
from zipf.services.cluster import Cluster, cluster_rows
from zipf.sources.dataforseo import labs


@dataclass(frozen=True)
class GapPlan:
    competitor: str
    mine: str
    limit: int
    estimate: PriceEstimate
    params: dict[str, Any]
    #: Age of the stored pull that already covers this request, if any.
    age: timedelta | None = None

    @property
    def is_fresh(self) -> bool:
        """Whether this request is already answered by data we own."""
        return self.age is not None

    @property
    def is_free(self) -> bool:
        return self.is_fresh


def _stored_age(
    conn: sqlite3.Connection, params: Mapping[str, Any], ttl: timedelta
) -> timedelta | None:
    """Age of the newest stored pull that already covers ``params``.

    Matches on the domain pair rather than on a params hash, and accepts a stored
    pull whose ``limit`` was at least as deep as the one requested. A hash match
    would treat a previous 1,000-row pull as useless to a 100-row request and
    charge for rows already owned.

    ``intersections`` is part of the match because the same endpoint answers two
    opposite questions: false is "theirs and not yours", true is "both". A stored
    intersection would otherwise be read as a gap.
    """
    return freshness.covering_age(
        conn,
        labs.DOMAIN_INTERSECTION,
        ttl=ttl,
        where=(
            freshness.equals("target1", params["target1"]),
            freshness.equals("target2", params["target2"]),
            freshness.equals("intersections", 0),
            freshness.at_least("limit", params["limit"]),
        ),
    )


def plan(
    conn: sqlite3.Connection,
    competitor: str,
    mine: str,
    *,
    limit: int = labs.DEFAULT_LIMIT,
    force: bool = False,
) -> GapPlan:
    """Price a gap pull without spending anything.

    Checks what is already stored first, so reading a gap you already own costs
    nothing and asks nothing.

    The estimate is a **worst case**: it prices the requested ``limit``, but the
    vendor bills for rows actually returned. A 100-row request that comes back
    empty is charged the base fee alone. Over-estimating is the safe direction
    and the only possible one, since the row count cannot be known before the
    call; ``zipf jobs show`` reports the difference.
    """
    competitor_clean = competitor.strip().lower()
    mine_clean = mine.strip().lower()
    if not competitor_clean or not mine_clean:
        raise InvalidRequestError(
            "A gap needs two domains: theirs and yours.",
            fix="Pass a competitor domain, and set own_domain in config or pass --mine.",
        )
    if competitor_clean == mine_clean:
        raise InvalidRequestError(
            f"{competitor_clean} cannot be compared with itself.",
            fix="Pass a competitor domain different from your own.",
        )

    capped = min(limit, labs.MAX_LIMIT)
    params: dict[str, Any] = {
        "target1": competitor_clean,
        "target2": mine_clean,
        "limit": capped,
        "intersections": False,
    }
    ttl = capabilities.get(labs.DOMAIN_INTERSECTION).ttl
    return GapPlan(
        competitor=competitor_clean,
        mine=mine_clean,
        limit=capped,
        estimate=labs.price_domain_intersection(params),
        params=params,
        age=None if force else _stored_age(conn, params, ttl),
    )


def enqueue(conn: sqlite3.Connection, gap_plan: GapPlan) -> int:
    """Queue the pull. Returns the job id. Spends nothing (R5)."""
    return queue.enqueue(
        conn,
        labs.DOMAIN_INTERSECTION,
        gap_plan.params,
        estimated_cost=gap_plan.estimate.usd,
    )


def read_rows(conn: sqlite3.Connection, competitor: str, mine: str) -> list[dict[str, Any]]:
    """Every current gap keyword, unclustered, best volume first.

    The gap is the *absence* of a row for your domain, which is why this is a
    NOT EXISTS rather than a comparison against a position value.

    The newest-observation join comes from ``browse`` rather than being repeated
    here. ``ranks.read_rows`` already delegates for the same reason: a second copy
    of that subquery is a second place for it to drift.
    """
    rows = conn.execute(
        "SELECT dk.keyword, dk.position, dk.url, k.volume, k.cpc, dk.observed_at "
        "FROM domain_keyword dk "
        f"{browse.LATEST_RANK_JOIN} "
        "LEFT JOIN keyword k ON k.keyword = dk.keyword "
        "WHERE dk.domain = ? "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM domain_keyword mine "
        "  WHERE mine.domain = ? AND mine.keyword = dk.keyword"
        ") "
        "ORDER BY CASE WHEN k.volume IS NULL THEN 1 ELSE 0 END, k.volume DESC, dk.position",
        (competitor.strip().lower(), mine.strip().lower()),
    ).fetchall()
    return [dict(row) for row in rows]


def read(conn: sqlite3.Connection, competitor: str, mine: str, limit: int = 50) -> list[Cluster]:
    """Gap keywords with restatements of the same query collapsed together.

    ``limit`` counts distinct queries, not rows. A hundred bought rows can be
    twenty real opportunities, and the count that matters is the one you could
    actually write about.
    """
    return cluster_rows(read_rows(conn, competitor, mine))[:limit]
