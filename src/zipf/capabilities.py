"""Capability registry.

The staleness policy (PRD §8) lives here as data rather than as constants
scattered through call sites. Adding a source means adding a row, not editing
``fetch``.

An unknown capability raises. There is deliberately no default TTL: a source
whose freshness nobody has thought about should not be fetchable.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx

from zipf.errors import CapabilityUnknownError
from zipf.pricing import PriceEstimate
from zipf.sources import autocomplete

type RequestBuilder = Callable[[Mapping[str, Any]], httpx.Request]
type Parser = Callable[[bytes, Mapping[str, Any]], Any]
type PriceFn = Callable[[Mapping[str, Any]], PriceEstimate]


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


REGISTRY: dict[str, Capability] = {
    autocomplete.CAPABILITY: Capability(
        name=autocomplete.CAPABILITY,
        tier=0,
        ttl=timedelta(days=90),
        build=autocomplete.build,
        parse=autocomplete.parse,
        price=autocomplete.price,
    ),
}


def get(name: str) -> Capability:
    """Look up a capability, or fail naming what is available."""
    try:
        return REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY)) or "none registered"
        raise CapabilityUnknownError(f"unknown capability {name!r}; known: {known}") from None
