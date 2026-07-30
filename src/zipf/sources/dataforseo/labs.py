"""DataForSEO Labs. Tier 1.

Pricing is dominated by a per-call base rather than per-row cost, so these
capabilities take many items per call (spec D13). Cache-aware batching lives in
the service layer; this module only builds and parses.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import httpx

from zipf.errors import InvalidRequestError
from zipf.pricing import PriceEstimate
from zipf.sources.dataforseo import client

SEARCH_VOLUME: Final = "labs.search_volume"
RANKED_KEYWORDS: Final = "labs.ranked_keywords"
DOMAIN_INTERSECTION: Final = "labs.domain_intersection"

# Measured against the live API on 2026-07-29 with 1 and 5 keywords:
#   1 keyword  -> $0.01212
#   5 keywords -> $0.01260
# giving a $0.012 base and $0.00012 per row. Estimated and actual cost are both
# recorded on every job, so drift from these constants is measurable rather than
# assumed.
BASE_USD: Final = 0.012
PER_ROW_USD: Final = 0.00012

#: Vendor caps a single keyword_overview call at 700 keywords.
MAX_KEYWORDS_PER_CALL: Final = 700

#: Conservative default depth for domain-wide pulls. A large domain can return
#: tens of thousands of rows, and the caller pays for the depth it asks for.
DEFAULT_LIMIT: Final = 100
MAX_LIMIT: Final = 1000


def _estimate(rows: int) -> PriceEstimate:
    return PriceEstimate(
        usd=round(BASE_USD + PER_ROW_USD * rows, 5),
        tier=1,
        queue="none",
        rows=rows,
    )


# --------------------------------------------------------------------------
# labs.search_volume — volume, cpc and competition for a batch of keywords
# --------------------------------------------------------------------------


def build_search_volume(params: Mapping[str, Any]) -> httpx.Request:
    keywords = list(params["keywords"])
    if not keywords:
        raise InvalidRequestError("No keywords were given to price.")
    if len(keywords) > MAX_KEYWORDS_PER_CALL:
        raise InvalidRequestError(
            f"{len(keywords):,} keywords is more than one call can carry "
            f"(the limit is {MAX_KEYWORDS_PER_CALL:,}).",
            fix="Split them across separate `zipf vol` runs.",
        )

    return client.build_task_request(
        SEARCH_VOLUME,
        "/dataforseo_labs/google/keyword_overview/live",
        {
            "keywords": keywords,
            "location_code": int(params.get("location_code", client.DEFAULT_LOCATION_CODE)),
            "language_code": str(params.get("language_code", client.DEFAULT_LANGUAGE_CODE)),
        },
    )


def price_search_volume(params: Mapping[str, Any]) -> PriceEstimate:
    return _estimate(len(list(params.get("keywords", []))))


def parse_search_volume(body: bytes, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten to one dict per keyword.

    Volume lives under ``keyword_info``; the vendor returns a null
    ``search_volume`` for terms it has no data for, which is a real answer and is
    preserved rather than coerced to zero.
    """
    rows: list[dict[str, Any]] = []
    for item in client.items_of(client.task_results(SEARCH_VOLUME, body)):
        info = item.get("keyword_info") or {}
        rows.append(
            {
                "keyword": item.get("keyword"),
                "volume": info.get("search_volume"),
                "cpc": info.get("cpc"),
                "competition": info.get("competition"),
            }
        )
    return rows


# --------------------------------------------------------------------------
# labs.ranked_keywords — what a domain already ranks for
# --------------------------------------------------------------------------


def build_ranked_keywords(params: Mapping[str, Any]) -> httpx.Request:
    return client.build_task_request(
        RANKED_KEYWORDS,
        "/dataforseo_labs/google/ranked_keywords/live",
        {
            "target": params["domain"],
            "location_code": int(params.get("location_code", client.DEFAULT_LOCATION_CODE)),
            "language_code": str(params.get("language_code", client.DEFAULT_LANGUAGE_CODE)),
            "limit": min(int(params.get("limit", DEFAULT_LIMIT)), MAX_LIMIT),
        },
    )


def price_ranked_keywords(params: Mapping[str, Any]) -> PriceEstimate:
    return _estimate(min(int(params.get("limit", DEFAULT_LIMIT)), MAX_LIMIT))


def parse_ranked_keywords(body: bytes, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in client.items_of(client.task_results(RANKED_KEYWORDS, body)):
        keyword_data = item.get("keyword_data") or {}
        serp_element = item.get("ranked_serp_element") or {}
        element = serp_element.get("serp_item") or {}
        info = keyword_data.get("keyword_info") or {}
        rows.append(
            {
                "keyword": keyword_data.get("keyword"),
                "position": element.get("rank_absolute"),
                "url": element.get("url"),
                "volume": info.get("search_volume"),
                "cpc": info.get("cpc"),
                "competition": info.get("competition"),
            }
        )
    return rows


# --------------------------------------------------------------------------
# labs.domain_intersection — keywords two domains both rank for
# --------------------------------------------------------------------------


def build_domain_intersection(params: Mapping[str, Any]) -> httpx.Request:
    return client.build_task_request(
        DOMAIN_INTERSECTION,
        "/dataforseo_labs/google/domain_intersection/live",
        {
            "target1": params["target1"],
            "target2": params["target2"],
            "location_code": int(params.get("location_code", client.DEFAULT_LOCATION_CODE)),
            "language_code": str(params.get("language_code", client.DEFAULT_LANGUAGE_CODE)),
            "limit": min(int(params.get("limit", DEFAULT_LIMIT)), MAX_LIMIT),
            "intersections": bool(params.get("intersections", True)),
        },
    )


def price_domain_intersection(params: Mapping[str, Any]) -> PriceEstimate:
    return _estimate(min(int(params.get("limit", DEFAULT_LIMIT)), MAX_LIMIT))


def parse_domain_intersection(body: bytes, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One row per keyword, with each domain's position where it has one."""
    rows: list[dict[str, Any]] = []
    for item in client.items_of(client.task_results(DOMAIN_INTERSECTION, body)):
        keyword_data = item.get("keyword_data") or {}
        info = keyword_data.get("keyword_info") or {}
        first = (item.get("first_domain_serp_element") or {}) or {}
        second = (item.get("second_domain_serp_element") or {}) or {}
        rows.append(
            {
                "keyword": keyword_data.get("keyword"),
                "volume": info.get("search_volume"),
                "cpc": info.get("cpc"),
                "competition": info.get("competition"),
                "target1_position": first.get("rank_absolute"),
                "target1_url": first.get("url"),
                "target2_position": second.get("rank_absolute"),
                "target2_url": second.get("url"),
            }
        )
    return rows
