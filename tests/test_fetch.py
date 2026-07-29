"""Tests for the metered door.

The first test is the M0 exit criterion and the whole thesis of the project: the
second call for the same thing must not reach the network.
"""

from __future__ import annotations

import json
import re
import sqlite3

import httpx
import pytest
import respx

from zipf.budget import Budget
from zipf.clock import now_iso, to_iso
from zipf.errors import BudgetExceededError, CapabilityUnknownError
from zipf.fetch import fetch, hash_params, normalise_params
from zipf.pricing import PriceEstimate
from zipf.sources.autocomplete import CAPABILITY, ENDPOINT

SUGGEST_BODY = json.dumps(
    ["crm software", ["best crm software", "crm software for small business"], [], [], {}]
).encode()


def _route(router: respx.MockRouter) -> respx.Route:
    return router.get(ENDPOINT).mock(
        return_value=httpx.Response(200, content=SUGGEST_BODY),
    )


async def test_second_fetch_is_free_and_makes_no_request(
    db: sqlite3.Connection, budget: Budget, blocked_network: respx.MockRouter
) -> None:
    """M0 exit criterion: fetch once, serve forever."""
    route = _route(blocked_network)
    params = {"seed": "crm software"}

    first = await fetch(db, CAPABILITY, params, budget=budget)
    second = await fetch(db, CAPABILITY, params, budget=budget)

    assert route.call_count == 1, "the second fetch reached the network"
    assert first.cached is False
    assert second.cached is True
    assert second.cost_usd == 0.0
    assert second.body == first.body
    assert second.raw_id == first.raw_id


async def test_force_bypasses_a_fresh_cache_entry(
    db: sqlite3.Connection, budget: Budget, blocked_network: respx.MockRouter
) -> None:
    route = _route(blocked_network)
    params = {"seed": "crm software"}

    await fetch(db, CAPABILITY, params, budget=budget)
    refetched = await fetch(db, CAPABILITY, params, budget=budget, force=True)

    assert route.call_count == 2
    assert refetched.cached is False
    # History is never pruned: the forced refetch appends rather than replaces.
    rows = db.execute("SELECT COUNT(*) AS n FROM raw_response").fetchone()["n"]
    assert rows == 2


async def test_entry_past_ttl_is_refetched(
    db: sqlite3.Connection, budget: Budget, blocked_network: respx.MockRouter
) -> None:
    """A stale row is ignored. Inserted directly, because R2 forbids UPDATE."""
    from datetime import timedelta

    from zipf.clock import now

    params_hash = hash_params(normalise_params({"seed": "crm software"}))
    stale = to_iso(now() - timedelta(days=91))  # autocomplete TTL is 90 days
    db.execute(
        "INSERT INTO raw_response "
        "(capability, params_hash, params_json, body, cost_usd, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (CAPABILITY, params_hash, "{}", SUGGEST_BODY, 0.0, stale),
    )

    route = _route(blocked_network)
    result = await fetch(db, CAPABILITY, {"seed": "crm software"}, budget=budget)

    assert route.call_count == 1
    assert result.cached is False


async def test_dry_run_prices_without_fetching(
    db: sqlite3.Connection, budget: Budget, blocked_network: respx.MockRouter
) -> None:
    route = _route(blocked_network)

    result = await fetch(db, CAPABILITY, {"seed": "crm"}, budget=budget, dry_run=True)

    assert route.call_count == 0
    assert result.raw_id is None
    assert result.body is None
    assert result.plan.tier == 0
    assert result.plan.is_free


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ({"seed": "CRM Software"}, {"seed": "  crm software  "}),
        ({"seed": "crm", "lang": "EN"}, {"lang": "en", "seed": "crm"}),
        ({"seed": "crm", "country": None}, {"seed": "crm"}),
    ],
)
def test_params_that_mean_the_same_thing_hash_the_same(
    left: dict[str, object], right: dict[str, object]
) -> None:
    """Casing, whitespace, key order, and absent values must not buy data twice."""
    assert hash_params(normalise_params(left)) == hash_params(normalise_params(right))


def test_params_that_differ_hash_differently() -> None:
    a = hash_params(normalise_params({"seed": "crm", "country": "us"}))
    b = hash_params(normalise_params({"seed": "crm", "country": "gb"}))
    assert a != b


async def test_ceiling_fails_the_call_rather_than_warning(
    db: sqlite3.Connection, blocked_network: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paid capability over the ceiling raises. It does not downgrade."""
    from zipf import capabilities

    paid = capabilities.get(CAPABILITY)
    monkeypatch.setitem(
        capabilities.REGISTRY,
        CAPABILITY,
        capabilities.Capability(
            name=paid.name,
            tier=1,
            ttl=paid.ttl,
            build=paid.build,
            parse=paid.parse,
            price=lambda _params: PriceEstimate(usd=5.00, tier=1, rows=1000),
        ),
    )
    route = _route(blocked_network)
    tight = Budget(ceiling_usd=1.00, threshold_usd=0.25)

    with pytest.raises(BudgetExceededError) as caught:
        await fetch(db, CAPABILITY, {"seed": "crm"}, budget=tight)

    assert route.call_count == 0, "money was spent after the ceiling was breached"
    assert caught.value.ceiling == 1.00


async def test_free_calls_are_never_gated_by_the_ceiling(
    db: sqlite3.Connection, blocked_network: respx.MockRouter
) -> None:
    """Tier 0 stays usable in a month where the ceiling is already reached."""
    db.execute(
        "INSERT INTO raw_response "
        "(capability, params_hash, params_json, body, cost_usd, fetched_at) "
        "VALUES ('labs.x', 'h', '{}', x'00', 99.0, ?)",
        (now_iso(),),
    )
    _route(blocked_network)
    exhausted = Budget(ceiling_usd=1.00, threshold_usd=0.25)

    result = await fetch(db, CAPABILITY, {"seed": "crm"}, budget=exhausted)

    assert result.cached is False


async def test_unknown_capability_names_what_is_available(
    db: sqlite3.Connection, budget: Budget
) -> None:
    with pytest.raises(CapabilityUnknownError, match=re.escape("autocomplete.suggest")):
        await fetch(db, "labs.does_not_exist", {}, budget=budget)


async def test_body_is_stored_untouched(
    db: sqlite3.Connection, budget: Budget, blocked_network: respx.MockRouter
) -> None:
    """raw_response holds the vendor's bytes, not a parsed shape."""
    _route(blocked_network)
    result = await fetch(db, CAPABILITY, {"seed": "crm"}, budget=budget)

    stored = db.execute("SELECT body FROM raw_response WHERE id = ?", (result.raw_id,)).fetchone()
    assert stored["body"] == SUGGEST_BODY
    assert result.parsed() == ["best crm software", "crm software for small business"]
