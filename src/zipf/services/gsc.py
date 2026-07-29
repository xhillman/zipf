"""Search Console import. Tier 0, free, refreshed greedily.

Paging is a loop over single-page fetches. Each page is its own cache entry, so
an import interrupted at page 7 resumes without re-reading pages 1 to 6, and a
re-run inside the 1-day TTL costs nothing and touches no network.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta

from zipf.budget import Budget
from zipf.clock import now
from zipf.fetch import fetch
from zipf.sources import gsc


@dataclass(frozen=True)
class ImportResult:
    site_url: str
    start_date: str
    end_date: str
    pages_read: int
    rows: int
    cache_hits: int
    truncated: bool = False
    projection_errors: list[str] = field(default_factory=list)


def date_range(days: int) -> tuple[str, str]:
    """Return the (start, end) dates for the trailing window.

    Search Console finalises data on a lag, so the window ends three days back.
    Asking for yesterday returns partial rows that would later change, and this
    projection has no mechanism to notice that they did.
    """
    end = now() - timedelta(days=3)
    start = end - timedelta(days=days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


async def import_queries(
    conn: sqlite3.Connection,
    *,
    site_url: str,
    budget: Budget,
    days: int = 90,
    force: bool = False,
) -> ImportResult:
    """Import search analytics rows, one page at a time."""
    start_date, end_date = date_range(days)

    pages_read = 0
    total_rows = 0
    cache_hits = 0
    projection_errors: list[str] = []
    truncated = True

    for page in range(gsc.MAX_PAGES):
        result = await fetch(
            conn,
            gsc.CAPABILITY,
            {
                "site_url": site_url,
                "start_date": start_date,
                "end_date": end_date,
                "row_limit": gsc.PAGE_SIZE,
                "start_row": page * gsc.PAGE_SIZE,
            },
            budget=budget,
            force=force,
        )
        pages_read += 1
        if result.cached:
            cache_hits += 1
        if result.projection_error:
            projection_errors.append(result.projection_error)

        rows = result.parsed()
        total_rows += len(rows)

        # A short page is the last page. This is the only stop condition the API
        # offers, which is why MAX_PAGES exists as a backstop.
        if len(rows) < gsc.PAGE_SIZE:
            truncated = False
            break

    return ImportResult(
        site_url=site_url,
        start_date=start_date,
        end_date=end_date,
        pages_read=pages_read,
        rows=total_rows,
        cache_hits=cache_hits,
        truncated=truncated,
        projection_errors=projection_errors,
    )
