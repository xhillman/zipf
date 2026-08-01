"""The ``keyword_month`` projection for the Google Ads discovery endpoint.

``keywords_for_keywords`` returns a monthly series and the current volume in one
response, so both are written here: ``rebuild`` keys projectors by capability and
one response gets one projector — the same arrangement ``domain_keyword`` uses
for the Labs endpoints that return ranks and volume together.

The writes themselves live in ``projections.keyword``, beside the ``keyword``
upsert they accompany. Every Labs endpoint returning ``keyword_info`` carries a
twelve-month series too, so this is no longer the only source of one, and putting
the write here would mean those projectors importing this module for it.

Idempotent under replay, as every projector must be: the primary key is
``(keyword, year, month)`` and the write is an upsert, so replaying a response
overwrites its own rows rather than accumulating duplicates.
"""

from __future__ import annotations

import sqlite3

from zipf.projections.base import Projector
from zipf.projections.keyword import upsert_measured, upsert_months
from zipf.sources.dataforseo import keywords_data


def apply_keywords_for_keywords(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    """Write the monthly series, and the current volume it came with.

    The volume was paid for in the same call, so discarding it would mean buying
    it again from Labs to learn something already stored in these bytes.
    """
    records = keywords_data.parse(row["body"], {})
    months = upsert_months(conn, row, records)
    upsert_measured(conn, row, records)
    return months


KEYWORDS_FOR_KEYWORDS = Projector(
    capability=keywords_data.CAPABILITY,
    tables=("keyword", "keyword_month"),
    apply=apply_keywords_for_keywords,
)
