"""Difficulty and intent: the two bulk keyword attributes.

The property worth the most care is not the values themselves but what these
projectors must *not* touch. Both write into the same ``keyword`` row that
carries volume, and volume's provenance decides whether it gets bought again.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from zipf.clock import now_iso
from zipf.errors import InvalidRequestError
from zipf.projections.rebuild import project
from zipf.services.volume import fresh_keywords
from zipf.sources.dataforseo import labs


def _body(items: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        {
            "status_code": 20000,
            "cost": 0.012,
            "tasks": [{"status_code": 20000, "result": [{"items": items}]}],
        }
    ).encode()


def _store(conn: sqlite3.Connection, capability: str, body: bytes, params: str = "{}") -> int:
    cursor = conn.execute(
        "INSERT INTO raw_response (capability, params_hash, params_json, body, cost_usd, "
        "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        (capability, f"h{capability}{params}", params, body, 0.012, now_iso()),
    )
    return int(cursor.lastrowid or 0)


def test_difficulty_is_parsed_from_the_nested_items(db: sqlite3.Connection) -> None:
    """Unlike keywords_for_keywords, these Labs endpoints nest an items array."""
    rows = labs.parse_bulk_keyword_difficulty(
        _body([{"keyword": "Best CRM", "keyword_difficulty": 47}]), {}
    )
    assert rows == [{"keyword": "best crm", "difficulty": 47}]


def test_intent_keeps_the_probability_alongside_the_label(db: sqlite3.Connection) -> None:
    """97% navigational and 51% navigational are not the same claim."""
    rows = labs.parse_search_intent(
        _body(
            [
                {
                    "keyword": "login page",
                    "keyword_intent": {"label": "navigational", "probability": 0.97},
                }
            ]
        ),
        {},
    )
    assert rows[0]["intent"] == "navigational"
    assert rows[0]["intent_probability"] == 0.97


def test_search_intent_sends_no_location(db: sqlite3.Connection) -> None:
    """The one Labs endpoint that takes no location: intent is about the phrase."""
    request = labs.build_search_intent({"keywords": ["free crm"]})
    payload = json.loads(request.content)[0]

    assert "location_code" not in payload
    assert payload["language_code"] == "en"


def test_difficulty_does_send_a_location(db: sqlite3.Connection) -> None:
    """Difficulty is a property of a market, so it needs one."""
    payload = json.loads(labs.build_bulk_keyword_difficulty({"keywords": ["free crm"]}).content)[0]
    assert payload["location_code"] == 2840


def test_both_endpoints_refuse_more_than_the_bulk_cap() -> None:
    too_many = {"keywords": [f"keyword {i}" for i in range(1001)]}
    for build in (labs.build_bulk_keyword_difficulty, labs.build_search_intent):
        with pytest.raises(InvalidRequestError) as caught:
            build(too_many)
        assert "1,000" in caught.value.problem


def test_both_price_at_the_labs_rate() -> None:
    """The same base plus per-item charge as every other Labs call."""
    params = {"keywords": ["a", "b", "c"]}
    assert labs.price_bulk_keyword_difficulty(params).usd == 0.01236
    assert labs.price_search_intent(params).usd == 0.01236


def test_difficulty_lands_on_the_keyword_row(db: sqlite3.Connection) -> None:
    raw_id = _store(
        db,
        labs.BULK_KEYWORD_DIFFICULTY,
        _body([{"keyword": "free crm", "keyword_difficulty": 63}]),
    )
    project(db, raw_id)

    assert (
        db.execute("SELECT difficulty FROM keyword WHERE keyword = 'free crm'").fetchone()[
            "difficulty"
        ]
        == 63
    )


def test_an_attribute_pull_does_not_disturb_a_measured_volume(db: sqlite3.Connection) -> None:
    """The subtlety this projector exists to get right.

    ``fresh_keywords`` decides whether volume needs buying by joining
    ``keyword.raw_id`` against the measuring capabilities. If a difficulty pull
    repointed ``raw_id`` at itself, an already-paid-for volume would look
    unmeasured and the next ``zipf vol`` would buy it a second time.
    """
    volume_id = _store(
        db,
        labs.SEARCH_VOLUME,
        _body([{"keyword": "free crm", "keyword_info": {"search_volume": 9900, "cpc": 3.0}}]),
        params='{"keywords": ["free crm"]}',
    )
    project(db, volume_id)
    assert fresh_keywords(db, ["free crm"]) == {"free crm"}

    difficulty_id = _store(
        db,
        labs.BULK_KEYWORD_DIFFICULTY,
        _body([{"keyword": "free crm", "keyword_difficulty": 63}]),
    )
    project(db, difficulty_id)

    row = db.execute("SELECT volume, difficulty, raw_id FROM keyword").fetchone()
    assert row["volume"] == 9900, "volume was clobbered by an attribute pull"
    assert row["difficulty"] == 63
    assert row["raw_id"] == volume_id, "provenance was repointed at the attribute call"
    assert fresh_keywords(db, ["free crm"]) == {"free crm"}, "volume now looks unbought"


def test_an_attribute_pull_introduces_an_unknown_keyword(db: sqlite3.Connection) -> None:
    """A keyword nobody had seen still gets a row, just without volume."""
    project(
        db,
        _store(
            db,
            labs.SEARCH_INTENT,
            _body(
                [
                    {
                        "keyword": "brand new term",
                        "keyword_intent": {"label": "commercial", "probability": 0.8},
                    }
                ]
            ),
        ),
    )

    row = db.execute(
        "SELECT volume, intent FROM keyword WHERE keyword = 'brand new term'"
    ).fetchone()
    assert row["intent"] == "commercial"
    assert row["volume"] is None
    assert fresh_keywords(db, ["brand new term"]) == set(), "intent is not a volume measurement"


def test_replaying_an_attribute_response_is_idempotent(db: sqlite3.Connection) -> None:
    raw_id = _store(
        db,
        labs.BULK_KEYWORD_DIFFICULTY,
        _body([{"keyword": "free crm", "keyword_difficulty": 63}]),
    )
    project(db, raw_id)
    project(db, raw_id)

    assert db.execute("SELECT COUNT(*) AS n FROM keyword").fetchone()["n"] == 1
