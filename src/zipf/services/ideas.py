"""Keyword discovery. Tier 1, paid, and the only flat-fee call zipf makes.

Every other purchase here is a base fee plus a per-row charge, so asking for less
costs less. This one is $0.09 whether thirty keywords come back or twenty
thousand. That single fact drives the whole design: seeds are batched, a stored
call that already covers your seeds is read rather than re-bought, and the
command says how many seeds are going in one charge.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from statistics import median
from typing import Any, Final

from zipf import capabilities
from zipf.clock import from_iso, now
from zipf.jobs import queue
from zipf.pricing import PriceEstimate
from zipf.services import freshness
from zipf.sources.dataforseo import keywords_data

#: How many stored pulls to examine when looking for one that already covers a
#: request. Bounded because this scans candidates in Python rather than in SQL —
#: a JSON array cannot be compared as a set by SQLite.
MAX_CANDIDATES: Final = 200

#: A month counts as a seasonal peak when it is this many times the median month.
#: Below it, a series is noise around a level and naming a "peak" would invent a
#: pattern. Set from what a genuinely seasonal term looks like: a Christmas or
#: Mother's Day query runs 3 to 10 times its off-season months.
PEAK_RATIO: Final = 2.0

_MONTHS: Final = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)  # fmt: skip


@dataclass(frozen=True)
class IdeasPlan:
    seeds: list[str]
    estimate: PriceEstimate
    params: dict[str, Any]
    #: Age of the stored pull that already covers these seeds, if any.
    age: timedelta | None = None
    #: The seeds that stored pull was made with. Usually a superset.
    covered_by: list[str] | None = None

    @property
    def is_fresh(self) -> bool:
        return self.age is not None

    @property
    def is_free(self) -> bool:
        return self.is_fresh


@dataclass(frozen=True)
class Covering:
    """A stored pull that already answers a request."""

    raw_id: int
    age: timedelta
    seeds: list[str]


def _covering_pull(
    conn: sqlite3.Connection, seeds: Sequence[str], ttl: timedelta
) -> Covering | None:
    """The newest stored pull whose seeds include every seed asked for.

    A superset counts. Buying ``[crm, project management]`` and later asking for
    ``[crm]`` alone should not cost another $0.09: the suggestions that call
    produced are already stored, and the vendor does not attribute a suggestion
    back to the seed that produced it, so there is nothing finer to buy.

    Candidates are compared in Python because SQLite cannot test one JSON array
    for being a superset of another, which is why this is the one freshness check
    that cannot go through ``freshness.covering_age``. It shares the TTL cutoff
    with the rest so at least that cannot drift; the scan is bounded by
    ``MAX_CANDIDATES``.
    """
    cutoff = freshness.stale_before(ttl)
    rows = conn.execute(
        "SELECT id, fetched_at, json_extract(params_json, '$.seeds') AS seeds "
        "FROM raw_response WHERE capability = ? AND fetched_at >= ? "
        "ORDER BY fetched_at DESC LIMIT ?",
        (keywords_data.CAPABILITY, cutoff, MAX_CANDIDATES),
    ).fetchall()

    wanted = set(seeds)
    for row in rows:
        stored = set(json.loads(row["seeds"] or "[]"))
        if stored >= wanted:
            return Covering(
                raw_id=int(row["id"]),
                age=now() - from_iso(row["fetched_at"]),
                seeds=sorted(stored),
            )
    return None


def plan(conn: sqlite3.Connection, seeds: Iterable[str], *, force: bool = False) -> IdeasPlan:
    """Price a discovery call without spending anything.

    Seeds are sorted into the params so that typing them in a different order is
    the same purchase. At a flat fee, an accidental re-buy costs the whole $0.09
    rather than a few thousandths of a cent.
    """
    cleaned = sorted(keywords_data.normalise_seeds(list(seeds)))
    params: dict[str, Any] = {"seeds": cleaned}
    ttl = capabilities.get(keywords_data.CAPABILITY).ttl

    covering = None if force else _covering_pull(conn, cleaned, ttl)
    return IdeasPlan(
        seeds=cleaned,
        estimate=keywords_data.price(params),
        params=params,
        age=covering.age if covering else None,
        covered_by=covering.seeds if covering else None,
    )


def enqueue(conn: sqlite3.Connection, ideas_plan: IdeasPlan) -> int:
    """Queue the pull. Returns the job id. Spends nothing (R5)."""
    return queue.enqueue(
        conn,
        keywords_data.CAPABILITY,
        ideas_plan.params,
        estimated_cost=ideas_plan.estimate.usd,
    )


def peak_month(monthly: Sequence[dict[str, int]]) -> str:
    """The month a keyword peaks in, when it genuinely has a peak.

    Returns empty for a keyword that is level all year, which is most of them.
    Naming a "peak" on flat data would turn ordinary month-to-month noise into a
    recommendation about when to publish.
    """
    volumes = [entry["volume"] for entry in monthly if entry.get("volume")]
    if len(volumes) < 6:
        return ""

    middle = median(volumes)
    if middle <= 0:
        return ""

    best = max(monthly, key=lambda entry: entry["volume"])
    if best["volume"] < middle * PEAK_RATIO:
        return ""
    return _MONTHS[best["month"] - 1] if 1 <= best["month"] <= 12 else ""


def read_rows(
    conn: sqlite3.Connection, seeds: Sequence[str], limit: int = 100
) -> list[dict[str, Any]]:
    """What a stored discovery call actually returned, best volume first.

    Read back out of the stored response rather than out of ``keyword``: that
    table holds every keyword from every source, and there is no column saying
    which discovery call introduced one.
    """
    ttl = capabilities.get(keywords_data.CAPABILITY).ttl
    covering = _covering_pull(conn, sorted(seeds), ttl)
    if covering is None:
        return []

    row = conn.execute("SELECT body FROM raw_response WHERE id = ?", (covering.raw_id,)).fetchone()
    if row is None:
        return []

    records = keywords_data.parse(row["body"], {})
    for record in records:
        record["peak"] = peak_month(record.get("monthly") or [])
    records.sort(key=lambda record: record.get("volume") or 0, reverse=True)
    return records[:limit]
