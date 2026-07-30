"""Gap freshness: reading owned data must be free.

`zipf gap` used to prompt for payment even when the pull was already stored,
because the plan never looked at what was owned. These tests pin the fixed
behaviour, including the case that prevents paying twice for the same rows.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from zipf.clock import now, to_iso
from zipf.services import gap
from zipf.sources.dataforseo import labs


def _store_pull(
    conn: sqlite3.Connection,
    *,
    target1: str = "them.com",
    target2: str = "mine.com",
    limit: int = 100,
    intersections: bool = False,
    age: timedelta = timedelta(hours=1),
) -> None:
    """Insert a raw_response shaped like one fetch would have written."""
    params: dict[str, Any] = {
        "intersections": intersections,
        "limit": limit,
        "target1": target1,
        "target2": target2,
    }
    fetched_at = to_iso(now() - age)
    conn.execute(
        "INSERT INTO raw_response "
        "(capability, params_hash, params_json, body, cost_usd, fetched_at) "
        "VALUES (?, ?, ?, x'00', 0.024, ?)",
        (labs.DOMAIN_INTERSECTION, f"h{limit}{fetched_at}", json.dumps(params), fetched_at),
    )


def test_a_stored_pull_makes_the_plan_free(db: sqlite3.Connection) -> None:
    """The whole point: reading a gap you own asks for nothing."""
    _store_pull(db)

    plan = gap.plan(db, "them.com", "mine.com", limit=100)

    assert plan.is_fresh is True
    assert plan.is_free is True
    assert plan.age is not None


def test_no_stored_pull_means_the_plan_costs_money(db: sqlite3.Connection) -> None:
    plan = gap.plan(db, "them.com", "mine.com", limit=100)

    assert plan.is_fresh is False
    assert plan.estimate.usd > 0


def test_a_pull_past_its_ttl_is_not_fresh(db: sqlite3.Connection) -> None:
    """The capability TTL is 7 days, so an 8-day-old pull must be re-bought."""
    _store_pull(db, age=timedelta(days=8))

    assert gap.plan(db, "them.com", "mine.com", limit=100).is_fresh is False


def test_a_deeper_stored_pull_satisfies_a_shallower_request(db: sqlite3.Connection) -> None:
    """Owning 1,000 rows must not require buying 100 of them again.

    This is why freshness matches on the domain pair rather than a params hash:
    the hashes differ, but the data is already owned.
    """
    _store_pull(db, limit=1000)

    assert gap.plan(db, "them.com", "mine.com", limit=100).is_fresh is True


def test_a_shallower_stored_pull_does_not_satisfy_a_deeper_request(db: sqlite3.Connection) -> None:
    _store_pull(db, limit=100)

    assert gap.plan(db, "them.com", "mine.com", limit=1000).is_fresh is False


def test_force_ignores_a_stored_pull(db: sqlite3.Connection) -> None:
    _store_pull(db)

    assert gap.plan(db, "them.com", "mine.com", limit=100, force=True).is_fresh is False


def test_an_overlap_pull_does_not_satisfy_a_gap_request(db: sqlite3.Connection) -> None:
    """intersections=True answers the opposite question and must not count."""
    _store_pull(db, intersections=True)

    assert gap.plan(db, "them.com", "mine.com", limit=100).is_fresh is False


def test_freshness_is_specific_to_the_domain_pair(db: sqlite3.Connection) -> None:
    _store_pull(db, target1="them.com", target2="mine.com")

    assert gap.plan(db, "other.com", "mine.com", limit=100).is_fresh is False
    assert gap.plan(db, "them.com", "other.com", limit=100).is_fresh is False


def test_direction_matters(db: sqlite3.Connection) -> None:
    """A gap is directional: what they have and I do not, not the reverse."""
    _store_pull(db, target1="them.com", target2="mine.com")

    assert gap.plan(db, "mine.com", "them.com", limit=100).is_fresh is False


def test_domains_are_normalised_before_matching(db: sqlite3.Connection) -> None:
    _store_pull(db, target1="them.com")

    assert gap.plan(db, "  THEM.com ", "mine.com", limit=100).is_fresh is True


def test_the_newest_covering_pull_sets_the_age(db: sqlite3.Connection) -> None:
    _store_pull(db, age=timedelta(days=6))
    _store_pull(db, age=timedelta(hours=2))

    plan = gap.plan(db, "them.com", "mine.com", limit=100)

    assert plan.age is not None
    assert plan.age < timedelta(hours=3), "an older pull won over a newer one"
