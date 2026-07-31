"""Per-capability request pacing.

Driven with a fake clock and a fake sleep. A limiter tested against real time
would either take minutes to run or assert nothing, and the property under test
is arithmetic about a window rather than anything about wall clocks.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any

import httpx
import pytest
import respx

from zipf import capabilities
from zipf.budget import Budget
from zipf.fetch import fetch
from zipf.ratelimit import (
    MAX_WAIT_SECONDS,
    WINDOW_SECONDS,
    RateLimiter,
    RateLimitTimeoutError,
)
from zipf.sources.autocomplete import CAPABILITY, ENDPOINT


class FakeClock:
    """A monotonic clock that only moves when something sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr("zipf.ratelimit.time.monotonic", fake.time)
    monkeypatch.setattr("zipf.ratelimit.asyncio.sleep", fake.sleep)
    return fake


async def test_a_capability_with_no_limit_is_never_paced(clock: FakeClock) -> None:
    """Guessing a limit for a vendor that never published one would slow calls."""
    limiter = RateLimiter()
    for _ in range(100):
        assert await limiter.acquire("unpaced", None) == 0.0
    assert clock.slept == []


async def test_calls_below_the_limit_do_not_wait(clock: FakeClock) -> None:
    limiter = RateLimiter()
    for _ in range(12):
        assert await limiter.acquire("paced", 12) == 0.0
    assert clock.slept == []


async def test_the_call_past_the_limit_waits_for_the_window_to_slide(
    clock: FakeClock,
) -> None:
    """The thirteenth call waits until the first falls out of the minute."""
    limiter = RateLimiter()
    for _ in range(12):
        await limiter.acquire("paced", 12)

    waited = await limiter.acquire("paced", 12)

    assert waited == pytest.approx(WINDOW_SECONDS)
    assert clock.slept == [pytest.approx(WINDOW_SECONDS)]


async def test_the_window_slides_rather_than_resetting(clock: FakeClock) -> None:
    """A fixed window would let twice the limit through across a boundary.

    Eleven calls, then a wait past the minute, then eleven more must not need to
    wait: the first eleven have expired by then.
    """
    limiter = RateLimiter()
    for _ in range(11):
        await limiter.acquire("paced", 12)

    clock.now += WINDOW_SECONDS + 1
    for _ in range(11):
        assert await limiter.acquire("paced", 12) == 0.0

    assert clock.slept == []


async def test_limits_are_tracked_separately_per_capability(clock: FakeClock) -> None:
    """One vendor's cap must not throttle another's."""
    limiter = RateLimiter()
    for _ in range(12):
        await limiter.acquire("first", 12)

    assert await limiter.acquire("second", 12) == 0.0
    assert clock.slept == []


async def test_waiting_beyond_the_bound_raises_rather_than_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A limiter that blocks forever is indistinguishable from a hang.

    Time is frozen here — sleeping records the delay but does not advance the
    clock — so the window never slides and the cumulative bound is what stops
    the loop. Without that bound this call would never return.
    """
    frozen = FakeClock()
    monkeypatch.setattr("zipf.ratelimit.time.monotonic", frozen.time)

    async def sleep_without_advancing(seconds: float) -> None:
        frozen.slept.append(seconds)

    monkeypatch.setattr("zipf.ratelimit.asyncio.sleep", sleep_without_advancing)

    limiter = RateLimiter()
    await limiter.acquire("paced", 1)

    with pytest.raises(RateLimitTimeoutError) as caught:
        await limiter.acquire("paced", 1)

    assert f"{MAX_WAIT_SECONDS:.0f}s" in str(caught.value)
    assert sum(frozen.slept) <= MAX_WAIT_SECONDS


async def test_a_cache_hit_is_never_paced(
    db: sqlite3.Connection,
    budget: Budget,
    blocked_network: respx.MockRouter,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Browsing stored data stays unlimited, which is the whole promise.

    Autocomplete publishes no limit, so a strict one is imposed for this test:
    a single request per minute. The second fetch would have to wait a full
    window if pacing ran before the cache check — it does not, because pacing
    lives in the send path and a cache hit returns before reaching it.
    """
    paced = replace(capabilities.get(CAPABILITY), rate_limit_per_minute=1)
    monkeypatch.setitem(capabilities.REGISTRY, CAPABILITY, paced)

    body = b'["crm", ["best crm"], [], [], {}]'
    blocked_network.get(ENDPOINT).mock(return_value=httpx.Response(200, content=body))

    params: dict[str, Any] = {"seed": "crm"}
    first = await fetch(db, CAPABILITY, params, budget=budget)
    second = await fetch(db, CAPABILITY, params, budget=budget)

    assert first.cached is False
    assert second.cached is True
    assert clock.slept == [], "a cached read waited on a rate limit"
