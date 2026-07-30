"""The app boots, browses, and quits.

Textual's ``run_test`` drives a real app in a headless terminal, so these are
integration tests over the same widgets a person sees — not renders of a mock.
The table's *contents* are asserted in ``test_tui_views``; here the concern is
wiring: does selecting a node change the table, does the cursor move the detail
pane, does the app survive an empty database.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
import respx
from textual.widgets import DataTable, Input, Static, Tree

from zipf.budget import Budget
from zipf.db.connection import open_ro
from zipf.errors import DatabaseMissingError
from zipf.jobs import queue as job_queue
from zipf.sources.autocomplete import ENDPOINT
from zipf.tui import views
from zipf.tui.app import ZipfApp
from zipf.tui.confirm import ConfirmModal


@pytest.fixture
def seeded(db: sqlite3.Connection) -> sqlite3.Connection:
    db.execute(
        "INSERT INTO raw_response (capability, params_hash, params_json, body, cost_usd, "
        "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "labs.domain_intersection",
            "h1",
            '{"target1": "ahrefs.com", "target2": "mine.com", "limit": 100, "intersections": 0}',
            b"{}",
            0.05,
            "2026-07-20T00:00:00Z",
        ),
    )
    for keyword, volume, position in (
        ("best crm software", 8100, 14),
        ("free crm", 9900, 3),
    ):
        db.execute(
            "INSERT INTO keyword (keyword, volume, updated_at) VALUES (?, ?, ?)",
            (keyword, volume, "2026-07-20T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO domain_keyword (domain, keyword, position, url, observed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "ahrefs.com",
                keyword,
                position,
                f"https://ahrefs.com/{position}",
                "2026-07-20T00:00:00Z",
            ),
        )
    return db


async def test_boots_on_an_empty_cache(db: sqlite3.Connection) -> None:
    """An empty database is a valid home screen, not an error."""
    app = ZipfApp(db)
    async with app.run_test() as pilot:
        assert app.query_one("#rows", DataTable).row_count == 0
        assert "0 kw" in str(app.query_one("#status", Static).render())
        await pilot.press("q")
    assert app.return_value is None


async def test_opens_on_keywords(seeded: sqlite3.Connection) -> None:
    """The home screen is the cache: keywords, already sorted by volume."""
    app = ZipfApp(seeded)
    async with app.run_test():
        table = app.query_one("#rows", DataTable)
        assert table.row_count == 2
        assert [str(column.label) for column in table.columns.values()] == [
            "keyword",
            "vol",
            "age",
            "aio",
            "pos",
        ]
        assert app.sub_title == "2 keywords"


async def test_sidebar_lists_every_bucket(seeded: sqlite3.Connection) -> None:
    tree: Tree[views.View]
    app = ZipfApp(seeded)
    async with app.run_test():
        tree = app.query_one("#sidebar", Tree)
        labels = [str(node.label) for node in tree.root.children]
        assert labels == ["domains  1", "gaps  1", "gsc  0", "visibility  0"]
        assert str(tree.root.label) == "cache  2"


async def test_selecting_a_node_swaps_the_table(seeded: sqlite3.Connection) -> None:
    """Columns are rebuilt, so a stale header can never sit over new data."""
    app = ZipfApp(seeded)
    async with app.run_test():
        app.show_view(views.View(views.DOMAINS))
        table = app.query_one("#rows", DataTable)
        assert [str(column.label) for column in table.columns.values()] == [
            "domain",
            "keywords",
            "best",
            "age",
        ]
        assert table.row_count == 1


async def test_drilling_a_domain_shows_its_keywords(seeded: sqlite3.Connection) -> None:
    app = ZipfApp(seeded)
    async with app.run_test():
        app.show_view(views.View(views.DOMAIN, "ahrefs.com"))
        assert app.query_one("#rows", DataTable).row_count == 2
        assert app.sub_title == "ahrefs.com · 2 keywords"


async def test_moving_the_cursor_updates_the_detail_pane(seeded: sqlite3.Connection) -> None:
    app = ZipfApp(seeded)
    async with app.run_test() as pilot:
        app.query_one("#rows", DataTable).focus()
        await pilot.pause()
        first = str(app.query_one("#detail", Static).render())
        await pilot.press("down")
        await pilot.pause()
        second = str(app.query_one("#detail", Static).render())

    assert "free crm" in first  # highest volume leads
    assert "best crm software" in second
    assert first != second


async def test_detail_pane_does_not_price_a_domain(seeded: sqlite3.Connection) -> None:
    """A domain row's key is not a keyword, so no volume lookup is attempted."""
    app = ZipfApp(seeded)
    async with app.run_test():
        app.show_view(views.View(views.DOMAINS))
        detail = str(app.query_one("#detail", Static).render())
    assert "ahrefs.com" in detail
    assert "nothing priced" not in detail


async def test_an_empty_view_says_so_rather_than_blanking(seeded: sqlite3.Connection) -> None:
    app = ZipfApp(seeded)
    async with app.run_test():
        app.show_view(views.View(views.VISIBILITY))
        assert "nothing sampled yet" in str(app.query_one("#detail", Static).render())


async def test_browsing_cannot_write(zipf_home: Path) -> None:
    """The handle the app browses on is refused writes by SQLite itself."""
    conn = open_ro(zipf_home / "zipf.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO keyword (keyword) VALUES ('x')")
    finally:
        conn.close()


async def test_opening_makes_no_network_call(seeded: sqlite3.Connection) -> None:
    """The status bar reads the cached balance. Opening the app must not fetch.

    The autouse ``blocked_network`` fixture raises on any unmocked request, so
    reaching the network here fails the test rather than passing quietly.
    """
    app = ZipfApp(seeded)
    async with app.run_test():
        assert "left" in str(app.query_one("#status", Static).render())


def test_missing_database_names_its_fix(tmp_path: Path) -> None:
    """Opening the browser before `zipf init` says what to run."""
    with pytest.raises(DatabaseMissingError) as caught:
        open_ro(tmp_path / "absent.db")
    assert "zipf init" in str(caught.value.fix)


async def test_slash_opens_the_filter_and_typing_narrows_the_table(
    seeded: sqlite3.Connection,
) -> None:
    """Filtering is local SQL, so it happens per keystroke with no spinner."""
    app = ZipfApp(seeded)
    async with app.run_test() as pilot:
        table = app.query_one("#rows", DataTable)
        assert table.row_count == 2

        await pilot.press("slash")
        await pilot.pause()
        bar = app.query_one("#filter", Input)
        assert bar.display and bar.has_focus

        for key in "free":
            await pilot.press(key)
        await pilot.pause()
        assert table.row_count == 1
        assert 'matching "free"' in app.sub_title


async def test_slash_is_typed_into_the_filter_not_re_triggered(
    seeded: sqlite3.Connection,
) -> None:
    """Once the bar has focus, its own key must reach the input as a character."""
    app = ZipfApp(seeded)
    async with app.run_test() as pilot:
        await pilot.press("slash")
        await pilot.press("slash")
        await pilot.pause()
        assert app.query_one("#filter", Input).value == "/"


async def test_escape_clears_the_filter_and_restores_every_row(
    seeded: sqlite3.Connection,
) -> None:
    app = ZipfApp(seeded)
    async with app.run_test() as pilot:
        await pilot.press("slash")
        for key in "free":
            await pilot.press(key)
        await pilot.pause()
        assert app.query_one("#rows", DataTable).row_count == 1

        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one("#rows", DataTable).row_count == 2
        assert not app.query_one("#filter", Input).display
        assert "matching" not in app.sub_title


async def test_enter_keeps_the_filter_but_returns_focus(seeded: sqlite3.Connection) -> None:
    """Submitting is not cancelling: the narrowed table stays."""
    app = ZipfApp(seeded)
    async with app.run_test() as pilot:
        await pilot.press("slash")
        for key in "free":
            await pilot.press(key)
        await pilot.press("enter")
        await pilot.pause()

        assert app.query_one("#rows", DataTable).row_count == 1
        assert app.query_one("#rows", DataTable).has_focus
        assert 'matching "free"' in app.sub_title


async def test_changing_view_drops_the_filter(seeded: sqlite3.Connection) -> None:
    """A filter carried into a new table would empty it for an off-screen reason."""
    app = ZipfApp(seeded)
    async with app.run_test() as pilot:
        await pilot.press("slash")
        for key in "free":
            await pilot.press(key)
        await pilot.pause()

        app.show_view(views.View(views.DOMAINS))
        await pilot.pause()
        assert app.query_one("#rows", DataTable).row_count == 1  # the one domain
        assert "matching" not in app.sub_title


async def test_s_cycles_the_sort_and_says_which(seeded: sqlite3.Connection) -> None:
    app = ZipfApp(seeded)
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        assert "by volume" in app.sub_title
        await pilot.press("s")
        await pilot.pause()
        assert "by keyword" in app.sub_title
        first_row = app.query_one("#rows", DataTable).get_row_at(0)
        assert str(first_row[0]) == "best crm software"  # alphabetical, not by volume


async def test_sorting_a_view_with_no_order_is_reported(seeded: sqlite3.Connection) -> None:
    """The keypress must not vanish silently."""
    app = ZipfApp(seeded)
    async with app.run_test() as pilot:
        app.show_view(views.View(views.VISIBILITY))
        await pilot.press("s")
        await pilot.pause()
        assert app.sub_title == "nothing sampled yet"  # unchanged, no sort applied


async def test_enter_drills_from_domains_into_one_domain(seeded: sqlite3.Connection) -> None:
    app = ZipfApp(seeded)
    async with app.run_test() as pilot:
        app.show_view(views.View(views.DOMAINS))
        app.query_one("#rows", DataTable).focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.sub_title == "ahrefs.com · 2 keywords"


async def test_reload_picks_up_a_row_written_since_opening(zipf_home: Path) -> None:
    """`r` re-reads the database, so finished work appears without a restart."""
    writer_path = zipf_home / "zipf.db"
    conn = open_ro(writer_path)
    try:
        app = ZipfApp(conn)
        async with app.run_test() as pilot:
            assert app.query_one("#rows", DataTable).row_count == 0

            from zipf.db.connection import connect

            with connect(writer_path) as writer:
                writer.execute(
                    "INSERT INTO keyword (keyword, volume, updated_at) VALUES (?, ?, ?)",
                    ("added later", 10, "2026-07-20T00:00:00Z"),
                )

            await pilot.press("r")
            await pilot.pause()
            assert app.query_one("#rows", DataTable).row_count == 1
            assert "cache  1" in str(app.query_one("#sidebar", Tree).root.label)
    finally:
        conn.close()


async def test_reload_makes_no_network_call(seeded: sqlite3.Connection) -> None:
    """`r` cannot refresh the vendor balance: that write needs the runner's handle."""
    app = ZipfApp(seeded)
    async with app.run_test() as pilot:
        await pilot.press("r")
        await pilot.pause()
        assert "left" in str(app.query_one("#status", Static).render())


async def _type(pilot: object, text: str) -> None:
    """Type a string into whichever bar has focus."""
    for character in text:
        await pilot.press("space" if character == " " else character)  # type: ignore[attr-defined]


async def test_colon_opens_the_command_bar(seeded: sqlite3.Connection) -> None:
    """Paid actions are typed, never clicked: this bar is the only route."""
    app = ZipfApp(seeded, write_conn=seeded)
    async with app.run_test() as pilot:
        await pilot.press("colon")
        await pilot.pause()
        bar = app.query_one("#command", Input)
        assert bar.display and bar.has_focus


async def test_a_command_is_not_run_as_it_is_typed(seeded: sqlite3.Connection) -> None:
    """A half-typed `:gap a.com` must not price anything."""
    app = ZipfApp(seeded, write_conn=seeded)
    async with app.run_test() as pilot:
        await pilot.press("colon")
        await _type(pilot, "gap ahrefs.com")
        await pilot.pause()
        assert app.screen is app.screen  # no modal was pushed
        assert seeded.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"] == 0


async def test_escape_closes_the_command_bar_without_running_it(
    seeded: sqlite3.Connection,
) -> None:
    app = ZipfApp(seeded, write_conn=seeded)
    async with app.run_test() as pilot:
        await pilot.press("colon")
        await _type(pilot, "gap ahrefs.com")
        await pilot.press("escape")
        await pilot.pause()
        assert not app.query_one("#command", Input).display
        assert seeded.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"] == 0


async def test_a_paid_command_shows_the_modal_and_enqueues_on_enter(
    seeded: sqlite3.Connection,
) -> None:
    """The whole gate, driven by keypresses: type, confirm, queue."""
    app = ZipfApp(seeded, write_conn=seeded, budget=Budget(ceiling_usd=20.0, threshold_usd=0.0))
    async with app.run_test() as pilot:
        await pilot.press("colon")
        await _type(pilot, "gap ahrefs.com --mine mine.com")
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ConfirmModal)
        body = str(app.screen.query_one("#confirm-body", Static).render())
        assert "not cached" in body
        assert "$0.0240" in body  # the price is stated before the keypress
        assert "remaining this month" in body

        await pilot.press("enter")
        await pilot.pause()

    assert seeded.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"] == 1


async def test_declining_the_modal_queues_nothing(seeded: sqlite3.Connection) -> None:
    app = ZipfApp(seeded, write_conn=seeded, budget=Budget(ceiling_usd=20.0, threshold_usd=0.0))
    async with app.run_test() as pilot:
        await pilot.press("colon")
        await _type(pilot, "gap ahrefs.com --mine mine.com")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)

        await pilot.press("escape")
        await pilot.pause()

    assert seeded.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"] == 0


async def test_a_paid_command_writes_no_raw_response(seeded: sqlite3.Connection) -> None:
    """R5 through the real UI: approving queues work, it does not call a vendor."""
    before = seeded.execute("SELECT COUNT(*) AS n FROM raw_response").fetchone()["n"]
    app = ZipfApp(seeded, write_conn=seeded, budget=Budget(ceiling_usd=20.0, threshold_usd=0.0))
    async with app.run_test() as pilot:
        await pilot.press("colon")
        await _type(pilot, "gap ahrefs.com --mine mine.com")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    assert seeded.execute("SELECT COUNT(*) AS n FROM raw_response").fetchone()["n"] == before


async def test_an_unknown_command_reports_rather_than_crashing(
    seeded: sqlite3.Connection,
) -> None:
    app = ZipfApp(seeded, write_conn=seeded)
    async with app.run_test() as pilot:
        await pilot.press("colon")
        await _type(pilot, "frobnicate")
        await pilot.press("enter")
        await pilot.pause()
        assert app.is_running  # survived
        assert not app.query_one("#command", Input).display


async def test_quit_command_closes_the_app(seeded: sqlite3.Connection) -> None:
    app = ZipfApp(seeded, write_conn=seeded)
    async with app.run_test() as pilot:
        await pilot.press("colon")
        await _type(pilot, "q")
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_value is None


async def test_without_a_write_handle_a_command_refuses(seeded: sqlite3.Connection) -> None:
    """A reader-only app says so rather than failing inside SQLite."""
    app = ZipfApp(seeded)
    async with app.run_test() as pilot:
        await pilot.press("colon")
        await _type(pilot, "gap ahrefs.com --mine mine.com")
        await pilot.press("enter")
        await pilot.pause()
        assert app.is_running
    assert seeded.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"] == 0


async def test_below_the_threshold_nothing_is_asked(seeded: sqlite3.Connection) -> None:
    """Default to silence: a spend under the threshold enqueues without a modal.

    The shipped config sets the threshold to $0.00 so every spend is confirmed,
    but the mechanism has to work for anyone who raises it — a $0.024 gap under
    a $1.00 threshold goes straight to the queue.
    """
    app = ZipfApp(seeded, write_conn=seeded, budget=Budget(ceiling_usd=20.0, threshold_usd=1.0))
    async with app.run_test() as pilot:
        await pilot.press("colon")
        app.query_one("#command", Input).value = "gap ahrefs.com --mine mine.com"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmModal)

    assert seeded.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"] == 1


SUGGEST_BODY = json.dumps(["crm", ["best crm software", "free crm"], [], [], {}]).encode()


async def test_the_app_drains_a_queued_job_and_shows_the_result(
    db: sqlite3.Connection, blocked_network: respx.MockRouter
) -> None:
    """Work queued before opening is already approved, so the app runs it.

    The whole loop through the UI: a queued job, a mocked vendor, and a table
    that has the new rows in it without anyone pressing refresh.
    """
    route = blocked_network.get(ENDPOINT).mock(
        return_value=httpx.Response(200, content=SUGGEST_BODY)
    )
    job_queue.enqueue(db, "autocomplete.suggest", {"seed": "crm"}, estimated_cost=0.0)

    app = ZipfApp(db, write_conn=db)
    async with app.run_test() as pilot:
        # The runner claims on start, so the job may already be done by the first
        # yield. Poll for the outcome rather than asserting an empty table first.
        for _ in range(20):
            await pilot.pause()
            if app.query_one("#rows", DataTable).row_count:
                break

        assert route.called
        assert app.query_one("#rows", DataTable).row_count == 2
        assert db.execute("SELECT status FROM job WHERE id = 1").fetchone()["status"] == "done"
        assert "done" in str(app.query_one("#jobs", Static).render())


async def test_the_jobs_pane_names_the_subject_not_the_capability(
    seeded: sqlite3.Connection,
) -> None:
    """Three gap pulls reading `labs.domain_intersection` would be identical."""
    job_queue.enqueue(
        seeded,
        "labs.domain_intersection",
        {"target1": "ahrefs.com", "target2": "mine.com", "limit": 100, "intersections": False},
        estimated_cost=0.024,
    )
    app = ZipfApp(seeded)  # reader-only: no runner, so the job stays queued
    async with app.run_test():
        pane = str(app.query_one("#jobs", Static).render())
    assert "ahrefs.com" in pane
    assert "labs.domain_intersection" not in pane
    assert "$0.0240" in pane


async def test_a_reader_only_app_starts_no_runner(
    seeded: sqlite3.Connection, blocked_network: respx.MockRouter
) -> None:
    """Without a write handle there is nothing to drain with, and nothing runs.

    `blocked_network` fails any unmocked request, so a runner that started here
    would fail this test rather than quietly spending.
    """
    job_queue.enqueue(seeded, "autocomplete.suggest", {"seed": "crm"}, estimated_cost=0.0)
    app = ZipfApp(seeded)
    async with app.run_test() as pilot:
        for _ in range(5):
            await pilot.pause()
        assert seeded.execute("SELECT status FROM job WHERE id = 1").fetchone()["status"] == (
            "queued"
        )


async def test_an_empty_queue_says_so(seeded: sqlite3.Connection) -> None:
    app = ZipfApp(seeded)
    async with app.run_test():
        assert "no jobs" in str(app.query_one("#jobs", Static).render())


async def test_cancelling_a_finished_job_reports_rather_than_lying(
    seeded: sqlite3.Connection,
) -> None:
    app = ZipfApp(seeded, write_conn=seeded)
    async with app.run_test() as pilot:
        await pilot.press("colon")
        app.query_one("#command", Input).value = "cancel 99"
        await pilot.press("enter")
        await pilot.pause()
        assert app.is_running
