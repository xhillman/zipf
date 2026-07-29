"""The ``keyword`` projection.

Four capabilities write this table, and they know different things. Autocomplete
discovers terms but has no volume; Labs pays for volume but discovers nothing.
The conflict rule differs accordingly:

- **Discovery inserts, never overwrites.** ``ON CONFLICT DO NOTHING`` keeps a
  free suggestion from erasing a volume that was paid for.
- **Paid measurement overwrites.** ``ON CONFLICT DO UPDATE`` lets a newer Labs
  response replace an older one, because it is the authoritative source for the
  columns it sets.

Replay is ordered by ``fetched_at``, so within the paid sources the most recent
measurement wins.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from zipf.projections.base import Projector
from zipf.sources import autocomplete
from zipf.sources.dataforseo import labs

_INSERT_DISCOVERED = """
INSERT INTO keyword (keyword, updated_at, raw_id)
VALUES (?, ?, ?)
ON CONFLICT (keyword) DO NOTHING
"""

_UPSERT_MEASURED = """
INSERT INTO keyword (keyword, volume, cpc, competition, updated_at, raw_id)
VALUES (:keyword, :volume, :cpc, :competition, :updated_at, :raw_id)
ON CONFLICT (keyword) DO UPDATE SET
  volume      = excluded.volume,
  cpc         = excluded.cpc,
  competition = excluded.competition,
  updated_at  = excluded.updated_at,
  raw_id      = excluded.raw_id
"""


def apply_autocomplete(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    """Record every suggested term as a known keyword."""
    suggestions = autocomplete.parse(row["body"], {})
    conn.executemany(
        _INSERT_DISCOVERED,
        [(term, row["fetched_at"], row["id"]) for term in suggestions],
    )
    return len(suggestions)


def upsert_measured(
    conn: sqlite3.Connection, row: sqlite3.Row, records: Sequence[Mapping[str, Any]]
) -> int:
    """Write paid volume data, keyed on keyword.

    Shared by every Labs capability that returns ``keyword_info``. Records
    without a keyword are skipped rather than written as a null primary key.
    """
    payload = [
        {
            "keyword": record["keyword"],
            "volume": record.get("volume"),
            "cpc": record.get("cpc"),
            "competition": record.get("competition"),
            "updated_at": row["fetched_at"],
            "raw_id": row["id"],
        }
        for record in records
        if record.get("keyword")
    ]
    conn.executemany(_UPSERT_MEASURED, payload)
    return len(payload)


def apply_search_volume(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    return upsert_measured(conn, row, labs.parse_search_volume(row["body"], {}))


AUTOCOMPLETE = Projector(
    capability=autocomplete.CAPABILITY,
    tables=("keyword",),
    apply=apply_autocomplete,
)

SEARCH_VOLUME = Projector(
    capability=labs.SEARCH_VOLUME,
    tables=("keyword",),
    apply=apply_search_volume,
)
