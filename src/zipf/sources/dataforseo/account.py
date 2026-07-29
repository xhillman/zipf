"""DataForSEO account state. Tier 0, free.

The vendor balance is the real spending limit. ``monthly_ceiling_usd`` is a
policy the user sets; the balance is what actually exists, and a ceiling above
it cannot stop anything.

This goes through ``fetch`` like every other source rather than being a side
call, so R1 holds and the response is cached, priced, and auditable on the same
terms as paid data.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import httpx

from zipf.pricing import PriceEstimate, free
from zipf.sources.dataforseo import client

CAPABILITY: Final = "dataforseo.user_data"


def build(params: Mapping[str, Any]) -> httpx.Request:
    return httpx.Request(
        "GET",
        f"{client.API_ROOT}/appendix/user_data",
        headers=client.auth_header(CAPABILITY),
    )


def price(params: Mapping[str, Any]) -> PriceEstimate:
    return free(tier=0)


def validate(body: bytes) -> None:
    client.validate(CAPABILITY, body)


def parse(body: bytes, params: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the account balance and lifetime spend."""
    results = client.task_results(CAPABILITY, body)
    if not results:
        return {"login": None, "balance": None, "total_spent": None}

    money = results[0].get("money") or {}
    return {
        "login": results[0].get("login"),
        "balance": _as_float(money.get("balance")),
        "total_spent": _as_float(money.get("total")),
    }


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None
