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
from typing import Any, Final

from zipf.errors import InvalidRequestError
from zipf.services import freshness, volume
from zipf.sources.dataforseo import labs

#: Default page size. Comfortably more than fits on a screen, so scrolling works
#: without a second query, and far below where a DataTable starts to feel slow.
DEFAULT_LIMIT: Final = 500

#: Hard ceiling on any single browse query, whatever the caller asks for.
MAX_ROWS: Final = 5_000

#: Sort keys for the keyword table, mapped to SQL. NULL volumes sort last in
#: both directions: a keyword whose volume was never bought is not "the smallest
#: volume", it is an unknown, and burying it under real data is misleading.
#: Each key puts the most interesting end first: the biggest volume, the *lowest*
#: difficulty, the highest cpc. Difficulty is the one that runs ascending, because
#: an easy keyword is the good one and a sort that buried it under 90s would be
#: answering the opposite question.
KEYWORD_SORTS: Final[dict[str, str]] = {
    "volume": "CASE WHEN k.volume IS NULL THEN 1 ELSE 0 END, k.volume DESC, k.keyword",
    "difficulty": "CASE WHEN k.difficulty IS NULL THEN 1 ELSE 0 END, k.difficulty, k.keyword",
    "cpc": "CASE WHEN k.cpc IS NULL THEN 1 ELSE 0 END, k.cpc DESC, k.keyword",
    "intent": "CASE WHEN k.intent IS NULL THEN 1 ELSE 0 END, k.intent, k.keyword",
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

RESPONSE_SORTS: Final[dict[str, str]] = {
    "fetched": "fetched_at DESC, id DESC",
    "cost": "cost_usd DESC, fetched_at DESC",
    "bytes": "bytes DESC, fetched_at DESC",
}

#: Every table that carries a ``raw_id``, which is every projection. Listed here
#: because "what did this response produce" has to ask each of them, and a table
#: missing from this list would silently look like it was never written to.
PROJECTED_TABLES: Final[tuple[str, ...]] = (
    "keyword",
    "keyword_month",
    "domain_keyword",
    "gsc_query",
    "observation",
)


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


def keyword_count(conn: sqlite3.Connection) -> int:
    """Count the keywords named in the persistent status bar."""
    row = conn.execute("SELECT COUNT(*) AS count FROM keyword").fetchone()
    return int(row["count"])


def keywords(
    conn: sqlite3.Connection,
    *,
    contains: str | None = None,
    sort: str | None = None,
    limit: int = DEFAULT_LIMIT,
    own_domain: str | None = None,
    watchlisted_only: bool = False,
) -> list[dict[str, Any]]:
    """Every keyword in the cache, with your own rank where one is known.

    ``own_domain`` is passed in rather than read from settings so the same
    function serves the configured domain, a domain typed at the prompt, and a
    test. When it is absent the ``position`` column is simply NULL — the rest of
    the row is unaffected. ``watchlisted_only`` narrows the same query through
    durable user state without changing its columns or ordering.
    """
    order = _order_by(KEYWORD_SORTS, sort)
    params: list[Any] = [own_domain]
    conditions: list[str] = []
    if watchlisted_only:
        conditions.append("EXISTS (SELECT 1 FROM watchlist w WHERE w.keyword = k.keyword)")
    if contains:
        conditions.append("k.keyword LIKE ? ESCAPE '\\'")
        params.append(_contains(contains))
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(_bounded(limit))

    rows = conn.execute(
        f"""
        SELECT k.keyword, k.volume, k.cpc, k.competition, k.has_aio, k.updated_at,
               k.raw_id, k.difficulty, k.intent, k.intent_probability, own.position
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


def stale_keywords(
    conn: sqlite3.Connection,
    *,
    contains: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Keywords that were measured once and whose measurement has aged out.

    Deliberately *not* every keyword lacking a fresh volume. A keyword
    autocomplete merely suggested has never been measured at all, which is a
    different question — "buy data I do not have" rather than "refresh data I
    do" — and folding the two together would quote a batch price for work the
    planner is not proposing.

    The join to ``raw_response`` is what draws that line, and it is the same join
    ``volume.fresh_keywords`` uses to draw it from the other side.
    """
    cutoff = freshness.stale_before(volume.VOLUME_TTL)
    capability_slots = ",".join("?" for _ in volume.MEASURING_CAPABILITIES)
    params: list[Any] = [*volume.MEASURING_CAPABILITIES, cutoff]
    where = ""
    if contains:
        where = "AND k.keyword LIKE ? ESCAPE '\\'"
        params.append(_contains(contains))
    params.append(_bounded(limit))

    rows = conn.execute(
        f"""
        SELECT k.keyword, k.volume, k.cpc, k.updated_at, k.raw_id,
               r.capability, r.fetched_at AS measured_at
        FROM keyword k
        JOIN raw_response r ON r.id = k.raw_id
        WHERE r.capability IN ({capability_slots})
          AND k.updated_at < ?
          {where}
        ORDER BY CASE WHEN k.volume IS NULL THEN 1 ELSE 0 END, k.volume DESC, k.keyword
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def responses(
    conn: sqlite3.Connection,
    *,
    contains: str | None = None,
    sort: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """The acquisition ledger: what was bought, when, and for how much.

    ``body`` is measured rather than selected. It is the whole vendor response
    and can run to megabytes; reading fifty of them to render a size column would
    make opening this view cost more than every other browse query combined.
    """
    order = _order_by(RESPONSE_SORTS, sort)
    params: list[Any] = []
    where = ""
    if contains:
        where = "WHERE capability LIKE ? ESCAPE '\\' OR params_json LIKE ? ESCAPE '\\'"
        params += [_contains(contains), _contains(contains)]
    params.append(_bounded(limit))

    rows = conn.execute(
        f"""
        SELECT id, capability, params_json, params_hash, cost_usd, fetched_at,
               LENGTH(body) AS bytes
        FROM raw_response
        {where}
        ORDER BY {order}
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def response_detail(conn: sqlite3.Connection, response_id: int) -> dict[str, Any] | None:
    """One stored response, and every row downstream of it.

    This is the ``raw_id`` edge walked forwards. It is the claim the whole tool
    rests on — bought once, owned forever, and rebuildable — so the counts come
    from the projections themselves rather than from anything recorded at fetch
    time, which could drift from what is actually stored.
    """
    row = conn.execute(
        "SELECT id, capability, params_json, params_hash, cost_usd, fetched_at, "
        "LENGTH(body) AS bytes FROM raw_response WHERE id = ?",
        (response_id,),
    ).fetchone()
    if row is None:
        return None

    detail = dict(row)
    # One query per projected table. The list is fixed and short, and each is an
    # indexed count, so this stays well inside a keypress.
    detail["projects"] = [
        {"table": table, "rows": count}
        for table in PROJECTED_TABLES
        if (
            count := int(
                conn.execute(
                    f"SELECT COUNT(*) AS rows FROM {table} WHERE raw_id = ?",
                    (response_id,),
                ).fetchone()["rows"]
            )
        )
    ]
    detail["job"] = conn.execute(
        "SELECT id, status, estimated_cost, actual_cost FROM job WHERE raw_id = ? LIMIT 1",
        (response_id,),
    ).fetchone()
    return detail


def keyword_detail(conn: sqlite3.Connection, keyword: str) -> dict[str, Any] | None:
    """Everything the cache knows about one keyword, for the detail pane.

    Returns ``None`` when the keyword has no projection row at all, which the
    caller must distinguish from a keyword that exists with nothing bought yet.
    """
    target = keyword.strip().lower()
    row = conn.execute(
        "SELECT keyword, volume, cpc, competition, has_aio, updated_at, raw_id, "
        "difficulty, intent, intent_probability "
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

    # The monthly series, in time order. Bounded to two years: the vendor returns
    # twelve months and a re-pull adds another twelve, and a sparkline of an
    # unbounded series would compress the recent months into nothing.
    months = conn.execute(
        "SELECT year, month, volume FROM keyword_month WHERE keyword = ? "
        "ORDER BY year DESC, month DESC LIMIT 24",
        (target,),
    ).fetchall()

    detail = dict(row)
    detail["ranks"] = [dict(rank) for rank in ranks]
    detail["gsc"] = dict(gsc) if gsc["impressions"] else None
    detail["months"] = [dict(month) for month in reversed(months)]
    detail["source"] = (
        conn.execute(
            "SELECT capability, cost_usd, fetched_at FROM raw_response WHERE id = ?",
            (row["raw_id"],),
        ).fetchone()
        if row["raw_id"] is not None
        else None
    )
    return detail
