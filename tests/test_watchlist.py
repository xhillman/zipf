"""Persistent watchlist storage and group toggle behavior."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from zipf import watchlist
from zipf.db.connection import connect


def test_watchlist_survives_a_reopened_database(zipf_home: Path) -> None:
    path = zipf_home / "zipf.db"
    with connect(path) as first:
        assert watchlist.toggle(first, ["best crm software"])

    with connect(path) as reopened:
        assert watchlist.are_watched(reopened, ["best crm software"])


def test_toggling_an_existing_keyword_removes_it(db: sqlite3.Connection) -> None:
    assert watchlist.toggle(db, ["best crm software"])
    assert not watchlist.toggle(db, ["best crm software"])
    assert not db.execute(
        "SELECT 1 FROM watchlist WHERE keyword = ?", ("best crm software",)
    ).fetchone()


def test_a_mixed_batch_becomes_fully_watched(db: sqlite3.Connection) -> None:
    assert watchlist.toggle(db, ["already watched"])

    watching = watchlist.toggle(db, ["already watched", "new keyword"])

    assert watching
    assert watchlist.are_watched(db, ["already watched", "new keyword"])


def test_an_entirely_watched_batch_is_removed(db: sqlite3.Connection) -> None:
    keywords = ["one", "two"]
    assert watchlist.toggle(db, keywords)

    watching = watchlist.toggle(db, keywords)

    assert not watching
    assert not db.execute("SELECT 1 FROM watchlist").fetchone()


def test_toggle_rejects_an_empty_batch(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="at least one"):
        watchlist.toggle(db, [])


def test_toggle_bounds_a_batch(db: sqlite3.Connection) -> None:
    keywords = [f"keyword {index}" for index in range(watchlist.MAX_TOGGLE_TARGETS + 1)]
    with pytest.raises(ValueError, match="limited"):
        watchlist.toggle(db, keywords)
