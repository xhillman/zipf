"""Ceiling enforcement.

The ceiling exists because a runaway query is the highest-impact failure in the
product (PRD §14). It must fail the call, before the socket, every time — and it
must not fail the free paths that make the tool usable at all.
"""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
import respx

from zipf.budget import Budget
from zipf.clock import month_start_iso, now_iso
from zipf.errors import BudgetExceededError
from zipf.fetch import fetch
from zipf.sources.dataforseo import client, labs

VOLUME_BODY = json.dumps(
    {
        "status_code": 20000,
        "cost": 0.01236,
        "tasks": [
            {
                "status_code": 20000,
                "result": [
                    {
                        "items": [
                            {
                                "keyword": "best crm software",
                                "keyword_info": {
                                    "search_volume": 3600,
                                    "cpc": 41.92,
                                    "competition": 0.12,
                                },
                            }
                        ]
                    }
                ],
            }
        ],
    }
).encode()

VOLUME_URL = f"{client.API_ROOT}/dataforseo_labs/google/keyword_overview/live"


@pytest.fixture(autouse=True)
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAFORSEO_LOGIN", "login")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "password")


def _volume_route(router: respx.MockRouter) -> respx.Route:
    return router.post(VOLUME_URL).mock(return_value=httpx.Response(200, content=VOLUME_BODY))


def _record_spend(conn: sqlite3.Connection, amount: float, fetched_at: str | None = None) -> None:
    conn.execute(
        "INSERT INTO raw_response "
        "(capability, params_hash, params_json, body, cost_usd, fetched_at) "
        "VALUES ('labs.prior', ?, '{}', x'00', ?, ?)",
        (f"h{amount}{fetched_at}", amount, fetched_at or now_iso()),
    )


async def test_ceiling_blocks_before_the_socket(
    db: sqlite3.Connection, blocked_network: respx.MockRouter
) -> None:
    """No money is spent discovering that there is no money left."""
    route = _volume_route(blocked_network)
    _record_spend(db, 0.99)

    with pytest.raises(BudgetExceededError):
        await fetch(
            db,
            labs.SEARCH_VOLUME,
            {"keywords": ["best crm software"]},
            budget=Budget(ceiling_usd=1.00, threshold_usd=0.0),
        )

    assert route.call_count == 0


async def test_a_call_that_fits_exactly_is_allowed(
    db: sqlite3.Connection, blocked_network: respx.MockRouter
) -> None:
    """The ceiling is a limit, not a margin. Spending up to it is permitted."""
    _volume_route(blocked_network)
    estimate = labs.price_search_volume({"keywords": ["best crm software"]}).usd
    _record_spend(db, 1.00 - estimate)

    result = await fetch(
        db,
        labs.SEARCH_VOLUME,
        {"keywords": ["best crm software"]},
        budget=Budget(ceiling_usd=1.00, threshold_usd=0.0),
    )

    assert result.cached is False


async def test_last_months_spend_does_not_count_against_this_month(
    db: sqlite3.Connection, blocked_network: respx.MockRouter
) -> None:
    """The ceiling is monthly. A closed month must not gate a new one."""
    _volume_route(blocked_network)
    _record_spend(db, 99.0, fetched_at="2020-01-15T00:00:00Z")

    result = await fetch(
        db,
        labs.SEARCH_VOLUME,
        {"keywords": ["best crm software"]},
        budget=Budget(ceiling_usd=1.00, threshold_usd=0.0),
    )

    assert result.cached is False
    assert db.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM raw_response WHERE fetched_at >= ?",
        (month_start_iso(),),
    ).fetchone()["s"] == pytest.approx(0.01236)


async def test_the_recorded_cost_is_the_vendors_not_the_estimate(
    db: sqlite3.Connection, blocked_network: respx.MockRouter
) -> None:
    """Estimates gate; invoices count. The ledger must hold what was charged."""
    _volume_route(blocked_network)
    estimate = labs.price_search_volume({"keywords": ["best crm software"]}).usd

    result = await fetch(
        db,
        labs.SEARCH_VOLUME,
        {"keywords": ["best crm software"]},
        budget=Budget(ceiling_usd=1.00, threshold_usd=0.0),
    )

    assert result.cost_usd == pytest.approx(0.01236)
    assert estimate != result.cost_usd, "test no longer distinguishes estimate from actual"
    stored = db.execute("SELECT cost_usd FROM raw_response").fetchone()["cost_usd"]
    assert stored == pytest.approx(0.01236)


async def test_a_cache_hit_is_free_even_at_the_ceiling(
    db: sqlite3.Connection, blocked_network: respx.MockRouter
) -> None:
    """Cached browsing stays unlimited in a month that hit the ceiling (G5)."""
    _volume_route(blocked_network)
    params = {"keywords": ["best crm software"]}
    generous = Budget(ceiling_usd=100.0, threshold_usd=0.0)
    await fetch(db, labs.SEARCH_VOLUME, params, budget=generous)

    exhausted = Budget(ceiling_usd=0.001, threshold_usd=0.0)
    result = await fetch(db, labs.SEARCH_VOLUME, params, budget=exhausted)

    assert result.cached is True
    assert result.cost_usd == 0.0


async def test_a_vendor_error_is_not_cached(
    db: sqlite3.Connection, blocked_network: respx.MockRouter
) -> None:
    """A task-level failure arrives as HTTP 200 and must never enter the cache.

    Caching it would serve the error for the full 30-day TTL.
    """
    error_body = json.dumps(
        {
            "status_code": 20000,
            "cost": 0,
            "tasks": [{"status_code": 40402, "status_message": "Invalid Path."}],
        }
    ).encode()
    blocked_network.post(VOLUME_URL).mock(return_value=httpx.Response(200, content=error_body))

    from zipf.errors import VendorError

    with pytest.raises(VendorError, match="40402"):
        await fetch(
            db,
            labs.SEARCH_VOLUME,
            {"keywords": ["best crm software"]},
            budget=Budget(ceiling_usd=1.0, threshold_usd=0.0),
        )

    assert db.execute("SELECT COUNT(*) AS n FROM raw_response").fetchone()["n"] == 0
