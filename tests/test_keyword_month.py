"""The ``keyword_month`` projection.

Two properties matter here beyond the row counts. Replaying a response must not
duplicate its history (R3), and the volume that arrived in the same paid call
must land in ``keyword`` rather than being thrown away and re-bought from Labs.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from zipf.clock import now_iso
from zipf.projections.rebuild import count_rows, project
from zipf.projections.rebuild import rebuild as rebuild_projections
from zipf.sources.dataforseo import keywords_data


def _body(items: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        {
            "status_code": 20000,
            "cost": 0.09,
            "tasks": [{"status_code": 20000, "result": items}],
        }
    ).encode()


def _item(keyword: str, volume: int = 8100, months: int = 12) -> dict[str, Any]:
    return {
        "keyword": keyword,
        "search_volume": volume,
        "cpc": 3.25,
        "competition_index": 60,
        "monthly_searches": [
            {"year": 2026, "month": month, "search_volume": 100 * month}
            for month in range(1, months + 1)
        ],
    }


def _store(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> int:
    cursor = conn.execute(
        "INSERT INTO raw_response (capability, params_hash, params_json, body, cost_usd, "
        "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            keywords_data.CAPABILITY,
            f"h{len(items)}",
            '{"seeds": ["crm"]}',
            _body(items),
            0.09,
            now_iso(),
        ),
    )
    return int(cursor.lastrowid or 0)


def test_the_series_lands_one_row_per_month(db: sqlite3.Connection) -> None:
    raw_id = _store(db, [_item("crm software")])
    project(db, raw_id)

    rows = db.execute(
        "SELECT year, month, volume FROM keyword_month WHERE keyword = ? ORDER BY year, month",
        ("crm software",),
    ).fetchall()
    assert len(rows) == 12
    assert rows[0]["month"] == 1
    assert rows[11]["volume"] == 1200


def test_the_volume_from_the_same_call_is_kept(db: sqlite3.Connection) -> None:
    """It was paid for in this response; discarding it means buying it twice."""
    project(db, _store(db, [_item("crm software", volume=8100)]))

    row = db.execute(
        "SELECT volume, cpc, competition FROM keyword WHERE keyword = ?", ("crm software",)
    ).fetchone()
    assert row["volume"] == 8100
    assert row["cpc"] == 3.25
    assert row["competition"] == 0.6  # scaled from the vendor's 0-100 index


def test_replaying_a_response_does_not_duplicate_history(db: sqlite3.Connection) -> None:
    """R3: applying the same raw row twice leaves the table as it was once."""
    raw_id = _store(db, [_item("crm software")])
    project(db, raw_id)
    project(db, raw_id)

    assert count_rows(db, "keyword_month") == 12


def test_a_later_response_overwrites_the_month_it_re_reports(db: sqlite3.Connection) -> None:
    """A refreshed series corrects its own months rather than appending them."""
    project(db, _store(db, [_item("crm software", months=1)]))
    assert db.execute("SELECT volume FROM keyword_month").fetchone()["volume"] == 100

    updated = _item("crm software", months=1)
    updated["monthly_searches"][0]["search_volume"] = 999
    project(db, _store(db, [updated]))

    rows = db.execute("SELECT volume FROM keyword_month").fetchall()
    assert len(rows) == 1
    assert rows[0]["volume"] == 999


def test_rebuild_restores_the_series_from_stored_bytes(db: sqlite3.Connection) -> None:
    """The recovery story for a wrong number is a rebuild, never an UPDATE."""
    project(db, _store(db, [_item("crm software"), _item("free crm")]))
    before = count_rows(db, "keyword_month")

    stats = rebuild_projections(db, keywords_data.CAPABILITY)

    assert count_rows(db, "keyword_month") == before == 24
    assert "keyword_month" in stats.tables_cleared
    assert not stats.lost_rows


def test_rebuilding_this_capability_also_replays_everything_sharing_keyword(
    db: sqlite3.Connection,
) -> None:
    """It writes `keyword`, so a rebuild must not wipe what autocomplete put there.

    The projector declares both tables, and ``_closure`` pulls in every other
    capability that writes either — without it, rebuilding this one would delete
    keywords discovered by autocomplete and never restore them.
    """
    stats = rebuild_projections(db, keywords_data.CAPABILITY)
    assert "keyword" in stats.tables_cleared
    assert "autocomplete.suggest" in stats.capabilities
    assert "labs.search_volume" in stats.capabilities


def test_a_keyword_with_no_history_writes_nothing(db: sqlite3.Connection) -> None:
    """Not every keyword carries a series; its absence is not an error."""
    item = _item("crm software")
    del item["monthly_searches"]
    project(db, _store(db, [item]))

    assert count_rows(db, "keyword_month") == 0
    assert count_rows(db, "keyword") == 1  # the volume still landed


def test_a_labs_volume_pull_also_writes_its_history(db: sqlite3.Connection) -> None:
    """Discovery is no longer the only source of a series.

    ``keyword_overview`` nests twelve months inside ``keyword_info``, paid for in
    the same call that priced the keyword. Before H1 those months were parsed
    away and the same history was bought again from the discovery endpoint.
    """
    body = json.dumps(
        {
            "status_code": 20000,
            "cost": 0.012,
            "tasks": [
                {
                    "status_code": 20000,
                    "result": [
                        {
                            "items": [
                                {
                                    "keyword": "systems thinking",
                                    "keyword_info": {
                                        "search_volume": 9900,
                                        "cpc": 7.39,
                                        "competition": 0.12,
                                        "monthly_searches": [
                                            {
                                                "year": 2026,
                                                "month": month,
                                                "search_volume": 100 * month,
                                            }
                                            for month in range(1, 13)
                                        ],
                                    },
                                }
                            ]
                        }
                    ],
                }
            ],
        }
    ).encode()
    cursor = db.execute(
        "INSERT INTO raw_response (capability, params_hash, params_json, body, cost_usd, "
        "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "labs.search_volume",
            "hvol",
            '{"keywords": ["systems thinking"]}',
            body,
            0.012,
            now_iso(),
        ),
    )
    project(db, int(cursor.lastrowid or 0))

    assert count_rows(db, "keyword_month") == 12
    assert db.execute("SELECT volume FROM keyword").fetchone()["volume"] == 9900
