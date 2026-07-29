"""Labs projections: paid measurement versus free discovery.

The rule under test is that a free suggestion can create a keyword row but never
overwrite a volume that was paid for, while a newer paid response may.
"""

from __future__ import annotations

import json
import sqlite3

from zipf.projections.rebuild import rebuild
from zipf.sources import autocomplete
from zipf.sources.dataforseo import labs


def _envelope(items: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {
            "status_code": 20000,
            "cost": 0.0126,
            "tasks": [{"status_code": 20000, "result": [{"items": items}]}],
        }
    ).encode()


def _volume_item(keyword: str, volume: int, cpc: float = 1.5) -> dict[str, object]:
    return {
        "keyword": keyword,
        "keyword_info": {"search_volume": volume, "cpc": cpc, "competition": 0.4},
    }


def _ranked_item(keyword: str, position: int, url: str, volume: int) -> dict[str, object]:
    return {
        "keyword_data": {
            "keyword": keyword,
            "keyword_info": {"search_volume": volume, "cpc": 2.0, "competition": 0.3},
        },
        "ranked_serp_element": {"serp_item": {"rank_absolute": position, "url": url}},
    }


def _intersection_item(keyword: str, p1: int | None, p2: int | None) -> dict[str, object]:
    return {
        "keyword_data": {
            "keyword": keyword,
            "keyword_info": {"search_volume": 500, "cpc": 3.0, "competition": 0.2},
        },
        "first_domain_serp_element": {"rank_absolute": p1, "url": f"https://a.com/{keyword}"},
        "second_domain_serp_element": {"rank_absolute": p2, "url": f"https://b.com/{keyword}"},
    }


def _insert(
    conn: sqlite3.Connection,
    capability: str,
    body: bytes,
    fetched_at: str,
    params: dict[str, object],
) -> int:
    cursor = conn.execute(
        "INSERT INTO raw_response "
        "(capability, params_hash, params_json, body, cost_usd, fetched_at) "
        "VALUES (?, ?, ?, ?, 0.0126, ?)",
        (capability, fetched_at, json.dumps(params), body, fetched_at),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def test_paid_volume_survives_later_free_discovery(db: sqlite3.Connection) -> None:
    """Autocomplete replaying after Labs must not null out a paid volume."""
    _insert(
        db,
        labs.SEARCH_VOLUME,
        _envelope([_volume_item("best crm", 3600)]),
        "2026-07-01T00:00:00Z",
        {"keywords": ["best crm"]},
    )
    _insert(
        db,
        autocomplete.CAPABILITY,
        json.dumps(["crm", ["best crm", "free crm"], [], [], {}]).encode(),
        "2026-07-02T00:00:00Z",
        {"seed": "crm"},
    )

    rebuild(db)

    rows = {r["keyword"]: r["volume"] for r in db.execute("SELECT keyword, volume FROM keyword")}
    assert rows["best crm"] == 3600, "free discovery erased a paid volume"
    assert rows["free crm"] is None, "discovery invented a volume it never bought"


def test_newer_paid_measurement_replaces_older(db: sqlite3.Connection) -> None:
    _insert(
        db,
        labs.SEARCH_VOLUME,
        _envelope([_volume_item("best crm", 3600)]),
        "2026-07-01T00:00:00Z",
        {"keywords": ["best crm"]},
    )
    _insert(
        db,
        labs.SEARCH_VOLUME,
        _envelope([_volume_item("best crm", 4400)]),
        "2026-07-20T00:00:00Z",
        {"keywords": ["best crm"]},
    )

    rebuild(db)

    row = db.execute("SELECT volume FROM keyword WHERE keyword = 'best crm'").fetchone()
    assert row["volume"] == 4400


def test_ranked_keywords_fills_both_tables(db: sqlite3.Connection) -> None:
    _insert(
        db,
        labs.RANKED_KEYWORDS,
        _envelope([_ranked_item("best crm", 14, "https://ahrefs.com/crm", 3600)]),
        "2026-07-01T00:00:00Z",
        {"domain": "ahrefs.com", "limit": 100},
    )

    rebuild(db)

    rank = db.execute("SELECT * FROM domain_keyword").fetchone()
    assert rank["domain"] == "ahrefs.com"
    assert rank["keyword"] == "best crm"
    assert rank["position"] == 14
    assert rank["observed_at"] == "2026-07-01T00:00:00Z"

    # Volume rode along with the ranks and was already paid for.
    assert db.execute("SELECT volume FROM keyword").fetchone()["volume"] == 3600


def test_intersection_attributes_positions_to_the_right_domains(db: sqlite3.Connection) -> None:
    """Domains come from params; the body reports two positions and names neither."""
    _insert(
        db,
        labs.DOMAIN_INTERSECTION,
        _envelope([_intersection_item("best crm", 3, 17)]),
        "2026-07-01T00:00:00Z",
        {"target1": "a.com", "target2": "b.com", "limit": 100},
    )

    rebuild(db)

    positions = {
        r["domain"]: r["position"]
        for r in db.execute("SELECT domain, position FROM domain_keyword")
    }
    assert positions == {"a.com": 3, "b.com": 17}


def test_a_domain_that_does_not_rank_writes_no_row(db: sqlite3.Connection) -> None:
    """A null position is an absence, not a rank of zero.

    This is what makes the gap query work: 'ranks for X and I do not' is the
    absence of a row, so a fabricated 0 would silently break it.
    """
    _insert(
        db,
        labs.DOMAIN_INTERSECTION,
        _envelope([_intersection_item("best crm", 3, None)]),
        "2026-07-01T00:00:00Z",
        {"target1": "a.com", "target2": "b.com", "limit": 100},
    )

    rebuild(db)

    domains = [r["domain"] for r in db.execute("SELECT domain FROM domain_keyword")]
    assert domains == ["a.com"]


def test_rank_history_accumulates_across_observations(db: sqlite3.Connection) -> None:
    """R4: two pulls of the same domain keep both ranks, not just the latest."""
    for date, position in (("2026-07-01T00:00:00Z", 14), ("2026-07-08T00:00:00Z", 9)):
        _insert(
            db,
            labs.RANKED_KEYWORDS,
            _envelope([_ranked_item("best crm", position, "https://ahrefs.com/crm", 3600)]),
            date,
            {"domain": "ahrefs.com", "limit": 100},
        )

    rebuild(db)

    history = [
        (r["observed_at"], r["position"])
        for r in db.execute("SELECT observed_at, position FROM domain_keyword ORDER BY observed_at")
    ]
    assert history == [("2026-07-01T00:00:00Z", 14), ("2026-07-08T00:00:00Z", 9)]


def test_labs_rebuild_is_deterministic(db: sqlite3.Connection) -> None:
    _insert(
        db,
        labs.RANKED_KEYWORDS,
        _envelope([_ranked_item("best crm", 14, "https://ahrefs.com/crm", 3600)]),
        "2026-07-01T00:00:00Z",
        {"domain": "ahrefs.com", "limit": 100},
    )

    rebuild(db)
    first = db.execute("SELECT * FROM domain_keyword").fetchall()
    rebuild(db)
    second = db.execute("SELECT * FROM domain_keyword").fetchall()

    assert [tuple(r) for r in first] == [tuple(r) for r in second]
