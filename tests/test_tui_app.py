"""The app boots, reads the cache, and quits.

Textual's ``run_test`` drives a real app in a headless terminal, so these are
integration tests over the same widgets a person sees — not renders of a mock.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from zipf.db.connection import connect, open_ro
from zipf.errors import DatabaseMissingError
from zipf.tui.app import ZipfApp


async def test_boots_on_an_empty_cache(db: sqlite3.Connection) -> None:
    """An empty database is a valid home screen, not an error."""
    app = ZipfApp(db)
    async with app.run_test() as pilot:
        summary = app.query_one("#cache-summary")
        assert "0 keywords" in str(summary.render())
        await pilot.press("q")
    assert app.return_value is None


async def test_shows_what_the_cache_holds(zipf_home: Path) -> None:
    """The count on screen is the count in the database."""
    with connect(zipf_home / "zipf.db") as writer:
        writer.execute(
            "INSERT INTO raw_response (capability, params_hash, params_json, body, "
            "cost_usd, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("autocomplete.suggest", "h", "{}", b"[]", 0.0, "2026-07-30T00:00:00Z"),
        )
        writer.execute(
            "INSERT INTO keyword (keyword, volume, updated_at) VALUES (?, ?, ?)",
            ("crm software", 8100, "2026-07-30T00:00:00Z"),
        )

    conn = open_ro(zipf_home / "zipf.db")
    try:
        app = ZipfApp(conn)
        async with app.run_test():
            rendered = str(app.query_one("#cache-summary").render())
        assert "1 keyword" in rendered
        assert "1 stored response" in rendered
    finally:
        conn.close()


async def test_browsing_cannot_write(zipf_home: Path) -> None:
    """The handle the app browses on is refused writes by SQLite itself."""
    conn = open_ro(zipf_home / "zipf.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO keyword (keyword) VALUES ('x')")
    finally:
        conn.close()


def test_missing_database_names_its_fix(tmp_path: Path) -> None:
    """Opening the browser before `zipf init` says what to run."""
    with pytest.raises(DatabaseMissingError) as caught:
        open_ro(tmp_path / "absent.db")
    assert "zipf init" in str(caught.value.fix)
