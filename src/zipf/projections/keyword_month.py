"""The ``keyword_month`` projection: twelve months of volume per keyword.

``keywords_for_keywords`` is the only source of a monthly series today, and the
same response also carries the current volume, cpc and competition. Both are
written here, because ``rebuild`` keys projectors by capability and one response
gets one projector — the same arrangement ``domain_keyword`` uses for the Labs
endpoints that return ranks and volume together.

Idempotent under replay, as every projector must be: the primary key is
``(keyword, year, month)`` and the write is an upsert, so replaying a response
overwrites its own rows rather than accumulating duplicates.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from zipf.projections.base import Projector
from zipf.projections.keyword import upsert_measured
from zipf.sources.dataforseo import keywords_data

_UPSERT = """
INSERT INTO keyword_month (keyword, year, month, volume, raw_id)
VALUES (:keyword, :year, :month, :volume, :raw_id)
ON CONFLICT (keyword, year, month) DO UPDATE SET
  volume = excluded.volume,
  raw_id = excluded.raw_id
"""


def _month_rows(records: Sequence[Mapping[str, Any]], raw_id: int) -> Iterator[Mapping[str, Any]]:
    """Flatten each keyword's series into one row per month."""
    for record in records:
        keyword = record.get("keyword")
        if not keyword:
            continue
        for entry in record.get("monthly") or []:
            yield {
                "keyword": keyword,
                "year": entry["year"],
                "month": entry["month"],
                "volume": entry["volume"],
                "raw_id": raw_id,
            }


def apply_keywords_for_keywords(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    """Write the monthly series, and the current volume it came with.

    The volume was paid for in the same call, so discarding it would mean buying
    it again from Labs to learn something already stored in these bytes.
    """
    records = keywords_data.parse(row["body"], {})
    months = list(_month_rows(records, row["id"]))
    conn.executemany(_UPSERT, months)
    upsert_measured(conn, row, records)
    return len(months)


KEYWORDS_FOR_KEYWORDS = Projector(
    capability=keywords_data.CAPABILITY,
    tables=("keyword", "keyword_month"),
    apply=apply_keywords_for_keywords,
)
