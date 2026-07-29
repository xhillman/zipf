"""Capability registry.

The staleness policy (PRD §8) lives here as data rather than as constants
scattered through call sites. Adding a source means adding a row, not editing
``fetch``.

An unknown capability raises. There is deliberately no default TTL: a source
whose freshness nobody has thought about should not be fetchable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx

from zipf.errors import CapabilityUnknownError
from zipf.pricing import PriceEstimate
from zipf.sources import autocomplete, google_oauth, gsc

type RequestBuilder = Callable[[Mapping[str, Any]], httpx.Request]
type Parser = Callable[[bytes, Mapping[str, Any]], Any]
type PriceFn = Callable[[Mapping[str, Any]], PriceEstimate]
type AuthProvider = Callable[[], Awaitable[Mapping[str, str]]]


@dataclass(frozen=True)
class Capability:
    """One fetchable thing, with the freshness and price rules that govern it."""

    name: str
    tier: int
    ttl: timedelta
    build: RequestBuilder
    parse: Parser
    price: PriceFn
    #: Environment variables that must be set before this capability can run.
    requires: tuple[str, ...] = ()
    #: Returns headers to merge into the request, e.g. a bearer token.
    #:
    #: Auth is deliberately separate from ``build`` because credentials must not
    #: reach ``params_hash``. A token folded into the params would change on
    #: every refresh, and every refresh would then invalidate the entire cache
    #: for this capability — turning a free re-read into a re-purchase.
    auth: AuthProvider | None = None


REGISTRY: dict[str, Capability] = {
    autocomplete.CAPABILITY: Capability(
        name=autocomplete.CAPABILITY,
        tier=0,
        ttl=timedelta(days=90),
        build=autocomplete.build,
        parse=autocomplete.parse,
        price=autocomplete.price,
    ),
    gsc.CAPABILITY: Capability(
        name=gsc.CAPABILITY,
        tier=0,
        ttl=timedelta(days=1),  # free, so refreshed greedily
        build=gsc.build,
        parse=gsc.parse,
        price=gsc.price,
        requires=("GSC_CLIENT_ID", "GSC_CLIENT_SECRET"),
        auth=google_oauth.auth_headers,
    ),
    gsc.SITES_CAPABILITY: Capability(
        name=gsc.SITES_CAPABILITY,
        tier=0,
        ttl=timedelta(days=1),
        build=gsc.build_sites,
        parse=gsc.parse_sites,
        price=gsc.price_sites,
        requires=("GSC_CLIENT_ID", "GSC_CLIENT_SECRET"),
        auth=google_oauth.auth_headers,
    ),
}


def get(name: str) -> Capability:
    """Look up a capability, or fail naming what is available."""
    try:
        return REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY)) or "none registered"
        raise CapabilityUnknownError(f"unknown capability {name!r}; known: {known}") from None
