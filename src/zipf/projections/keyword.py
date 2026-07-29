"""The ``keyword`` projection.

Autocomplete discovers keywords but knows nothing about their volume. Labs knows
volume but discovers nothing. Both write this table, so the autocomplete
projector must never clobber a volume that Labs supplied.

``ON CONFLICT DO NOTHING`` is what makes that true, and it is also what makes
replay order irrelevant to the outcome for this projector.
"""

from __future__ import annotations

import sqlite3

from zipf.projections.base import Projector
from zipf.sources import autocomplete

_INSERT_DISCOVERED = """
INSERT INTO keyword (keyword, updated_at, raw_id)
VALUES (?, ?, ?)
ON CONFLICT (keyword) DO NOTHING
"""


def apply_autocomplete(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    """Record every suggested term as a known keyword."""
    suggestions = autocomplete.parse(row["body"], {})
    conn.executemany(
        _INSERT_DISCOVERED,
        [(term, row["fetched_at"], row["id"]) for term in suggestions],
    )
    return len(suggestions)


AUTOCOMPLETE = Projector(
    capability=autocomplete.CAPABILITY,
    tables=("keyword",),
    apply=apply_autocomplete,
)
