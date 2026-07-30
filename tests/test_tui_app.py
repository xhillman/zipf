"""The app boots, browses, and quits.

Textual's ``run_test`` drives a real app in a headless terminal, so these are
integration tests over the same widgets a person sees — not renders of a mock.
The table's *contents* are asserted in ``test_tui_views``; here the concern is
wiring: does selecting a node change the table, does the cursor move the detail
pane, does the app survive an empty database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from textual.widgets import DataTable, Static, Tree

from zipf.db.connection import open_ro
from zipf.errors import DatabaseMissingError
from zipf.tui import views
from zipf.tui.app import ZipfApp


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
