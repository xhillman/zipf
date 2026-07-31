"""Keyword discovery: the flat-fee call.

Two behaviours carry the milestone. A stored pull whose seeds *include* the ones
being asked for must be read rather than re-bought — at $0.09 flat, an accidental
repeat costs the whole fee rather than a fraction of a cent. And a peak month is
only named when the series actually has one, because inventing seasonality is
worse than reporting none.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

import pytest

from zipf.clock import now, to_iso
from zipf.errors import InvalidRequestError
from zipf.services import ideas
from zipf.sources.dataforseo import keywords_data


def _body(items: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        {"status_code": 20000, "cost": 0.09, "tasks": [{"status_code": 20000, "result": items}]}
    ).encode()


def _item(keyword: str, volume: int = 100, monthly: list[int] | None = None) -> dict[str, Any]:
    series = monthly if monthly is not None else [volume] * 12
    return {
        "keyword": keyword,
        "search_volume": volume,
        "cpc": 1.0,
        "competition_index": 50,
        "monthly_searches": [
            {"year": 2026, "month": month, "search_volume": value}
            for month, value in enumerate(series, start=1)
        ],
    }


def _store(
    conn: sqlite3.Connection,
    seeds: list[str],
    items: list[dict[str, Any]],
    *,
    age: timedelta = timedelta(0),
) -> int:
    cursor = conn.execute(
        "INSERT INTO raw_response (capability, params_hash, params_json, body, cost_usd, "
        "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            keywords_data.CAPABILITY,
            f"h{sorted(seeds)}",
            json.dumps({"seeds": sorted(seeds)}),
            _body(items),
            0.09,
            to_iso(now() - age),
        ),
    )
    return int(cursor.lastrowid or 0)


def test_a_fresh_request_is_priced_flat(db: sqlite3.Connection) -> None:
    one = ideas.plan(db, ["crm software"])
    twenty = ideas.plan(db, [f"seed {i}" for i in range(20)])

    assert one.estimate.usd == twenty.estimate.usd == 0.09
    assert one.estimate.rows is None, "the row count is unknowable before the call"


def test_seed_order_does_not_change_the_purchase(db: sqlite3.Connection) -> None:
    """Sorted into params, so typing them differently is not a second $0.09."""
    first = ideas.plan(db, ["b", "a"])
    second = ideas.plan(db, ["a", "b"])
    assert first.params == second.params


def test_an_identical_stored_pull_is_read_not_re_bought(db: sqlite3.Connection) -> None:
    _store(db, ["crm software"], [_item("best crm")])
    plan = ideas.plan(db, ["crm software"])

    assert plan.is_fresh
    assert plan.is_free


def test_a_pull_with_more_seeds_covers_a_narrower_request(db: sqlite3.Connection) -> None:
    """The suggestions are already stored, and the vendor does not tag them by seed."""
    _store(db, ["crm software", "project management"], [_item("best crm")])

    plan = ideas.plan(db, ["crm software"])

    assert plan.is_fresh
    assert plan.covered_by == ["crm software", "project management"]


def test_a_pull_with_fewer_seeds_does_not_cover_a_wider_request(db: sqlite3.Connection) -> None:
    """The other direction: a new seed was never asked about, so it costs."""
    _store(db, ["crm software"], [_item("best crm")])

    plan = ideas.plan(db, ["crm software", "project management"])

    assert not plan.is_fresh
    assert plan.estimate.usd == 0.09


def test_a_stored_pull_past_its_ttl_no_longer_covers(db: sqlite3.Connection) -> None:
    _store(db, ["crm software"], [_item("best crm")], age=timedelta(days=31))
    assert not ideas.plan(db, ["crm software"]).is_fresh


def test_force_ignores_a_covering_pull(db: sqlite3.Connection) -> None:
    _store(db, ["crm software"], [_item("best crm")])
    assert not ideas.plan(db, ["crm software"], force=True).is_fresh


def test_too_many_seeds_is_refused_before_pricing(db: sqlite3.Connection) -> None:
    with pytest.raises(InvalidRequestError):
        ideas.plan(db, [f"seed {i}" for i in range(21)])


def test_reading_returns_what_that_call_bought_best_first(db: sqlite3.Connection) -> None:
    _store(
        db,
        ["crm"],
        [_item("small crm", volume=100), _item("best crm", volume=9000)],
    )
    rows = ideas.read_rows(db, ["crm"])

    assert [row["keyword"] for row in rows] == ["best crm", "small crm"]


def test_reading_with_nothing_stored_is_empty_not_an_error(db: sqlite3.Connection) -> None:
    assert ideas.read_rows(db, ["crm"]) == []


def test_a_seasonal_keyword_names_its_peak_month(db: sqlite3.Connection) -> None:
    """A May spike against a quiet year is the pattern worth surfacing."""
    flat_year = [100] * 12
    flat_year[4] = 1500  # May
    _store(db, ["mothers day"], [_item("mothers day gifts", monthly=flat_year)])

    assert ideas.read_rows(db, ["mothers day"])[0]["peak"] == "May"


def test_a_level_keyword_names_no_peak(db: sqlite3.Connection) -> None:
    """Most keywords are flat. Naming a peak there invents a publishing schedule."""
    _store(db, ["crm"], [_item("crm software", monthly=[100, 110, 95, 105] * 3)])

    assert ideas.read_rows(db, ["crm"])[0]["peak"] == ""


def test_a_short_series_names_no_peak() -> None:
    """Fewer than six months cannot establish what normal looks like."""
    series = [{"year": 2026, "month": m, "volume": v} for m, v in enumerate([1, 1, 900], start=1)]
    assert ideas.peak_month(series) == ""


def test_an_all_zero_series_names_no_peak() -> None:
    series = [{"year": 2026, "month": m, "volume": 0} for m in range(1, 13)]
    assert ideas.peak_month(series) == ""


def test_enqueue_spends_nothing(db: sqlite3.Connection) -> None:
    """R5: the command queues, the runner spends."""
    before = db.execute("SELECT COUNT(*) AS n FROM raw_response").fetchone()["n"]
    job_id = ideas.enqueue(db, ideas.plan(db, ["crm software"]))

    assert job_id
    assert db.execute("SELECT COUNT(*) AS n FROM raw_response").fetchone()["n"] == before
    row = db.execute(
        "SELECT capability, estimated_cost FROM job WHERE id = ?", (job_id,)
    ).fetchone()
    assert row["capability"] == keywords_data.CAPABILITY
    assert row["estimated_cost"] == 0.09
