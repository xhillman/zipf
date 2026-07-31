"""The Google Ads keywords_for_keywords adapter.

The response body here mirrors the documented envelope: a flat ``result`` list
with no nested ``items``, a word-valued ``competition`` alongside a 0-100
``competition_index``, and a twelve-month ``monthly_searches`` series. Each of
those differs from the Labs endpoints, and each has its own test because getting
one wrong fails silently rather than loudly.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from zipf import capabilities
from zipf.errors import InvalidRequestError
from zipf.sources.dataforseo import keywords_data


def _envelope(items: list[dict[str, Any]], cost: float = 0.09) -> bytes:
    return json.dumps(
        {
            "status_code": 20000,
            "status_message": "Ok.",
            "cost": cost,
            "tasks": [{"status_code": 20000, "status_message": "Ok.", "result": items}],
        }
    ).encode()


def _item(keyword: str, **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "keyword": keyword,
        "search_volume": 8100,
        "cpc": 12.5,
        "competition": "HIGH",
        "competition_index": 85,
        "monthly_searches": [
            {"year": 2026, "month": month, "search_volume": 1000 * month} for month in range(1, 13)
        ],
    }
    item.update(overrides)
    return item


def test_the_request_carries_the_seeds_and_one_task() -> None:
    request = keywords_data.build({"seeds": ["CRM Software", "  free crm  "]})
    payload = json.loads(request.content)

    assert len(payload) == 1, "zipf sends one task per request so one fetch is one cache entry"
    assert payload[0]["keywords"] == ["crm software", "free crm"]
    assert payload[0]["location_code"] == 2840
    assert request.url.path.endswith("/keywords_data/google_ads/keywords_for_keywords/live")


def test_duplicate_seeds_collapse_into_one() -> None:
    """Two spellings of one seed must not become two paid calls."""
    assert keywords_data.normalise_seeds(["CRM", "crm", " crm "]) == ["crm"]


def test_seed_order_is_preserved() -> None:
    assert keywords_data.normalise_seeds(["b", "a", "c"]) == ["b", "a", "c"]


def test_too_many_seeds_is_refused_before_paying() -> None:
    """The vendor fails the whole task above twenty, so this must not be sent."""
    with pytest.raises(InvalidRequestError) as caught:
        keywords_data.normalise_seeds([f"seed {i}" for i in range(21)])
    assert "20" in str(caught.value.fix)


def test_an_overlong_seed_is_refused() -> None:
    with pytest.raises(InvalidRequestError):
        keywords_data.normalise_seeds(["x" * 81])


def test_no_seeds_at_all_is_refused() -> None:
    with pytest.raises(InvalidRequestError):
        keywords_data.normalise_seeds(["", "   "])


def test_results_are_read_flat_not_from_nested_items() -> None:
    """Labs nests an `items` array; this family does not.

    Reading through ``items_of`` here would return an empty list and look like a
    keyword with no matches rather than a parsing bug.
    """
    rows = keywords_data.parse(_envelope([_item("best crm software")]), {})
    assert len(rows) == 1
    assert rows[0]["keyword"] == "best crm software"
    assert rows[0]["volume"] == 8100


def test_competition_is_scaled_to_the_units_every_other_source_uses() -> None:
    """Labs stores 0-1; this family reports 0-100. The column holds one meaning."""
    rows = keywords_data.parse(_envelope([_item("crm", competition_index=85)]), {})
    assert rows[0]["competition"] == 0.85


def test_the_competition_word_is_never_stored() -> None:
    """`competition` arrives as HIGH/MEDIUM/LOW and the column is numeric."""
    rows = keywords_data.parse(_envelope([_item("crm", competition="HIGH")]), {})
    assert isinstance(rows[0]["competition"], float)


def test_a_missing_competition_index_is_none_not_zero() -> None:
    """Unknown and "no competition" are different facts."""
    item = _item("crm")
    del item["competition_index"]
    assert keywords_data.parse(_envelope([item]), {})[0]["competition"] is None


def test_twelve_months_of_history_come_back_oldest_first() -> None:
    series = keywords_data.parse(_envelope([_item("crm")]), {})[0]["monthly"]
    assert len(series) == 12
    assert [row["month"] for row in series] == list(range(1, 13))
    assert series[0]["volume"] == 1000


def test_history_is_sorted_even_when_the_vendor_shuffles_it() -> None:
    item = _item(
        "crm",
        monthly_searches=[
            {"year": 2026, "month": 3, "search_volume": 30},
            {"year": 2025, "month": 12, "search_volume": 12},
            {"year": 2026, "month": 1, "search_volume": 10},
        ],
    )
    series = keywords_data.parse(_envelope([item]), {})[0]["monthly"]
    assert [(row["year"], row["month"]) for row in series] == [(2025, 12), (2026, 1), (2026, 3)]


def test_a_keyword_with_no_name_is_skipped() -> None:
    assert keywords_data.parse(_envelope([{"search_volume": 10}]), {}) == []


def test_an_empty_result_parses_to_nothing_rather_than_raising() -> None:
    """A seed with no matches is an answer, not a failure."""
    assert keywords_data.parse(_envelope([]), {}) == []


def test_the_price_is_flat_and_the_row_count_is_unknown() -> None:
    """Unlike every other call zipf makes, depth does not change the bill."""
    few = keywords_data.price({"seeds": ["crm"]})
    many = keywords_data.price({"seeds": [f"seed {i}" for i in range(20)]})

    assert few.usd == many.usd == 0.09
    assert few.rows is None
    assert "unknown rows" in few.describe()


def test_the_capability_is_registered_with_a_monthly_ttl() -> None:
    capability = capabilities.get(keywords_data.CAPABILITY)
    assert capability.tier == 1
    assert capability.ttl.days == 30
    assert capability.cost_from_response is not None, "the vendor reports what it charged"
