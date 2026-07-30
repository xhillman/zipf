"""SQLite connection management.

Connections run in autocommit mode (``isolation_level=None``) so that transaction
boundaries are written explicitly at the call site rather than inferred by the
driver. ``fetch()`` depends on that: the insert into ``raw_response`` and the
projection derived from it must commit together or not at all.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from zipf.errors import DatabaseMissingError

# WAL lets the TUI read while the job runner writes. NORMAL synchronous is safe
# under WAL for this workload; the durability gap is a crash mid-commit losing
# the last transaction, which a re-fetch recovers.
PRAGMAS: Final[dict[str, str | int]] = {
    "journal_mode": "WAL",
    "synchronous": "NORMAL",
    "foreign_keys": "ON",
    "busy_timeout": 5000,
}


def _configure(conn: sqlite3.Connection, *, read_only: bool) -> None:
    conn.row_factory = sqlite3.Row
    for name, value in PRAGMAS.items():
        # journal_mode is a persistent database property, not a connection one,
        # and setting it on a read-only handle fails.
        if read_only and name == "journal_mode":
            continue
        conn.execute(f"PRAGMA {name} = {value}")


def open_rw(path: Path) -> sqlite3.Connection:
    """Open a read-write connection, creating the file if absent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    _configure(conn, read_only=False)
    return conn


def open_ro(path: Path) -> sqlite3.Connection:
    """Open a read-only connection.

    Read-only is enforced by SQLite via the URI flag rather than by inspecting
    statements, so consumers such as the MCP server cannot write regardless of
    what SQL they are handed.
    """
    if not path.exists():
        raise DatabaseMissingError(str(path))
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    _configure(conn, read_only=True)
    return conn


@contextmanager
def connect(path: Path, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
    """Open a connection and close it on every exit path."""
    conn = open_ro(path) if read_only else open_rw(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside one transaction, rolling back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
