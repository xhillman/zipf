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
from dataclasses import dataclass
from typing import Any

from zipf.errors import InvalidRequestError
from zipf.jobs import queue
from zipf.pricing import PriceEstimate
from zipf.sources.dataforseo import labs


@dataclass(frozen=True)
class GapPlan:
    competitor: str
    mine: str
    limit: int
    estimate: PriceEstimate
    params: dict[str, Any]


def plan(competitor: str, mine: str, *, limit: int = labs.DEFAULT_LIMIT) -> GapPlan:
    """Price a gap pull without spending anything.

    The row count is the requested ``limit``, because that is what the vendor
    charges for whether or not the domain has that many keywords.
    """
    competitor_clean = competitor.strip().lower()
    mine_clean = mine.strip().lower()
    if not competitor_clean or not mine_clean:
        raise InvalidRequestError("gap needs both a competitor domain and your own domain")
    if competitor_clean == mine_clean:
        raise InvalidRequestError("gap between a domain and itself is always empty")

    capped = min(limit, labs.MAX_LIMIT)
    params: dict[str, Any] = {
        "target1": competitor_clean,
        "target2": mine_clean,
        "limit": capped,
        "intersections": False,
    }
    return GapPlan(
        competitor=competitor_clean,
        mine=mine_clean,
        limit=capped,
        estimate=labs.price_domain_intersection(params),
        params=params,
    )


def enqueue(conn: sqlite3.Connection, gap_plan: GapPlan) -> int:
    """Queue the pull. Returns the job id. Spends nothing (R5)."""
    return queue.enqueue(
        conn,
        labs.DOMAIN_INTERSECTION,
        gap_plan.params,
        estimated_cost=gap_plan.estimate.usd,
    )


def read(
    conn: sqlite3.Connection, competitor: str, mine: str, limit: int = 50
) -> list[dict[str, Any]]:
    """Keywords the competitor ranks for and you do not, best volume first.

    The gap is the *absence* of a row for your domain, which is why this is a
    NOT EXISTS rather than a comparison against a position value.
    """
    rows = conn.execute(
        "SELECT dk.keyword, dk.position, dk.url, k.volume, k.cpc, dk.observed_at "
        "FROM domain_keyword dk "
        "LEFT JOIN keyword k ON k.keyword = dk.keyword "
        "WHERE dk.domain = ? "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM domain_keyword mine "
        "  WHERE mine.domain = ? AND mine.keyword = dk.keyword"
        ") "
        "ORDER BY CASE WHEN k.volume IS NULL THEN 1 ELSE 0 END, k.volume DESC, dk.position "
        "LIMIT ?",
        (competitor.strip().lower(), mine.strip().lower(), limit),
    ).fetchall()
    return [dict(row) for row in rows]
