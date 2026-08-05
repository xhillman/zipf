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


def test_keyword_view_leads_with_what_decides_a_keyword(
    seeded: sqlite3.Connection,
) -> None:
    """The landing table: demand, what the searcher wanted, and what it costs."""
    spec = views.table_for(seeded, View(views.KEYWORDS))
    assert spec.columns == ("keyword", "volume", "intent", "difficulty", "cpc")
    assert spec.key_kind == views.KEYWORD_KEY


def test_age_renders_only_past_ttl(db: sqlite3.Connection) -> None:
    """Below the TTL the stored figure is current, so the age column stays silent.

    Asserted on the domain table, which is where an age column now lives: the
    keyword table leads with what decides a keyword, and staleness has its own
    view and its own sidebar count.
    """
    recent = to_iso(now() - timedelta(days=2))
    old = to_iso(now() - timedelta(days=45))
    for domain, observed in (("fresh.com", recent), ("stale.com", old)):
        db.execute(
            "INSERT INTO domain_keyword (domain, keyword, position, url, observed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (domain, "a keyword", 4, f"https://{domain}/x", observed),
        )

    spec = views.table_for(db, View(views.DOMAINS))
    ages = {str(spec.keys[i]): str(row[3]) for i, row in enumerate(spec.rows)}
    assert ages["fresh.com"] == ""
    assert ages["stale.com"] == "45d!"


def test_sorting_by_rank_needs_the_domain_that_holds_it(
    seeded: sqlite3.Connection,
) -> None:
    """Own rank left the table but not the query: it still orders it.

    Without a domain there is no rank to order by, so the sort has to be
    harmless rather than wrong — every row is an unknown and falls back to the
    keyword, not to a position it does not have.
    """
    ranked = views.table_for(seeded, View(views.KEYWORDS), sort="position", own_domain="ahrefs.com")
    assert ranked.keys[0] == "fresh keyword"  # the only one with a rank

    without = views.table_for(seeded, View(views.KEYWORDS), sort="position")
    assert sorted(without.keys) == without.keys


def test_stored_text_is_never_parsed_as_markup(db: sqlite3.Connection) -> None:
    """A keyword containing brackets must survive intact.

    ``DataTable`` runs bare strings through ``Text.from_markup``, so an
    unwrapped cell would silently drop "[free]" as an unknown style tag.
    """
    _keyword(db, "best crm [free]", 100, to_iso(now()))
    spec = views.table_for(db, View(views.KEYWORDS))
    cell = spec.rows[0][0]
    assert isinstance(cell, Text)
    # The leading gutter is the mark column; the keyword itself is untouched.
    assert str(cell).lstrip() == "best crm [free]"


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
    assert views.sort_for_column(views.KEYWORDS, "volume") == "volume"
    assert views.sort_for_column(views.KEYWORDS, "difficulty") == "difficulty"
    assert views.sort_for_column(views.KEYWORDS, "cpc") == "cpc"
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
    # The representative, not the variant. Leading gutter is the mark column.
    assert str(filtered.rows[0][0]).lstrip() == "best crm software"


# ---------------------------------------------------------------------------
# Questions, sort marks, marks and density
# ---------------------------------------------------------------------------


def test_every_view_states_the_question_it_answers(seeded: sqlite3.Connection) -> None:
    """A bucket name says which table you are in, which you already know."""
    for kind in (views.KEYWORDS, views.DOMAINS, views.GAPS, views.STALE, views.RESPONSES):
        assert views.table_for(seeded, View(kind)).question.endswith("?")


def test_the_sorted_column_is_marked_in_its_header(seeded: sqlite3.Connection) -> None:
    """`dev/notes.md` asks for a sorted-by indicator; it goes on the column."""
    unsorted = views.table_for(seeded, View(views.KEYWORDS))
    assert unsorted.columns == ("keyword", "volume", "intent", "difficulty", "cpc")

    by_volume = views.table_for(seeded, View(views.KEYWORDS), sort="volume")
    assert by_volume.columns[1] == "volume" + views.SORT_MARK

    by_difficulty = views.table_for(seeded, View(views.KEYWORDS), sort="difficulty")
    assert by_difficulty.columns[3] == "difficulty" + views.SORT_MARK


def test_a_marked_column_still_resolves_to_its_sort_key() -> None:
    """Otherwise sorting by a column once would make that column inert."""
    assert views.sort_for_column(views.KEYWORDS, "volume" + views.SORT_MARK) == "volume"


def test_marking_shows_a_glyph_without_reflowing_the_table(
    seeded: sqlite3.Connection,
) -> None:
    """The gutter is reserved whether or not anything is marked.

    Without that, marking a row would shift every column beside it by two
    characters, which is a redraw of the whole table for one keypress.
    """
    spec = views.table_for(seeded, View(views.KEYWORDS), marked=frozenset({"stale keyword"}))
    cells = {str(spec.keys[i]): row[0] for i, row in enumerate(spec.rows)}

    assert str(cells["stale keyword"]) == f"{views.MARK} stale keyword"
    assert str(cells["fresh keyword"]) == "  fresh keyword"
    assert len(str(cells["stale keyword"])) - len("stale keyword") == 2
    assert len(str(cells["fresh keyword"])) - len("fresh keyword") == 2


def test_sources_are_hidden_until_asked_for(seeded: sqlite3.Connection) -> None:
    """Default to silence: raw_id is only interesting once you doubt something."""
    quiet = views.table_for(seeded, View(views.KEYWORDS))
    loud = views.table_for(seeded, View(views.KEYWORDS), forensic=True)
    assert "src" not in quiet.columns
    assert loud.columns[-2:] == ("src", "fetched")
    assert len(loud.rows[0]) == len(quiet.rows[0]) + 2


# ---------------------------------------------------------------------------
# The refresh planner
# ---------------------------------------------------------------------------


def test_the_planner_names_the_command_that_acts_on_it(db: sqlite3.Connection) -> None:
    """It is a view, not a control: the only way to act on it is to type."""
    from zipf.sources.dataforseo import labs

    aged = to_iso(now() - timedelta(days=45))
    raw = db.execute(
        "INSERT INTO raw_response (capability, params_hash, params_json, body, cost_usd, "
        "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        (labs.SEARCH_VOLUME, "h", "{}", b"{}", 0.0, aged),
    ).lastrowid
    db.execute(
        "INSERT INTO keyword (keyword, volume, updated_at, raw_id) VALUES (?, ?, ?, ?)",
        ("aged out", 8100, aged, raw),
    )

    spec = views.table_for(db, View(views.STALE))
    assert ":vol --stale" in spec.hint
    assert "$0.01212" in spec.hint  # 0.012 base + one row
    assert "1 keyword" in spec.caption


def test_an_empty_planner_offers_no_command(db: sqlite3.Connection) -> None:
    """Nothing to buy is not an opportunity to sell something."""
    spec = views.table_for(db, View(views.STALE))
    assert spec.hint == ""
    assert "inside its" in spec.caption


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_keyword_rows_carry_the_response_that_produced_them(db: sqlite3.Connection) -> None:
    raw = db.execute(
        "INSERT INTO raw_response (capability, params_hash, params_json, body, cost_usd, "
        "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("labs.search_volume", "h", "{}", b"{}", 0.012, to_iso(now())),
    ).lastrowid
    db.execute(
        "INSERT INTO keyword (keyword, volume, updated_at, raw_id) VALUES (?, ?, ?, ?)",
        ("tracked", 100, to_iso(now()), raw),
    )
    spec = views.table_for(db, View(views.KEYWORDS))
    assert spec.source_of(0) == raw


def test_a_row_with_no_single_source_offers_none(seeded: sqlite3.Connection) -> None:
    """A gap cluster collapses several rows; guessing one source would be wrong."""
    spec = views.table_for(seeded, View(views.DOMAINS))
    assert spec.source_of(0) is None


def test_following_a_response_reaches_the_rows_it_wrote() -> None:
    assert views.drill_target(View(views.RESPONSES), "52") == View(views.RESPONSE, "52")
    assert views.drill_target(View(views.RESPONSE), "keyword") == View(views.KEYWORDS)
    assert views.drill_target(View(views.RESPONSE), "not_a_table") is None


def test_an_unknown_response_id_renders_a_view_rather_than_raising(
    db: sqlite3.Connection,
) -> None:
    """Reached by keypress from a row, so it must not crash mid-render."""
    assert views.table_for(db, View(views.RESPONSE, "9999")).is_empty
    assert views.table_for(db, View(views.RESPONSE, "not-a-number")).is_empty


# ---------------------------------------------------------------------------
# The detail pane, matching the mockup
# ---------------------------------------------------------------------------


def _priced(conn: sqlite3.Connection, keyword: str) -> int:
    raw_id = conn.execute(
        "INSERT INTO raw_response (capability, params_hash, params_json, body, cost_usd, "
        "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("labs.search_volume", "h-d", "{}", b"{}", 0.01212, to_iso(now())),
    ).lastrowid
    conn.execute(
        "INSERT INTO keyword (keyword, volume, cpc, updated_at, raw_id, difficulty, intent, "
        "intent_probability) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (keyword, 8100, 22.4, to_iso(now()), raw_id, 68, "commercial", 0.97),
    )
    for index in range(12):
        conn.execute(
            "INSERT INTO keyword_month (keyword, year, month, volume, raw_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (keyword, 2026, index + 1, 1000 * (index + 1), raw_id),
        )
    return int(raw_id)


def test_detail_shows_difficulty_and_intent(db: sqlite3.Connection) -> None:
    """Volume and cpc describe an advertising market; difficulty is the only
    organic figure stored, and reading the first two without it is how a keyword
    looks worth writing about right up until you see the 68."""
    from zipf.services import browse

    _priced(db, "best crm software")
    markup = views.detail_markup(browse.keyword_detail(db, "best crm software"), "x")
    assert "difficulty 68" in markup
    assert "commercial" in markup
    assert "97%" in markup  # the confidence behind the label


def test_detail_draws_the_monthly_series(db: sqlite3.Connection) -> None:
    from zipf.services import browse

    _priced(db, "best crm software")
    markup = views.detail_markup(browse.keyword_detail(db, "best crm software"), "x")
    assert "12mo" in markup
    assert "1,000 to 12,000" in markup


def test_detail_names_what_the_source_cost(db: sqlite3.Connection) -> None:
    from zipf.services import browse

    raw_id = _priced(db, "best crm software")
    markup = views.detail_markup(browse.keyword_detail(db, "best crm software"), "x")
    assert f"from #{raw_id}" in markup
    assert "labs.search_volume" in markup
    assert "$0.01212" in markup


def test_a_flat_series_renders_flat() -> None:
    """Scaled against its own maximum: the question is when in the year, not how big."""
    assert len(set(views.sparkline([500] * 12))) == 1


def test_a_seasonal_series_peaks_where_the_data_does() -> None:
    series = [10, 10, 10, 10, 10, 10, 10, 10, 10, 900, 10, 10]
    drawn = views.sparkline(series)
    assert drawn[9] == "█"
    assert drawn.count("█") == 1


def test_an_absent_series_draws_nothing(db: sqlite3.Connection) -> None:
    assert views.sparkline([]) == ""


def test_the_landing_table_shows_intent_difficulty_and_cpc(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO keyword (keyword, volume, cpc, updated_at, difficulty, intent, "
        "intent_probability) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("best crm software", 8100, 22.4, to_iso(now()), 68, "commercial", 0.97),
    )
    row = views.table_for(db, View(views.KEYWORDS)).rows[0]
    assert str(row[1]) == "8,100"
    assert str(row[2]) == "commercial"
    assert str(row[3]) == "68"
    assert str(row[4]) == "$22.40"


def test_an_unbought_attribute_is_absent_not_zero(db: sqlite3.Connection) -> None:
    """A keyword autocomplete suggested has no difficulty, cpc or intent.

    Rendering those as 0 would read as "free clicks, trivially easy", which is
    the most expensive misreading this table could produce.
    """
    db.execute(
        "INSERT INTO keyword (keyword, updated_at) VALUES (?, ?)",
        ("only suggested", to_iso(now())),
    )
    row = views.table_for(db, View(views.KEYWORDS)).rows[0]
    assert [str(cell) for cell in row[1:]] == ["—", "—", "—", "—"]


def test_difficulty_sorts_easiest_first(db: sqlite3.Connection) -> None:
    """An easy keyword is the good one; burying it under 90s answers the
    opposite question to the one being asked."""
    for keyword, difficulty in (("hard", 90), ("easy", 12), ("unknown", None)):
        db.execute(
            "INSERT INTO keyword (keyword, volume, updated_at, difficulty) VALUES (?, ?, ?, ?)",
            (keyword, 100, to_iso(now()), difficulty),
        )
    spec = views.table_for(db, View(views.KEYWORDS), sort="difficulty")
    assert spec.keys == ["easy", "hard", "unknown"]  # unknown last, never first
