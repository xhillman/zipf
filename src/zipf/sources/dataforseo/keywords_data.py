"""Keywords Data — Google Ads. A second DataForSEO API family, same credentials.

``keywords_for_keywords`` is the discovery endpoint: hand it up to twenty seed
words and it returns keywords *with volume already attached*, plus twelve months
of history for each. Autocomplete gives strings and no numbers; Labs gives
numbers for strings you already have. This gives both at once.

Three things differ from the Labs endpoints in ``labs.py``, and each one is a
correctness trap rather than a preference:

1. **The result is a flat list.** Labs nests an ``items`` array inside each
   result; this does not. Using ``client.items_of`` here silently returns
   nothing.
2. **Competition is a word, not a number.** This family reports ``competition``
   as LOW/MEDIUM/HIGH and ``competition_index`` as 0-100, while Labs reports a
   0-1 float. The ``keyword`` table stores the Labs units, so the index is
   divided by 100 rather than the word being stored in a numeric column.
3. **The price is flat.** Every other DataForSEO call zipf makes is a base fee
   plus a per-row charge. This is $0.09 whether twenty thousand keywords come
   back or thirty.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx

from zipf.errors import InvalidRequestError
from zipf.pricing import PriceEstimate
from zipf.sources.dataforseo import client

CAPABILITY: Final = "keywords_data.keywords_for_keywords"

#: Vendor cap on seeds per call. Exceeding it fails the whole task, so the
#: request is rejected here rather than paid for and refused.
MAX_SEEDS: Final = 20

#: Vendor cap on the length of a single seed.
MAX_SEED_LENGTH: Final = 80

#: Flat price per call, confirmed against the vendor's Live Mode pricing on
#: 2026-07-30. Not a function of depth: the same $0.09 buys thirty rows or the
#: twenty thousand the endpoint can return, which is why the command batches
#: seeds rather than asking one question at a time.
PRICE_USD: Final = 0.09

#: Vendor rate limit for this family: twelve requests per minute per account.
#: Recorded here so the runner has something to enforce against.
REQUESTS_PER_MINUTE: Final = 12


def normalise_seeds(seeds: Sequence[str]) -> list[str]:
    """Clean, deduplicate and validate seeds, preserving the order given.

    The vendor lowercases seeds anyway, so doing it here means two spellings of
    one seed collapse into a single cache entry instead of two paid calls.
    """
    seen: dict[str, None] = {}
    for seed in seeds:
        cleaned = seed.strip().lower()
        if not cleaned:
            continue
        if len(cleaned) > MAX_SEED_LENGTH:
            raise InvalidRequestError(
                f"The seed {cleaned[:30]!r}… is {len(cleaned)} characters; the limit is "
                f"{MAX_SEED_LENGTH}.",
                fix="Shorten it, or split it into two shorter seeds.",
            )
        seen.setdefault(cleaned, None)

    if not seen:
        raise InvalidRequestError(
            "No seed keywords were given.",
            fix='Pass at least one, as in `zipf ideas "crm software"`.',
        )
    if len(seen) > MAX_SEEDS:
        raise InvalidRequestError(
            f"{len(seen)} seeds were given; one call takes at most {MAX_SEEDS}.",
            fix=f"Run it again with {MAX_SEEDS} or fewer. Each call is a separate charge.",
        )
    return list(seen)


def build(params: Mapping[str, Any]) -> httpx.Request:
    return client.build_task_request(
        CAPABILITY,
        "/keywords_data/google_ads/keywords_for_keywords/live",
        {
            "keywords": normalise_seeds(params["seeds"]),
            "location_code": int(params.get("location_code", client.DEFAULT_LOCATION_CODE)),
            "language_code": str(params.get("language_code", client.DEFAULT_LANGUAGE_CODE)),
            "sort_by": str(params.get("sort_by", "search_volume")),
        },
    )


def _competition(item: Mapping[str, Any]) -> float | None:
    """Competition as a 0-1 float, matching what the Labs endpoints store.

    This family reports a 0-100 ``competition_index`` alongside a LOW/MEDIUM/HIGH
    word. Storing the index unscaled would put 85 in a column where every other
    source writes 0.85, and every comparison across sources would be wrong by
    two orders of magnitude.
    """
    index = item.get("competition_index")
    return round(index / 100, 4) if isinstance(index, int | float) else None


def parse(body: bytes, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten the response into one record per keyword.

    ``task_results`` is used directly, without ``items_of``: this family returns
    the keyword objects at the top level of ``result``.
    """
    rows: list[dict[str, Any]] = []
    for item in client.task_results(CAPABILITY, body):
        keyword = item.get("keyword")
        if not isinstance(keyword, str) or not keyword:
            continue
        rows.append(
            {
                "keyword": keyword.strip().lower(),
                "volume": item.get("search_volume"),
                "cpc": item.get("cpc"),
                "competition": _competition(item),
                "monthly": client.monthly_series(item),
            }
        )
    return rows


def price(params: Mapping[str, Any]) -> PriceEstimate:
    """A flat charge, with a row count that cannot be known until it arrives.

    ``rows=None`` is the honest answer rather than a guess: the confirm gate
    renders it as "unknown rows", which is what the buyer is actually facing.
    """
    return PriceEstimate(usd=PRICE_USD, tier=1, queue="live", rows=None)
