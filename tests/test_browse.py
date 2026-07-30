"""The cache browser's read layer.

These run against real SQLite with hand-seeded projection rows. Seeding
projections directly is normally forbidden (R3), but here the projectors are not
under test — the inventory queries over their output are, and going through a
fetch would couple every assertion to a vendor fixture.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from zipf.errors import InvalidRequestError
from zipf.services import browse


def _response(conn: sqlite3.Connection, capability: str, params: str, fetched_at: str) -> int:
    cursor = conn.execute(
        "INSERT INTO raw_response (capability, params_hash, params_json, body, "
        "cost_usd, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        (capability, f"h{fetched_at}", params, b"{}", 0.0, fetched_at),
    )
    return int(cursor.lastrowid or 0)


def _keyword(conn: sqlite3.Connection, keyword: str, volume: int | None, **extra: Any) -> None:
    conn.execute(
        "INSERT INTO keyword (keyword, volume, cpc, competition, has_aio, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            keyword,
            volume,
            extra.get("cpc"),
            extra.get("competition"),
            extra.get("has_aio"),
            extra.get("updated_at", "2026-07-01T00:00:00Z"),
        ),
    )


def _rank(
    conn: sqlite3.Connection, domain: str, keyword: str, position: int, observed_at: str
) -> None:
    conn.execute(
        "INSERT INTO domain_keyword (domain, keyword, position, url, observed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (domain, keyword, position, f"https://{domain}/{keyword.replace(' ', '-')}", observed_at),
    )


@pytest.fixture
def seeded(db: sqlite3.Connection) -> sqlite3.Connection:
    _keyword(db, "best crm software", 8100, cpc=12.5, has_aio=1)
    _keyword(db, "crm for freelancers", 2400, has_aio=0)
    _keyword(db, "free crm", 9900, has_aio=1)
    _keyword(db, "never priced", None)

    # ahrefs.com ranks for two of them, and "best crm software" twice — an older
    # observation plus a newer one, so latest-only behaviour is exercised.
    _rank(db, "ahrefs.com", "best crm software", 30, "2026-06-01T00:00:00Z")
    _rank(db, "ahrefs.com", "best crm software", 14, "2026-07-01T00:00:00Z")
    _rank(db, "ahrefs.com", "free crm", 3, "2026-07-01T00:00:00Z")
    _rank(db, "mine.com", "crm for freelancers", 7, "2026-07-01T00:00:00Z")

    _response(
        db,
        "labs.domain_intersection",
        '{"target1": "ahrefs.com", "target2": "mine.com", "limit": 100, "intersections": 0}',
        "2026-07-20T00:00:00Z",
    )
    return db


def test_counts_report_every_bucket(seeded: sqlite3.Connection) -> None:
    totals = browse.counts(seeded)
    assert totals.keywords == 4
    assert totals.domains == 2
    assert totals.gap_pairs == 1
    assert totals.observations == 0  # empty until the SERP and LLM milestones
    assert totals.jobs_pending == 0


def test_counts_an_empty_cache(db: sqlite3.Connection) -> None:
    """An empty database returns zeros, not None and not an error."""
    totals = browse.counts(db)
    assert totals.keywords == 0
    assert totals.gap_pairs == 0
    assert totals.responses == 0


def test_keywords_default_to_volume_order_with_unpriced_last(
    seeded: sqlite3.Connection,
) -> None:
    rows = browse.keywords(seeded)
    assert [row["keyword"] for row in rows] == [
        "free crm",
        "best crm software",
        "crm for freelancers",
        "never priced",
    ]


def test_keywords_join_only_the_newest_rank(seeded: sqlite3.Connection) -> None:
    """A keyword measured twice appears once, at its current position."""
    rows = browse.keywords(seeded, own_domain="ahrefs.com")
    by_keyword = {row["keyword"]: row for row in rows}
    assert len(rows) == 4
    assert by_keyword["best crm software"]["position"] == 14  # not 30
    assert by_keyword["crm for freelancers"]["position"] is None


def test_keywords_without_own_domain_have_no_position(seeded: sqlite3.Connection) -> None:
    assert all(row["position"] is None for row in browse.keywords(seeded))


def test_filter_matches_a_substring(seeded: sqlite3.Connection) -> None:
    rows = browse.keywords(seeded, contains="crm f")
    assert [row["keyword"] for row in rows] == ["crm for freelancers"]


def test_filter_treats_wildcards_literally(seeded: sqlite3.Connection) -> None:
    """``_`` is a LIKE wildcard; someone typing it means the character."""
    _keyword(seeded, "crm_software", 10)
    assert [row["keyword"] for row in browse.keywords(seeded, contains="crm_")] == ["crm_software"]
    assert browse.keywords(seeded, contains="%") == []


def test_sort_keys_are_allowlisted(seeded: sqlite3.Connection) -> None:
    """A column name never reaches SQL from input, and the refusal names the fix."""
    assert browse.keywords(seeded, sort="keyword")[0]["keyword"] == "best crm software"
    with pytest.raises(InvalidRequestError) as caught:
        browse.keywords(seeded, sort="volume; DROP TABLE keyword")
    assert "volume" in str(caught.value.fix)
    assert browse.counts(seeded).keywords == 4  # the table is still there


def test_every_query_is_bounded(seeded: sqlite3.Connection) -> None:
    """A caller cannot ask for more rows than a table can render."""
    assert len(browse.keywords(seeded, limit=2)) == 2
    assert len(browse.keywords(seeded, limit=0)) == 1  # clamped up to one
    assert browse._bounded(10_000_000) == browse.MAX_ROWS


def test_domains_summarise_what_is_known(seeded: sqlite3.Connection) -> None:
    rows = browse.domains(seeded)
    assert [row["domain"] for row in rows] == ["ahrefs.com", "mine.com"]
    ahrefs = rows[0]
    assert ahrefs["keywords"] == 2
    assert ahrefs["best_position"] == 3
    assert ahrefs["last_observed"] == "2026-07-01T00:00:00Z"


def test_domain_keywords_are_current_ranks_only(seeded: sqlite3.Connection) -> None:
    rows = browse.domain_keywords(seeded, "ahrefs.com")
    assert [(row["keyword"], row["position"]) for row in rows] == [
        ("free crm", 3),
        ("best crm software", 14),
    ]


def test_gap_pairs_record_who_was_compared(seeded: sqlite3.Connection) -> None:
    pairs = browse.gap_pairs(seeded)
    assert pairs == [
        {
            "competitor": "ahrefs.com",
            "mine": "mine.com",
            "last_pulled": "2026-07-20T00:00:00Z",
            "pulls": 1,
        }
    ]


def test_gsc_position_is_impression_weighted(db: sqlite3.Connection) -> None:
    """A day with three impressions must not outweigh a day with three thousand."""
    raw_id = _response(db, "gsc.searchanalytics", "{}", "2026-07-01T00:00:00Z")
    for date, impressions, position in (
        ("2026-07-01", 3, 1.0),
        ("2026-07-02", 3000, 41.0),
    ):
        db.execute(
            "INSERT INTO gsc_query (query, page, date, clicks, impressions, ctr, position, "
            "raw_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("free crm", "https://mine.com/crm", date, 1, impressions, 0.01, position, raw_id),
        )

    row = browse.gsc_queries(db)[0]
    assert row["impressions"] == 3003
    assert row["position"] == pytest.approx(40.96, abs=0.01)  # not the 21.0 plain mean


def test_keyword_detail_gathers_every_surface(seeded: sqlite3.Connection) -> None:
    detail = browse.keyword_detail(seeded, "Best CRM Software")  # case-insensitive
    assert detail is not None
    assert detail["volume"] == 8100
    assert [(rank["domain"], rank["position"]) for rank in detail["ranks"]] == [("ahrefs.com", 14)]
    assert detail["gsc"] is None


def test_keyword_detail_absent_is_none(seeded: sqlite3.Connection) -> None:
    assert browse.keyword_detail(seeded, "nothing here") is None
