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
from zipf.sources.dataforseo import account as dfs_account
from zipf.sources.dataforseo import client as dfs_client
from zipf.sources.dataforseo import keywords_data, labs

type RequestBuilder = Callable[[Mapping[str, Any]], httpx.Request]
type Parser = Callable[[bytes, Mapping[str, Any]], Any]
type PriceFn = Callable[[Mapping[str, Any]], PriceEstimate]
type AuthProvider = Callable[[], Awaitable[Mapping[str, str]]]
type Validator = Callable[[bytes], None]
type CostReader = Callable[[bytes], float | None]


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
    #: Rejects a vendor-level error before the response is cached.
    #:
    #: Some vendors report failure with HTTP 200 and an error code in the body.
    #: Without this hook such a response would be persisted and then served from
    #: cache for the whole TTL, turning one bad call into a stale wrong answer.
    validate: Validator | None = None
    #: Reads the amount actually charged out of the response, when the vendor
    #: reports it. Falls back to the estimate when absent.
    cost_from_response: CostReader | None = None
    #: Requests per minute this vendor accepts for this capability, when it has
    #: published one. ``None`` means unpaced: a limit nobody stated is not a
    #: limit to invent, and guessing one would slow calls for no reason.
    rate_limit_per_minute: int | None = None
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
    dfs_account.CAPABILITY: Capability(
        name=dfs_account.CAPABILITY,
        tier=0,
        ttl=timedelta(minutes=15),
        build=dfs_account.build,
        parse=dfs_account.parse,
        price=dfs_account.price,
        requires=("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"),
        validate=dfs_account.validate,
    ),
    keywords_data.CAPABILITY: Capability(
        name=keywords_data.CAPABILITY,
        tier=1,
        # Google Ads refreshes volume monthly, so this matches the Labs volume
        # TTL. Both ultimately read the same upstream.
        ttl=timedelta(days=30),
        build=keywords_data.build,
        parse=keywords_data.parse,
        price=keywords_data.price,
        requires=("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"),
        validate=lambda body: dfs_client.validate(keywords_data.CAPABILITY, body),
        cost_from_response=dfs_client.actual_cost,
        rate_limit_per_minute=keywords_data.REQUESTS_PER_MINUTE,
    ),
    labs.SEARCH_VOLUME: Capability(
        name=labs.SEARCH_VOLUME,
        tier=1,
        ttl=timedelta(days=30),  # the upstream updates monthly anyway
        build=labs.build_search_volume,
        parse=labs.parse_search_volume,
        price=labs.price_search_volume,
        requires=("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"),
        validate=lambda body: dfs_client.validate(labs.SEARCH_VOLUME, body),
        cost_from_response=dfs_client.actual_cost,
    ),
    labs.BULK_KEYWORD_DIFFICULTY: Capability(
        name=labs.BULK_KEYWORD_DIFFICULTY,
        tier=1,
        # Difficulty moves with link profiles, which change over months rather
        # than days. Matched to the volume TTL: both describe a competitive
        # landscape, and refreshing one without the other invites comparing
        # figures measured weeks apart.
        ttl=timedelta(days=30),
        build=labs.build_bulk_keyword_difficulty,
        parse=labs.parse_bulk_keyword_difficulty,
        price=labs.price_bulk_keyword_difficulty,
        requires=("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"),
        validate=lambda body: dfs_client.validate(labs.BULK_KEYWORD_DIFFICULTY, body),
        cost_from_response=dfs_client.actual_cost,
    ),
    labs.SEARCH_INTENT: Capability(
        name=labs.SEARCH_INTENT,
        tier=1,
        # Intent is a property of the phrase, not of a market or a moment. What
        # someone means by "how to fix a leaky tap" does not drift, so this is
        # the longest TTL on any paid capability.
        ttl=timedelta(days=90),
        build=labs.build_search_intent,
        parse=labs.parse_search_intent,
        price=labs.price_search_intent,
        requires=("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"),
        validate=lambda body: dfs_client.validate(labs.SEARCH_INTENT, body),
        cost_from_response=dfs_client.actual_cost,
    ),
    labs.RANKED_KEYWORDS: Capability(
        name=labs.RANKED_KEYWORDS,
        tier=1,
        ttl=timedelta(days=7),
        build=labs.build_ranked_keywords,
        parse=labs.parse_ranked_keywords,
        price=labs.price_ranked_keywords,
        requires=("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"),
        validate=lambda body: dfs_client.validate(labs.RANKED_KEYWORDS, body),
        cost_from_response=dfs_client.actual_cost,
    ),
    labs.DOMAIN_INTERSECTION: Capability(
        name=labs.DOMAIN_INTERSECTION,
        tier=1,
        ttl=timedelta(days=7),
        build=labs.build_domain_intersection,
        parse=labs.parse_domain_intersection,
        price=labs.price_domain_intersection,
        requires=("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"),
        validate=lambda body: dfs_client.validate(labs.DOMAIN_INTERSECTION, body),
        cost_from_response=dfs_client.actual_cost,
    ),
}


def get(name: str) -> Capability:
    """Look up a capability, or fail naming what is available."""
    try:
        return REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY)) or "none registered"
        raise CapabilityUnknownError(name, known) from None
