"""Search Console. Tier 0, free, and therefore refreshed greedily.

This is the user's own real data and the fix for a cold-start empty database
(PRD F7).

One ``fetch`` call is one page of results. Paging is a service-level loop, so
each page is independently cached and a resumed import re-reads nothing it
already holds.

Positions here are **averaged over the requested period**, not integer ranks.
They project to ``gsc_query``, never to ``observation`` (spec D3).
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping
from typing import Any, Final

import httpx

from zipf.errors import VendorError
from zipf.pricing import PriceEstimate, free

API_ROOT: Final = "https://searchconsole.googleapis.com/webmasters/v3"
CAPABILITY: Final = "gsc.search_analytics"

#: Vendor maximum is 25,000 rows per request.
PAGE_SIZE: Final = 25_000

#: Bound on a single import (spec §2.5). 20 pages at 25,000 rows is 500,000
#: rows, far past what one personal site produces in a 16-month window.
MAX_PAGES: Final = 20

DIMENSIONS: Final = ("query", "page", "date")


def build(params: Mapping[str, Any]) -> httpx.Request:
    """Build one searchAnalytics.query page request.

    The bearer token is attached by the capability's auth provider, not here, so
    that credentials stay out of ``params_hash``.
    """
    site_url = params["site_url"]
    body = {
        "startDate": params["start_date"],
        "endDate": params["end_date"],
        "dimensions": list(DIMENSIONS),
        "rowLimit": int(params.get("row_limit", PAGE_SIZE)),
        "startRow": int(params.get("start_row", 0)),
        "dataState": "final",
    }
    # The property id is one path segment, so every character in it must be
    # escaped: 'sc-domain:example.com' and 'https://example.com/' both contain
    # characters that would otherwise restructure the URL.
    property_id = urllib.parse.quote(str(site_url), safe="")
    return httpx.Request(
        "POST",
        f"{API_ROOT}/sites/{property_id}/searchAnalytics/query",
        json=body,
        headers={"Content-Type": "application/json"},
    )


def price(params: Mapping[str, Any]) -> PriceEstimate:
    return free(tier=0, rows=int(params.get("row_limit", PAGE_SIZE)))


SITES_CAPABILITY: Final = "gsc.sites"


def build_sites(params: Mapping[str, Any]) -> httpx.Request:
    """List the properties this account can read. Needed to learn a property id."""
    return httpx.Request("GET", f"{API_ROOT}/sites")


def price_sites(params: Mapping[str, Any]) -> PriceEstimate:
    return free(tier=0)


def parse_sites(body: bytes, params: Mapping[str, Any]) -> list[dict[str, str]]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise VendorError(capability=SITES_CAPABILITY, detail=f"body was not JSON: {exc}") from exc

    entries = payload.get("siteEntry", []) if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        raise VendorError(capability=SITES_CAPABILITY, detail="'siteEntry' was not a list")

    return [
        {
            "site_url": str(entry.get("siteUrl", "")),
            "permission": str(entry.get("permissionLevel", "")),
        }
        for entry in entries
        if isinstance(entry, dict)
    ]


def parse(body: bytes, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten one page into row dicts keyed by dimension name."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise VendorError(capability=CAPABILITY, detail=f"body was not JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise VendorError(capability=CAPABILITY, detail="response was not a JSON object")

    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        raise VendorError(capability=CAPABILITY, detail="'rows' was not a list")

    parsed: list[dict[str, Any]] = []
    for row in rows:
        keys = row.get("keys", [])
        if len(keys) != len(DIMENSIONS):
            continue
        parsed.append(
            {
                "query": keys[0],
                "page": keys[1],
                "date": keys[2],
                "clicks": int(row.get("clicks", 0)),
                "impressions": int(row.get("impressions", 0)),
                "ctr": float(row.get("ctr", 0.0)),
                "position": float(row.get("position", 0.0)),
            }
        )
    return parsed
