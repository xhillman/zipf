"""Budget reporting and the confirmation gate.

The gate is the last thing standing between a typo and a purchase, so its
behaviour at the boundaries is tested rather than assumed.
"""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
import respx

from zipf.budget import Budget
from zipf.pricing import PriceEstimate, free
from zipf.services import budget as budget_service
from zipf.sources.dataforseo import account, client

USER_DATA_BODY = json.dumps(
    {
        "status_code": 20000,
        "cost": 0,
        "tasks": [
            {
                "status_code": 20000,
                "result": [{"login": "me@example.com", "money": {"balance": 0.96, "total": 1.0}}],
            }
        ],
    }
).encode()

CONFIRM_EVERYTHING = Budget(ceiling_usd=20.0, threshold_usd=0.0)


def _account_route(router: respx.MockRouter) -> respx.Route:
    return router.get(f"{client.API_ROOT}/appendix/user_data").mock(
        return_value=httpx.Response(200, content=USER_DATA_BODY)
    )


def _credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAFORSEO_LOGIN", "login")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "password")


def test_threshold_zero_confirms_the_cheapest_possible_spend() -> None:
    """Any tier-1 call clears the vendor's base cost, so all of them must ask."""
    cheapest = PriceEstimate(usd=0.01212, tier=1, rows=1)
    assert CONFIRM_EVERYTHING.needs_confirmation(cheapest) is True


def test_threshold_zero_still_never_confirms_free_work() -> None:
    """Tier 0 and cache hits must stay frictionless even when confirming all spend."""
    assert CONFIRM_EVERYTHING.needs_confirmation(free(tier=0)) is False


async def test_status_reports_the_live_balance(
    db: sqlite3.Connection, blocked_network: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)
    route = _account_route(blocked_network)

    state = await budget_service.status(db, CONFIRM_EVERYTHING)

    assert route.call_count == 1
    assert state.balance == 0.96
    assert state.spent == 0.0


async def test_a_balance_lookup_failure_does_not_break_the_readout(
    db: sqlite3.Connection, blocked_network: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spend is ground truth and must still be reported when the vendor is down."""
    _credentials(monkeypatch)
    blocked_network.get(f"{client.API_ROOT}/appendix/user_data").mock(
        return_value=httpx.Response(500, content=b"down")
    )

    state = await budget_service.status(db, CONFIRM_EVERYTHING)

    assert state.spent == 0.0
    assert state.balance is None
    assert state.balance_error is not None


async def test_a_failed_lookup_falls_back_to_the_last_known_balance(
    db: sqlite3.Connection, blocked_network: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credentials(monkeypatch)
    _account_route(blocked_network)
    await budget_service.status(db, CONFIRM_EVERYTHING)

    blocked_network.get(f"{client.API_ROOT}/appendix/user_data").mock(
        return_value=httpx.Response(500, content=b"down")
    )
    state = await budget_service.status(db, CONFIRM_EVERYTHING)

    assert state.balance == 0.96, "a transient outage discarded a known balance"
    assert state.balance_error is not None


def test_cached_balance_makes_no_network_call(
    db: sqlite3.Connection, blocked_network: respx.MockRouter
) -> None:
    """The confirmation gate reads this, so it must work offline and instantly."""
    db.execute(
        "INSERT INTO raw_response "
        "(capability, params_hash, params_json, body, cost_usd, fetched_at) "
        "VALUES (?, 'h', '{}', ?, 0.0, '2026-07-29T00:00:00Z')",
        (account.CAPABILITY, USER_DATA_BODY),
    )
    route = _account_route(blocked_network)

    balance, age = budget_service.cached_balance(db)

    assert route.call_count == 0
    assert balance == 0.96
    assert age is not None


def test_cached_balance_is_absent_before_any_lookup(db: sqlite3.Connection) -> None:
    assert budget_service.cached_balance(db) == (None, None)


def test_a_ceiling_above_the_balance_is_reported_as_decorative() -> None:
    state = budget_service.BudgetStatus(
        spent=0.0, ceiling=20.0, remaining=20.0, threshold=0.0, balance=0.96, balance_age=None
    )
    assert state.ceiling_exceeds_balance is True
    assert state.effective_limit == 0.96


def test_the_effective_limit_is_the_ceiling_when_well_funded() -> None:
    state = budget_service.BudgetStatus(
        spent=2.0, ceiling=20.0, remaining=18.0, threshold=0.0, balance=500.0, balance_age=None
    )
    assert state.ceiling_exceeds_balance is False
    assert state.effective_limit == 18.0
