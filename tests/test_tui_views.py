"""The view layer: what each sidebar selection puts in the table.

No Textual here. These are the assertions that would be painful to make through
a terminal and that matter most — column shapes, silence below the TTL, and
markup in stored data not being interpreted.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from rich.text import Text

from zipf.clock import now, to_iso
from zipf.services.browse import CacheCounts
from zipf.services.budget import BudgetStatus
from zipf.tui import views
from zipf.tui.views import View


def _keyword(conn: sqlite3.Connection, keyword: str, volume: int | None, updated: str) -> None:
    conn.execute(
        "INSERT INTO keyword (keyword, volume, cpc, has_aio, updated_at) VALUES (?, ?, ?, ?, ?)",
        (keyword, volume, 1.5, None, updated),
    )


@pytest.fixture
def seeded(db: sqlite3.Connection) -> sqlite3.Connection:
    fresh = to_iso(now() - timedelta(days=2))
    stale = to_iso(now() - timedelta(days=45))
    _keyword(db, "fresh keyword", 8100, fresh)
    _keyword(db, "stale keyword", 2400, stale)
    db.execute(
        "INSERT INTO domain_keyword (domain, keyword, position, url, observed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("ahrefs.com", "fresh keyword", 14, "https://ahrefs.com/blog", fresh),
    )
    return db


def test_keyword_view_has_the_columns_from_the_mock(seeded: sqlite3.Connection) -> None:
    spec = views.table_for(seeded, View(views.KEYWORDS))
    assert spec.columns == ("keyword", "vol", "age", "aio", "pos")
    assert spec.key_kind == views.KEYWORD_KEY


def test_age_renders_only_past_ttl(seeded: sqlite3.Connection) -> None:
    """Below the TTL the stored figure is current, so the age column stays silent."""
    spec = views.table_for(seeded, View(views.KEYWORDS))
    ages = {str(spec.keys[i]): str(row[2]) for i, row in enumerate(spec.rows)}
    assert ages["fresh keyword"] == ""
    assert ages["stale keyword"] == "45d!"


def test_own_rank_appears_only_for_the_given_domain(seeded: sqlite3.Connection) -> None:
    with_domain = views.table_for(seeded, View(views.KEYWORDS), own_domain="ahrefs.com")
    positions = {str(with_domain.keys[i]): str(row[4]) for i, row in enumerate(with_domain.rows)}
    assert positions["fresh keyword"] == "14"

    without = views.table_for(seeded, View(views.KEYWORDS))
    assert all(str(row[4]) == "—" for row in without.rows)


def test_stored_text_is_never_parsed_as_markup(db: sqlite3.Connection) -> None:
    """A keyword containing brackets must survive intact.

    ``DataTable`` runs bare strings through ``Text.from_markup``, so an
    unwrapped cell would silently drop "[free]" as an unknown style tag.
    """
    _keyword(db, "best crm [free]", 100, to_iso(now()))
    spec = views.table_for(db, View(views.KEYWORDS))
    cell = spec.rows[0][0]
    assert isinstance(cell, Text)
    assert str(cell) == "best crm [free]"


def test_domain_view_keys_are_not_keywords(seeded: sqlite3.Connection) -> None:
    """The detail pane must not look up "ahrefs.com" as a keyword."""
    spec = views.table_for(seeded, View(views.DOMAINS))
    assert spec.key_kind == views.OPAQUE_KEY
    assert spec.keys == ["ahrefs.com"]


def test_drilling_a_domain_shows_its_ranks(seeded: sqlite3.Connection) -> None:
    spec = views.table_for(seeded, View(views.DOMAIN, "ahrefs.com"))
    assert spec.columns == ("keyword", "pos", "vol", "url")
    assert "ahrefs.com" in spec.caption
    assert str(spec.rows[0][3]) == "ahrefs.com/blog"  # scheme stripped for width


def test_visibility_is_an_empty_bucket_not_a_missing_one(db: sqlite3.Connection) -> None:
    spec = views.table_for(db, View(views.VISIBILITY))
    assert spec.is_empty
    assert spec.columns == ("subject", "surface", "rate")


def test_unknown_view_falls_back_rather_than_raising(db: sqlite3.Connection) -> None:
    """A view kind with no branch must not crash the app mid-render."""
    assert views.table_for(db, View("nonsense")).is_empty


def test_detail_reports_an_unpriced_keyword_honestly() -> None:
    """A gap keyword was never priced; the pane must not imply a volume of zero."""
    assert "nothing priced" in views.detail_markup(None, "some keyword")


def test_detail_gathers_volume_and_ranks(seeded: sqlite3.Connection) -> None:
    from zipf.services import browse

    detail = browse.keyword_detail(seeded, "fresh keyword")
    markup = views.detail_markup(detail, "fresh keyword")
    assert "8,100" in markup
    assert "ahrefs.com [bold]#14[/]" in markup


def test_status_line_leads_with_the_effective_limit() -> None:
    """The headline is the smaller limit, matching `zipf budget`."""
    state = BudgetStatus(
        spent=2.0,
        ceiling=20.0,
        remaining=18.0,
        threshold=0.0,
        balance=5.0,
        balance_age=timedelta(0),
    )
    totals = CacheCounts(
        keywords=1204,
        domains=4,
        gap_pairs=2,
        gsc_queries=0,
        observations=0,
        responses=49,
        jobs_pending=2,
    )
    line = views.status_line(state, totals)
    assert "$5.00 left" in line  # the balance, not the $18.00 ceiling remainder
    assert "1,204 kw" in line
    assert "2 jobs" in line


def test_status_line_omits_jobs_when_there_are_none() -> None:
    """Default to silence: a zero is not worth a segment of the readout."""
    state = BudgetStatus(
        spent=0.0, ceiling=20.0, remaining=20.0, threshold=0.0, balance=None, balance_age=None
    )
    totals = CacheCounts(
        keywords=0,
        domains=0,
        gap_pairs=0,
        gsc_queries=0,
        observations=0,
        responses=0,
        jobs_pending=0,
    )
    assert "job" not in views.status_line(state, totals)


def test_sort_cycles_wrap_and_start_at_the_default() -> None:
    cycle = views.SORT_CYCLES[views.KEYWORDS]
    current = views.next_sort(views.KEYWORDS, None)
    assert current == "volume"  # volume first: the reason you bought the data

    seen: list[str] = []
    for _ in range(len(cycle)):
        assert current is not None
        seen.append(current)
        current = views.next_sort(views.KEYWORDS, current)

    assert seen == list(cycle)  # every key reachable, none repeated
    assert current == cycle[0]  # and it wraps back to the start


def test_views_without_a_sortable_query_report_none() -> None:
    """A gap pull is already ordered by opportunity; re-sorting answers nothing."""
    assert views.next_sort(views.GAP, None) is None
    assert views.next_sort(views.VISIBILITY, None) is None


def test_every_sort_key_is_one_browse_accepts(seeded: sqlite3.Connection) -> None:
    """The cycle and the allowlist cannot drift apart without this failing."""
    for kind, cycle in views.SORT_CYCLES.items():
        for key in cycle:
            views.table_for(seeded, View(kind), sort=key)


def test_column_headers_map_to_sort_keys() -> None:
    assert views.sort_for_column(views.KEYWORDS, "vol") == "volume"
    assert views.sort_for_column(views.KEYWORDS, "aio") is None  # three values, no order
    assert views.sort_for_column(views.GAP, "vol") is None


def test_drill_goes_from_container_to_contents() -> None:
    assert views.drill_target(View(views.DOMAINS), "ahrefs.com") == View(views.DOMAIN, "ahrefs.com")
    assert views.drill_target(View(views.GAPS), "a.com|b.com") == View(views.GAP, "a.com|b.com")


def test_a_keyword_row_has_nowhere_to_drill_yet() -> None:
    """Honest until the SERP milestone: a keyword has no deeper screen."""
    assert views.drill_target(View(views.KEYWORDS), "free crm") is None


def test_filtering_a_gap_matches_collapsed_variants(db: sqlite3.Connection) -> None:
    """A term matching a restatement must still surface the cluster holding it."""
    db.execute(
        "INSERT INTO raw_response (capability, params_hash, params_json, body, cost_usd, "
        "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("labs.domain_intersection", "h", "{}", b"{}", 0.0, to_iso(now())),
    )
    for keyword, position in (("best crm software", 3), ("crm software best", 4), ("free crm", 9)):
        db.execute(
            "INSERT INTO domain_keyword (domain, keyword, position, url, observed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("them.com", keyword, position, "https://them.com/x", to_iso(now())),
        )

    unfiltered = views.table_for(db, View(views.GAP, "them.com|mine.com"))
    assert len(unfiltered.rows) == 2  # the two phrasings collapsed into one

    filtered = views.table_for(db, View(views.GAP, "them.com|mine.com"), contains="software best")
    assert len(filtered.rows) == 1
    assert str(filtered.rows[0][0]) == "best crm software"  # the representative, not the variant
