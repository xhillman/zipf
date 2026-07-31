"""What a domain already ranks for. Tier 1, paid.

Usually your own domain, which is why it defaults to ``own_domain``. Without it
zipf knows what competitors rank for and nothing about you — the ``pos`` column
in every keyword view joins ``domain_keyword`` for your domain and finds nothing.

Mirrors ``gap`` in shape: price without spending, check what is stored first, and
enqueue rather than fetch. Reading ranks you already own is free and promptless.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from zipf import capabilities
from zipf.clock import from_iso, now, to_iso
from zipf.errors import InvalidRequestError
from zipf.jobs import queue
from zipf.pricing import PriceEstimate
from zipf.services import browse
from zipf.services.cluster import Cluster, cluster_rows
from zipf.sources.dataforseo import labs


@dataclass(frozen=True)
class RanksPlan:
    domain: str
    limit: int
    estimate: PriceEstimate
    params: dict[str, Any]
    #: Age of the stored pull that already covers this request, if any.
    age: timedelta | None = None

    @property
    def is_fresh(self) -> bool:
        return self.age is not None

    @property
    def is_free(self) -> bool:
        return self.is_fresh


def _stored_age(
    conn: sqlite3.Connection, params: Mapping[str, Any], ttl: timedelta
) -> timedelta | None:
    """Age of the newest stored pull that already covers ``params``.

    Accepts a stored pull whose ``limit`` was at least as deep as the one asked
    for. A hash match would treat a previous 1,000-row pull as useless to a
    100-row request and charge again for rows already owned.
    """
    cutoff = to_iso(now() - ttl)
    row = conn.execute(
        "SELECT fetched_at FROM raw_response "
        "WHERE capability = ? "
        "AND json_extract(params_json, '$.domain') = ? "
        "AND json_extract(params_json, '$.limit') >= ? "
        "AND fetched_at >= ? "
        "ORDER BY fetched_at DESC LIMIT 1",
        (labs.RANKED_KEYWORDS, params["domain"], params["limit"], cutoff),
    ).fetchone()
    return now() - from_iso(row["fetched_at"]) if row is not None else None


def plan(
    conn: sqlite3.Connection,
    domain: str,
    *,
    limit: int = labs.DEFAULT_LIMIT,
    force: bool = False,
) -> RanksPlan:
    """Price a ranked-keywords pull without spending anything.

    The row count is the requested ``limit``, because that is what the vendor
    charges for whether or not the domain ranks for that many keywords.
    """
    cleaned = domain.strip().lower()
    if not cleaned:
        raise InvalidRequestError(
            "A rank pull needs a domain.",
            fix="Pass a domain, or set own_domain in your config file.",
        )

    capped = min(limit, labs.MAX_LIMIT)
    params: dict[str, Any] = {"domain": cleaned, "limit": capped}
    ttl = capabilities.get(labs.RANKED_KEYWORDS).ttl
    return RanksPlan(
        domain=cleaned,
        limit=capped,
        estimate=labs.price_ranked_keywords(params),
        params=params,
        age=None if force else _stored_age(conn, params, ttl),
    )


def enqueue(conn: sqlite3.Connection, ranks_plan: RanksPlan) -> int:
    """Queue the pull. Returns the job id. Spends nothing (R5)."""
    return queue.enqueue(
        conn,
        labs.RANKED_KEYWORDS,
        ranks_plan.params,
        estimated_cost=ranks_plan.estimate.usd,
    )


def read_rows(conn: sqlite3.Connection, domain: str) -> list[dict[str, Any]]:
    """Every current rank for this domain, best-known volume first.

    Delegates to ``browse`` rather than repeating the latest-observation join.
    ``domain_keyword`` accumulates rank history (R4), and a third copy of that
    subquery is a third place for it to drift.
    """
    return browse.domain_keywords(conn, domain, limit=browse.MAX_ROWS)


def read(conn: sqlite3.Connection, domain: str, limit: int = 50) -> list[Cluster]:
    """Ranks with restatements of the same query collapsed together."""
    return cluster_rows(read_rows(conn, domain))[:limit]
