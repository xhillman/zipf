"""Inventory queries for the cache browser.

Every other read in ``services/`` is query-shaped: you already know which
keywords or which domain pair you want, and ``read_rows`` fetches exactly those.
Browsing is the opposite — *what do I have at all* — so it needs its own
functions rather than optional arguments bolted onto the existing ones.

Three rules hold throughout, because the caller is a UI driven by typed input:

1. **Read-only.** Nothing here writes, and the browser's connection is opened
   ``mode=ro`` so SQLite refuses a write regardless.
2. **Bounded.** Every query carries a ``LIMIT``, capped at ``MAX_ROWS``. A table
   widget handed 50,000 rows stalls long before SQLite does.
3. **Column names come from an allowlist, never from input.** SQLite cannot
   parameterise an identifier, so a sort key is looked up rather than
   interpolated.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Final

from zipf.errors import InvalidRequestError
from zipf.sources.dataforseo import labs

#: Default page size. Comfortably more than fits on a screen, so scrolling works
#: without a second query, and far below where a DataTable starts to feel slow.
DEFAULT_LIMIT: Final = 500

#: Hard ceiling on any single browse query, whatever the caller asks for.
MAX_ROWS: Final = 5_000

#: Sort keys for the keyword table, mapped to SQL. NULL volumes sort last in
#: both directions: a keyword whose volume was never bought is not "the smallest
#: volume", it is an unknown, and burying it under real data is misleading.
KEYWORD_SORTS: Final[dict[str, str]] = {
    "volume": "CASE WHEN k.volume IS NULL THEN 1 ELSE 0 END, k.volume DESC, k.keyword",
    "keyword": "k.keyword",
    "updated": "k.updated_at DESC, k.keyword",
    "position": "CASE WHEN own.position IS NULL THEN 1 ELSE 0 END, own.position, k.keyword",
}

DOMAIN_SORTS: Final[dict[str, str]] = {
    "keywords": "keywords DESC, domain",
    "domain": "domain",
    "observed": "last_observed DESC, domain",
}

#: Narrows a query over ``domain_keyword dk`` to each keyword's newest
#: observation. ``domain_keyword`` accumulates a rank history (R4), so without
#: this a second pull lists every keyword once per pull rather than once with its
#: current rank.
#:
#: Exported because ``gap`` needs the same restriction over its own SELECT. There
#: is one copy of this join in the codebase and it lives here, next to the
#: queries that read the table most.
LATEST_RANK_JOIN = """
JOIN (
  SELECT domain, keyword, MAX(observed_at) AS latest
  FROM domain_keyword GROUP BY domain, keyword
) newest
  ON newest.domain = dk.domain
 AND newest.keyword = dk.keyword
 AND newest.latest = dk.observed_at
"""

#: The current rank per domain and keyword, as a subquery callers select from.
_LATEST_RANK = f"""
SELECT dk.domain, dk.keyword, dk.position, dk.url, dk.observed_at
FROM domain_keyword dk
{LATEST_RANK_JOIN}
"""

GSC_SORTS: Final[dict[str, str]] = {
    "clicks": "clicks DESC, query",
    "impressions": "impressions DESC, query",
    "query": "query",
    "position": "position, query",
}


@dataclass(frozen=True)
class CacheCounts:
    """What the sidebar counts. One number per bucket the tree offers."""

    keywords: int
    domains: int
    gap_pairs: int
    gsc_queries: int
    observations: int
    responses: int
    jobs_pending: int


def _bounded(limit: int) -> int:
    """Clamp a caller's page size into something a table can render."""
    return max(1, min(limit, MAX_ROWS))


def _order_by(sorts: dict[str, str], sort: str | None) -> str:
    """Resolve a sort key against an allowlist.

    Raises rather than falling back to a default: a mistyped sort key that
    silently returns a different order is worse than an error, because the caller
    believes it got the ordering it asked for. The sort key reaches here from a
    typed command, so the error names the valid keys.
    """
    if sort is None:
        return next(iter(sorts.values()))
    if sort not in sorts:
        raise InvalidRequestError(
            f"There is no way to sort by {sort!r}.",
            fix=f"Sort by one of: {', '.join(sorts)}.",
        )
    return sorts[sort]


def _contains(term: str) -> str:
    """A LIKE pattern matching ``term`` anywhere, with wildcards taken literally.

    Without escaping, filtering for ``crm_software`` would also match
    ``crm software`` — ``_`` is a single-character wildcard in SQL LIKE. Someone
    typing an underscore means an underscore.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def counts(conn: sqlite3.Connection) -> CacheCounts:
    """Row counts for every bucket, in one pass per table.

    ``observations`` is always zero until the SERP and LLM milestones land. It is
    counted anyway: a sidebar that hid the bucket would suggest the surface does
    not exist rather than that nothing has been observed yet.
    """
    row = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM keyword)                                  AS keywords,
          (SELECT COUNT(DISTINCT domain) FROM domain_keyword)             AS domains,
          (SELECT COUNT(DISTINCT json_extract(params_json, '$.target1')
                        || ' vs ' || json_extract(params_json, '$.target2'))
             FROM raw_response
            WHERE capability = ?)                                        AS gap_pairs,
          (SELECT COUNT(DISTINCT query) FROM gsc_query)                   AS gsc_queries,
          (SELECT COUNT(*) FROM observation)                              AS observations,
          (SELECT COUNT(*) FROM raw_response)                             AS responses,
          (SELECT COUNT(*) FROM job WHERE status IN ('queued', 'running')) AS jobs_pending
        """,
        (labs.DOMAIN_INTERSECTION,),
    ).fetchone()
    return CacheCounts(
        keywords=int(row["keywords"]),
        domains=int(row["domains"]),
        gap_pairs=int(row["gap_pairs"]),
        gsc_queries=int(row["gsc_queries"]),
        observations=int(row["observations"]),
        responses=int(row["responses"]),
        jobs_pending=int(row["jobs_pending"]),
    )


def keywords(
    conn: sqlite3.Connection,
    *,
    contains: str | None = None,
    sort: str | None = None,
    limit: int = DEFAULT_LIMIT,
    own_domain: str | None = None,
) -> list[dict[str, Any]]:
    """Every keyword in the cache, with your own rank where one is known.

    ``own_domain`` is passed in rather than read from settings so the same
    function serves the configured domain, a domain typed at the prompt, and a
    test. When it is absent the ``position`` column is simply NULL — the rest of
    the row is unaffected.
    """
    order = _order_by(KEYWORD_SORTS, sort)
    params: list[Any] = [own_domain]
    where = ""
    if contains:
        where = "WHERE k.keyword LIKE ? ESCAPE '\\'"
        params.append(_contains(contains))
    params.append(_bounded(limit))

    rows = conn.execute(
        f"""
        SELECT k.keyword, k.volume, k.cpc, k.competition, k.has_aio, k.updated_at,
               own.position
        FROM keyword k
        LEFT JOIN ({_LATEST_RANK}) own
          ON own.keyword = k.keyword AND own.domain = ?
        {where}
        ORDER BY {order}
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def domains(
    conn: sqlite3.Connection,
    *,
    contains: str | None = None,
    sort: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Every domain the cache holds ranks for, with how much is known about each.

    ``contains`` matches the domain name, not its keywords. Filtering a list of
    domains by their contents would return domains whose names do not match the
    term, which reads as a broken filter.
    """
    order = _order_by(DOMAIN_SORTS, sort)
    params: list[Any] = []
    where = ""
    if contains:
        where = "WHERE domain LIKE ? ESCAPE '\\'"
        params.append(_contains(contains))
    params.append(_bounded(limit))

    rows = conn.execute(
        f"""
        SELECT domain,
               COUNT(DISTINCT keyword) AS keywords,
               MIN(position)           AS best_position,
               MAX(observed_at)        AS last_observed
        FROM domain_keyword
        {where}
        GROUP BY domain
        ORDER BY {order}
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def domain_keywords(
    conn: sqlite3.Connection,
    domain: str,
    *,
    contains: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Current ranks for one domain, best-known volume first."""
    params: list[Any] = [domain.strip().lower()]
    where = ""
    if contains:
        where = "AND r.keyword LIKE ? ESCAPE '\\'"
        params.append(_contains(contains))
    params.append(_bounded(limit))

    rows = conn.execute(
        f"""
        SELECT r.keyword, r.position, r.url, r.observed_at, k.volume, k.cpc
        FROM ({_LATEST_RANK}) r
        LEFT JOIN keyword k ON k.keyword = r.keyword
        WHERE r.domain = ? {where}
        ORDER BY CASE WHEN k.volume IS NULL THEN 1 ELSE 0 END, k.volume DESC, r.position
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def gap_pairs(
    conn: sqlite3.Connection,
    *,
    contains: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Which gap pulls exist, newest first.

    Read from ``raw_response`` params rather than from a projection because a gap
    is a question that was asked, not a row that was stored. The answer lives in
    ``domain_keyword``; only the pairing records who was compared with whom.

    ``contains`` matches either side of the comparison, so filtering for your own
    domain finds every pull you are part of.
    """
    params: list[Any] = [labs.DOMAIN_INTERSECTION]
    having = ""
    if contains:
        having = "HAVING competitor LIKE ? ESCAPE '\\' OR mine LIKE ? ESCAPE '\\'"
        params += [_contains(contains), _contains(contains)]
    params.append(_bounded(limit))

    rows = conn.execute(
        f"""
        SELECT json_extract(params_json, '$.target1') AS competitor,
               json_extract(params_json, '$.target2') AS mine,
               MAX(fetched_at)                        AS last_pulled,
               COUNT(*)                               AS pulls
        FROM raw_response
        WHERE capability = ?
        GROUP BY competitor, mine
        {having}
        ORDER BY last_pulled DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def gsc_queries(
    conn: sqlite3.Connection,
    *,
    contains: str | None = None,
    sort: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Your own Search Console queries, totalled across the stored date range.

    Clicks and impressions sum. Position is weighted by impressions rather than
    averaged, because a plain average of daily averages treats a day with three
    impressions as equal to a day with three thousand.
    """
    order = _order_by(GSC_SORTS, sort)
    params: list[Any] = []
    where = ""
    if contains:
        where = "WHERE query LIKE ? ESCAPE '\\'"
        params.append(_contains(contains))
    params.append(_bounded(limit))

    rows = conn.execute(
        f"""
        SELECT query,
               SUM(clicks)                                   AS clicks,
               SUM(impressions)                              AS impressions,
               SUM(position * impressions) / SUM(impressions) AS position,
               COUNT(DISTINCT page)                          AS pages,
               MAX(date)                                     AS last_seen
        FROM gsc_query
        {where}
        GROUP BY query
        HAVING SUM(impressions) > 0
        ORDER BY {order}
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def keyword_detail(conn: sqlite3.Connection, keyword: str) -> dict[str, Any] | None:
    """Everything the cache knows about one keyword, for the detail pane.

    Returns ``None`` when the keyword has no projection row at all, which the
    caller must distinguish from a keyword that exists with nothing bought yet.
    """
    target = keyword.strip().lower()
    row = conn.execute(
        "SELECT keyword, volume, cpc, competition, has_aio, updated_at "
        "FROM keyword WHERE keyword = ?",
        (target,),
    ).fetchone()
    if row is None:
        return None

    ranks = conn.execute(
        f"SELECT domain, position, url, observed_at FROM ({_LATEST_RANK}) r "
        "WHERE r.keyword = ? ORDER BY r.position LIMIT ?",
        (target, _bounded(DEFAULT_LIMIT)),
    ).fetchall()
    gsc = conn.execute(
        "SELECT SUM(clicks) AS clicks, SUM(impressions) AS impressions, "
        "COUNT(DISTINCT page) AS pages FROM gsc_query WHERE query = ?",
        (target,),
    ).fetchone()

    detail = dict(row)
    detail["ranks"] = [dict(rank) for rank in ranks]
    detail["gsc"] = dict(gsc) if gsc["impressions"] else None
    return detail
