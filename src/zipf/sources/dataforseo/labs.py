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
BULK_KEYWORD_DIFFICULTY: Final = "labs.bulk_keyword_difficulty"
SEARCH_INTENT: Final = "labs.search_intent"

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

#: The bulk attribute endpoints take more per call than keyword_overview does.
MAX_BULK_KEYWORDS: Final = 1000

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


def parse_keyword_data(block: Mapping[str, Any]) -> dict[str, Any]:
    """Every keyword fact carried by the shared Labs ``keyword_data`` block.

    Three endpoints return this identical structure: ``keyword_overview`` returns
    it as the item itself, while ``ranked_keywords`` and ``domain_intersection``
    nest it under ``keyword_data`` beside their positional fields. Reading it in
    one place is what makes difficulty, intent and the monthly series arrive with
    every paid call rather than only from the endpoints that sell them alone.

    ``intent_probability`` is deliberately absent. This block reports
    ``main_intent`` as a bare label; only the dedicated ``search_intent``
    endpoint returns a confidence alongside it, so claiming one here would invent
    a number the vendor did not send.
    """
    info = block.get("keyword_info") or {}
    properties = block.get("keyword_properties") or {}
    intent = block.get("search_intent_info") or {}
    return {
        "keyword": block.get("keyword"),
        "volume": info.get("search_volume"),
        "cpc": info.get("cpc"),
        "competition": info.get("competition"),
        "difficulty": properties.get("keyword_difficulty"),
        "intent": intent.get("main_intent"),
        "monthly": client.monthly_series(info),
    }


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

    The item *is* the shared keyword-data block for this endpoint, so everything
    the response carries is read. The vendor returns a null ``search_volume`` for
    terms it has no data for, which is a real answer and is preserved rather than
    coerced to zero.
    """
    return [
        parse_keyword_data(item)
        for item in client.items_of(client.task_results(SEARCH_VOLUME, body))
    ]


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
    """One row per ranked keyword: where the domain sits, and what the term is worth."""
    rows: list[dict[str, Any]] = []
    for item in client.items_of(client.task_results(RANKED_KEYWORDS, body)):
        serp_element = item.get("ranked_serp_element") or {}
        element = serp_element.get("serp_item") or {}
        rows.append(
            {
                **parse_keyword_data(item.get("keyword_data") or {}),
                "position": element.get("rank_absolute"),
                "url": element.get("url"),
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
        first = item.get("first_domain_serp_element") or {}
        second = item.get("second_domain_serp_element") or {}
        rows.append(
            {
                **parse_keyword_data(item.get("keyword_data") or {}),
                "target1_position": first.get("rank_absolute"),
                "target1_url": first.get("url"),
                "target2_position": second.get("rank_absolute"),
                "target2_url": second.get("url"),
            }
        )
    return rows


# --------------------------------------------------------------------------
# labs.bulk_keyword_difficulty — can I realistically rank for this
# --------------------------------------------------------------------------
#
# Volume, cpc and competition all describe an *advertising* market. Difficulty
# is the only figure here that answers the organic question, which is the one a
# gap list exists to raise.


def _bulk_keywords(params: Mapping[str, Any], command: str) -> list[str]:
    """Validate a bulk keyword list against the vendor's per-call cap."""
    keywords = list(params["keywords"])
    if not keywords:
        raise InvalidRequestError("No keywords were given.")
    if len(keywords) > MAX_BULK_KEYWORDS:
        raise InvalidRequestError(
            f"{len(keywords):,} keywords is more than one call can carry "
            f"(the limit is {MAX_BULK_KEYWORDS:,}).",
            fix=f"Split them across separate `{command}` runs.",
        )
    return keywords


def build_bulk_keyword_difficulty(params: Mapping[str, Any]) -> httpx.Request:
    return client.build_task_request(
        BULK_KEYWORD_DIFFICULTY,
        "/dataforseo_labs/google/bulk_keyword_difficulty/live",
        {
            "keywords": _bulk_keywords(params, "zipf enrich"),
            "location_code": int(params.get("location_code", client.DEFAULT_LOCATION_CODE)),
            "language_code": str(params.get("language_code", client.DEFAULT_LANGUAGE_CODE)),
        },
    )


def price_bulk_keyword_difficulty(params: Mapping[str, Any]) -> PriceEstimate:
    return _estimate(len(list(params.get("keywords", []))))


def parse_bulk_keyword_difficulty(body: bytes, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One record per keyword, carrying a 0-100 difficulty."""
    rows: list[dict[str, Any]] = []
    for item in client.items_of(client.task_results(BULK_KEYWORD_DIFFICULTY, body)):
        keyword = item.get("keyword")
        if isinstance(keyword, str) and keyword:
            rows.append(
                {
                    "keyword": keyword.strip().lower(),
                    "difficulty": item.get("keyword_difficulty"),
                }
            )
    return rows


# --------------------------------------------------------------------------
# labs.search_intent — what the searcher wanted
# --------------------------------------------------------------------------


def build_search_intent(params: Mapping[str, Any]) -> httpx.Request:
    """Note the absent location.

    This is the one Labs endpoint that takes no location parameter: intent is a
    property of the phrase rather than of a market. Sending one anyway would put
    a field in the request that the vendor does not document accepting.
    """
    return client.build_task_request(
        SEARCH_INTENT,
        "/dataforseo_labs/google/search_intent/live",
        {
            "keywords": _bulk_keywords(params, "zipf enrich"),
            "language_code": str(params.get("language_code", client.DEFAULT_LANGUAGE_CODE)),
        },
    )


def price_search_intent(params: Mapping[str, Any]) -> PriceEstimate:
    return _estimate(len(list(params.get("keywords", []))))


def parse_search_intent(body: bytes, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One record per keyword: the winning intent and how sure the vendor is.

    The probability is kept because the label alone hides the difference between
    a keyword that is 97% navigational and one that is 51% — the second is a
    coin toss dressed as a classification.
    """
    rows: list[dict[str, Any]] = []
    for item in client.items_of(client.task_results(SEARCH_INTENT, body)):
        keyword = item.get("keyword")
        if not isinstance(keyword, str) or not keyword:
            continue
        intent = item.get("keyword_intent") or {}
        rows.append(
            {
                "keyword": keyword.strip().lower(),
                "intent": intent.get("label"),
                "intent_probability": intent.get("probability"),
            }
        )
    return rows
