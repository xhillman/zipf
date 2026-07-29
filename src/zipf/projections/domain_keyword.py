"""The ``domain_keyword`` projection: which domain ranks where, and when.

The primary key is ``(domain, keyword, observed_at)``, so the table accumulates a
rank history rather than overwriting one. ``observed_at`` is the response's
``fetched_at``, which makes replay idempotent: projecting the same stored
response twice targets the same key and writes the same values.

Both Labs capabilities that produce ranks feed this table. A domain intersection
also fills ``keyword``, because it returns volume alongside the positions and
that data is already paid for.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from zipf.projections.base import Projector
from zipf.projections.keyword import upsert_measured
from zipf.sources.dataforseo import labs

_UPSERT = """
INSERT INTO domain_keyword (domain, keyword, position, url, observed_at, raw_id)
VALUES (:domain, :keyword, :position, :url, :observed_at, :raw_id)
ON CONFLICT (domain, keyword, observed_at) DO UPDATE SET
  position = excluded.position,
  url      = excluded.url,
  raw_id   = excluded.raw_id
"""


def _params(row: sqlite3.Row) -> dict[str, Any]:
    """The normalised params that produced this response.

    Domain names are not recoverable from an intersection body, which reports
    two positions per keyword without naming either domain.
    """
    parsed: dict[str, Any] = json.loads(row["params_json"])
    return parsed


def _write_ranks(
    conn: sqlite3.Connection, row: sqlite3.Row, domain: str, records: list[dict[str, Any]]
) -> int:
    payload = [
        {
            "domain": domain,
            "keyword": record["keyword"],
            "position": record.get("position"),
            "url": record.get("url"),
            "observed_at": row["fetched_at"],
            "raw_id": row["id"],
        }
        for record in records
        if record.get("keyword") and record.get("position") is not None
    ]
    conn.executemany(_UPSERT, payload)
    return len(payload)


def apply_ranked_keywords(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    records = labs.parse_ranked_keywords(row["body"], {})
    domain = str(_params(row).get("domain", ""))
    if not domain:
        return 0

    written = _write_ranks(conn, row, domain, records)
    # The response carries volume for each ranked keyword; it is already bought,
    # so it is projected rather than discarded.
    upsert_measured(conn, row, records)
    return written


def apply_domain_intersection(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    records = labs.parse_domain_intersection(row["body"], {})
    params = _params(row)

    written = 0
    for domain_key, position_key, url_key in (
        ("target1", "target1_position", "target1_url"),
        ("target2", "target2_position", "target2_url"),
    ):
        domain = str(params.get(domain_key, ""))
        if not domain:
            continue
        written += _write_ranks(
            conn,
            row,
            domain,
            [
                {
                    "keyword": record["keyword"],
                    "position": record.get(position_key),
                    "url": record.get(url_key),
                }
                for record in records
            ],
        )

    upsert_measured(conn, row, records)
    return written


RANKED_KEYWORDS = Projector(
    capability=labs.RANKED_KEYWORDS,
    tables=("domain_keyword", "keyword"),
    apply=apply_ranked_keywords,
)

DOMAIN_INTERSECTION = Projector(
    capability=labs.DOMAIN_INTERSECTION,
    tables=("domain_keyword", "keyword"),
    apply=apply_domain_intersection,
)
