"""The ``gsc_query`` projection.

Search Console keeps its own table because its ``position`` is averaged over the
requested period rather than an integer rank at a moment (spec D3). Putting it in
``observation.position`` would make that column mean two different things, and
nothing in a query would reveal which one you were looking at.

``ON CONFLICT DO UPDATE`` makes replay idempotent: the primary key is
``(query, page, date)``, and re-projecting the same page overwrites with
identical values.
"""

from __future__ import annotations

import sqlite3

from zipf.projections.base import Projector
from zipf.sources import gsc

_UPSERT = """
INSERT INTO gsc_query (query, page, date, clicks, impressions, ctr, position, raw_id)
VALUES (:query, :page, :date, :clicks, :impressions, :ctr, :position, :raw_id)
ON CONFLICT (query, page, date) DO UPDATE SET
  clicks      = excluded.clicks,
  impressions = excluded.impressions,
  ctr         = excluded.ctr,
  position    = excluded.position,
  raw_id      = excluded.raw_id
"""


def apply_search_analytics(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    rows = gsc.parse(row["body"], {})
    conn.executemany(_UPSERT, [{**r, "raw_id": row["id"]} for r in rows])
    return len(rows)


SEARCH_ANALYTICS = Projector(
    capability=gsc.CAPABILITY,
    tables=("gsc_query",),
    apply=apply_search_analytics,
)
