"""Google Autocomplete. Tier 0, free, unauthenticated.

One ``fetch`` call is one HTTP request for one seed. Alphabet expansion and
question expansion are service-level loops over many seeds, so each expanded
seed becomes its own cache entry with its own TTL. Batching them into a single
fetch would make the whole expansion expire together and would re-request seeds
that are individually still fresh.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

from zipf.errors import VendorError
from zipf.pricing import PriceEstimate, free

ENDPOINT = "https://www.google.com/complete/search"
CAPABILITY = "autocomplete.suggest"


def build(params: Mapping[str, Any]) -> httpx.Request:
    """Build the request. This function never sends it (R1)."""
    return httpx.Request(
        "GET",
        ENDPOINT,
        params={
            "client": "chrome",
            "q": params["seed"],
            "hl": params.get("lang", "en"),
            "gl": params.get("country", "us"),
        },
        headers={"User-Agent": "zipf/0.1 (+https://zipf.dev)"},
    )


def price(params: Mapping[str, Any]) -> PriceEstimate:
    return free(tier=0)


def _decode(body: bytes) -> Any:
    """Decode the response body.

    The endpoint labels itself ``text/javascript`` and is inconsistent about
    encoding, so UTF-8 is tried first and Latin-1 is the fallback. Latin-1 cannot
    fail, which means a mangled character is preferable to losing a paid-for
    body — and this body is the cached source of truth.
    """
    for encoding in ("utf-8", "latin-1"):
        try:
            return json.loads(body.decode(encoding))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise VendorError(capability=CAPABILITY, detail="response was not decodable JSON")


def parse(body: bytes, params: Mapping[str, Any]) -> list[str]:
    """Extract suggestions.

    The Chrome-client response is a positional array: ``[query, [suggestions],
    [descriptions], [], {metadata}]``. Only element 1 is load-bearing.
    """
    payload = _decode(body)

    if not isinstance(payload, list) or len(payload) < 2:
        raise VendorError(
            capability=CAPABILITY, detail=f"unexpected shape: {type(payload).__name__}"
        )

    suggestions = payload[1]
    if not isinstance(suggestions, list):
        raise VendorError(capability=CAPABILITY, detail="element 1 was not a list of suggestions")

    return [s for s in suggestions if isinstance(s, str)]
