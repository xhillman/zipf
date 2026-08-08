"""Persistent keyword watchlist operations."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from typing import Final

from zipf.clock import now_iso
from zipf.db.connection import transaction

# A table view cannot expose more than this many targets at once. Refusing a
# larger internal batch bounds accidental callers without limiting the total
# number of durable watchlist entries.
MAX_TOGGLE_TARGETS: Final = 5_000


def _targets(keywords: Collection[str]) -> tuple[str, ...]:
    targets = tuple(dict.fromkeys(keywords))
    if not targets:
        raise ValueError("watchlist toggle requires at least one keyword")
    if len(targets) > MAX_TOGGLE_TARGETS:
        raise ValueError(f"watchlist toggle is limited to {MAX_TOGGLE_TARGETS:,} keywords")
    return targets


def _are_watched(conn: sqlite3.Connection, keywords: tuple[str, ...]) -> bool:
    return all(
        conn.execute("SELECT 1 FROM watchlist WHERE keyword = ?", (keyword,)).fetchone()
        is not None
        for keyword in keywords
    )


def are_watched(conn: sqlite3.Connection, keywords: Collection[str]) -> bool:
    """Return whether every supplied keyword is on the watchlist."""
    return _are_watched(conn, _targets(keywords))


def toggle(conn: sqlite3.Connection, keywords: Collection[str]) -> bool:
    """Atomically toggle a keyword batch and return its new watched state.

    A mixed batch becomes fully watched. A batch is removed only when every
    target was already watched, so one keypress never unexpectedly drops the
    watched members of a mixed selection.
    """
    targets = _targets(keywords)
    with transaction(conn):
        watching = not _are_watched(conn, targets)
        if watching:
            added_at = now_iso()
            conn.executemany(
                "INSERT INTO watchlist (keyword, added_at) VALUES (?, ?) "
                "ON CONFLICT (keyword) DO NOTHING",
                ((keyword, added_at) for keyword in targets),
            )
        else:
            conn.executemany(
                "DELETE FROM watchlist WHERE keyword = ?",
                ((keyword,) for keyword in targets),
            )
    return watching
