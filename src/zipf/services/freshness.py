"""Is a stored response already answering this request?

Three services asked this and each answered it separately, which meant three
places for one rule to drift — and drift here is not a display bug, it is buying
data you already own.

The rule is deliberately *not* a params-hash match, which is what ``fetch`` uses.
A hash is exact, and exactness is wrong here: a stored 1,000-row pull of a domain
fully answers a later 100-row request for the same domain, but hashes
differently. So a request is covered when the stored params *satisfy* it rather
than equal it, which is what ``at_least`` exists to express.

Predicates are built rather than passed as strings. The JSON path and the
comparison both reach SQL by interpolation — SQLite cannot parameterise either —
so the operator comes from a two-entry allowlist here rather than from a caller.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from zipf.clock import from_iso, now, to_iso


@dataclass(frozen=True)
class Predicate:
    """One condition on a stored response's params."""

    path: str
    operator: Literal["=", ">="]
    value: Any

    @property
    def sql(self) -> str:
        return f"json_extract(params_json, '$.{self.path}') {self.operator} ?"


def equals(path: str, value: Any) -> Predicate:
    """The stored request asked for exactly this."""
    return Predicate(path=path, operator="=", value=value)


def at_least(path: str, value: Any) -> Predicate:
    """The stored request went at least this deep, so it covers a shallower one."""
    return Predicate(path=path, operator=">=", value=value)


def stale_before(ttl: timedelta) -> str:
    """The timestamp a response of this capability must be newer than.

    The one piece of arithmetic every freshness check needs. Shared even where
    the surrounding query is not, because a TTL applied inconsistently is the
    failure that costs money rather than correctness.
    """
    return to_iso(now() - ttl)


def covering_age(
    conn: sqlite3.Connection,
    capability: str,
    *,
    ttl: timedelta,
    where: Sequence[Predicate] = (),
) -> timedelta | None:
    """Age of the newest stored response that covers this request, if one exists.

    Returns ``None`` when nothing covers it, which every caller reads as "this
    has to be bought". Newest first, so a re-pull supersedes an older one without
    the older one having to be removed.
    """
    conditions = "".join(f" AND {predicate.sql}" for predicate in where)
    row = conn.execute(
        "SELECT fetched_at FROM raw_response "
        f"WHERE capability = ? AND fetched_at >= ?{conditions} "
        "ORDER BY fetched_at DESC LIMIT 1",
        (capability, stale_before(ttl), *(predicate.value for predicate in where)),
    ).fetchone()
    return now() - from_iso(row["fetched_at"]) if row is not None else None
