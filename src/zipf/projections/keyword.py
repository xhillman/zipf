"""What a paid response says about a keyword.

Several capabilities write the ``keyword`` table, and they know different things.
Autocomplete discovers terms but has no volume; Labs pays for volume but
discovers nothing. The conflict rule differs accordingly:

- **Discovery inserts, never overwrites.** ``ON CONFLICT DO NOTHING`` keeps a
  free suggestion from erasing a volume that was paid for.
- **Paid measurement overwrites.** ``ON CONFLICT DO UPDATE`` lets a newer Labs
  response replace an older one, because it is the authoritative source for the
  columns it sets.

Replay is ordered by ``fetched_at``, so within the paid sources the most recent
measurement wins.

The ``keyword_month`` write lives here too, in ``upsert_months``. One response
feeds both tables — every Labs endpoint returning ``keyword_info`` carries a
twelve-month series inside it — and a response is projected exactly once, so the
two writes belong to the same module rather than to two that import each other.
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

#: Volume, cpc and competition overwrite: the response setting them is the
#: authoritative source for what it measured, and replay is ordered by
#: ``fetched_at`` so the newest measurement wins.
#:
#: Difficulty and intent are merged rather than overwritten. Not every response
#: carrying volume also carries them — an older stored pull may predate the
#: fields, and a keyword the vendor knows little about can arrive without
#: ``keyword_properties`` at all. Overwriting would let one such response erase a
#: figure a previous call did report, so the rule is last-non-null-wins, which is
#: still deterministic under an ordered replay.
_UPSERT_MEASURED = """
INSERT INTO keyword (
  keyword, volume, cpc, competition, difficulty, intent, intent_probability, updated_at, raw_id
)
VALUES (
  :keyword, :volume, :cpc, :competition, :difficulty, :intent, :intent_probability,
  :updated_at, :raw_id
)
ON CONFLICT (keyword) DO UPDATE SET
  volume      = excluded.volume,
  cpc         = excluded.cpc,
  competition = excluded.competition,
  difficulty  = COALESCE(excluded.difficulty, keyword.difficulty),
  -- The label and its confidence move together. A response that reports an
  -- intent without a probability must not leave the previous call's confidence
  -- attached to a label it was never measured against.
  intent      = COALESCE(excluded.intent, keyword.intent),
  intent_probability = CASE
    WHEN excluded.intent IS NOT NULL THEN excluded.intent_probability
    ELSE keyword.intent_probability
  END,
  updated_at  = excluded.updated_at,
  raw_id      = excluded.raw_id
"""

#: One row per keyword per month. Written from any capability whose response
#: carries a monthly series, not only the discovery endpoint that sells one:
#: ``keyword_overview`` and both rank endpoints return twelve months inside
#: ``keyword_info``, already paid for.
_UPSERT_MONTH = """
INSERT INTO keyword_month (keyword, year, month, volume, raw_id)
VALUES (:keyword, :year, :month, :volume, :raw_id)
ON CONFLICT (keyword, year, month) DO UPDATE SET
  volume = excluded.volume,
  raw_id = excluded.raw_id
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
    """Write paid keyword data, keyed on keyword.

    Shared by every capability that returns a keyword's measured facts. Records
    without a keyword are skipped rather than written as a null primary key, and
    a record that omits an attribute leaves the stored one alone.
    """
    payload = [
        {
            "keyword": record["keyword"],
            "volume": record.get("volume"),
            "cpc": record.get("cpc"),
            "competition": record.get("competition"),
            "difficulty": record.get("difficulty"),
            "intent": record.get("intent"),
            "intent_probability": record.get("intent_probability"),
            "updated_at": row["fetched_at"],
            "raw_id": row["id"],
        }
        for record in records
        if record.get("keyword")
    ]
    conn.executemany(_UPSERT_MEASURED, payload)
    return len(payload)


def upsert_months(
    conn: sqlite3.Connection, row: sqlite3.Row, records: Sequence[Mapping[str, Any]]
) -> int:
    """Write each keyword's monthly volume series. Returns the month rows written.

    Lives here beside ``upsert_measured`` because the same paid response feeds
    both tables, and a response is projected once. Keeping the two writes in one
    module is what lets every capability carrying a series reach it without the
    projectors importing each other.
    """
    payload = [
        {
            "keyword": record["keyword"],
            "year": entry["year"],
            "month": entry["month"],
            "volume": entry["volume"],
            "raw_id": row["id"],
        }
        for record in records
        if record.get("keyword")
        for entry in record.get("monthly") or []
    ]
    conn.executemany(_UPSERT_MONTH, payload)
    return len(payload)


def apply_search_volume(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    """Write volume, the attributes that came with it, and the monthly series.

    All three are in the same response and were paid for together. Keeping only
    the volume meant buying difficulty and intent a second time from the
    endpoints that sell them alone.
    """
    records = labs.parse_search_volume(row["body"], {})
    upsert_months(conn, row, records)
    return upsert_measured(conn, row, records)


AUTOCOMPLETE = Projector(
    capability=autocomplete.CAPABILITY,
    tables=("keyword",),
    apply=apply_autocomplete,
)

SEARCH_VOLUME = Projector(
    capability=labs.SEARCH_VOLUME,
    tables=("keyword", "keyword_month"),
    apply=apply_search_volume,
)


#: Attribute upserts touch their own column and nothing else.
#:
#: On conflict they deliberately leave ``updated_at`` and ``raw_id`` alone. Those
#: two record *which paid measurement* a keyword's volume came from, and
#: ``fresh_keywords`` joins them against the measuring capabilities to decide
#: whether volume needs buying. Repointing ``raw_id`` at a difficulty response
#: would make an already-measured keyword look unmeasured, and the next
#: ``zipf vol`` would buy its volume a second time.
_UPSERT_DIFFICULTY = """
INSERT INTO keyword (keyword, difficulty, updated_at, raw_id)
VALUES (:keyword, :difficulty, :updated_at, :raw_id)
ON CONFLICT (keyword) DO UPDATE SET difficulty = excluded.difficulty
"""

_UPSERT_INTENT = """
INSERT INTO keyword (keyword, intent, intent_probability, updated_at, raw_id)
VALUES (:keyword, :intent, :intent_probability, :updated_at, :raw_id)
ON CONFLICT (keyword) DO UPDATE SET
  intent             = excluded.intent,
  intent_probability = excluded.intent_probability
"""


def _upsert_attribute(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    records: Sequence[Mapping[str, Any]],
    statement: str,
    fields: Sequence[str],
) -> int:
    payload = [
        {
            "keyword": record["keyword"],
            **{field: record.get(field) for field in fields},
            "updated_at": row["fetched_at"],
            "raw_id": row["id"],
        }
        for record in records
        if record.get("keyword")
    ]
    conn.executemany(statement, payload)
    return len(payload)


def apply_bulk_keyword_difficulty(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    return _upsert_attribute(
        conn,
        row,
        labs.parse_bulk_keyword_difficulty(row["body"], {}),
        _UPSERT_DIFFICULTY,
        ("difficulty",),
    )


def apply_search_intent(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    return _upsert_attribute(
        conn,
        row,
        labs.parse_search_intent(row["body"], {}),
        _UPSERT_INTENT,
        ("intent", "intent_probability"),
    )


BULK_KEYWORD_DIFFICULTY = Projector(
    capability=labs.BULK_KEYWORD_DIFFICULTY,
    tables=("keyword",),
    apply=apply_bulk_keyword_difficulty,
)

SEARCH_INTENT = Projector(
    capability=labs.SEARCH_INTENT,
    tables=("keyword",),
    apply=apply_search_intent,
)
