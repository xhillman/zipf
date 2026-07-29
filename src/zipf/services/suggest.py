"""Keyword suggestions. Tier 0, free.

Service functions are the one place both the CLI and the TUI call into. Neither
interface owns logic; both are shells over this layer.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from zipf.budget import Budget
from zipf.fetch import fetch
from zipf.sources import autocomplete


@dataclass(frozen=True)
class SuggestResult:
    seed: str
    suggestions: list[str]
    cached: bool
    age_days: float | None


async def suggest(
    conn: sqlite3.Connection,
    seed: str,
    *,
    budget: Budget,
    lang: str = "en",
    country: str = "us",
    force: bool = False,
) -> SuggestResult:
    """Return autocomplete suggestions for one seed."""
    result = await fetch(
        conn,
        autocomplete.CAPABILITY,
        {"seed": seed, "lang": lang, "country": country},
        budget=budget,
        force=force,
    )
    return SuggestResult(
        seed=seed,
        suggestions=result.parsed(),
        cached=result.cached,
        age_days=result.age.total_seconds() / 86400 if result.age else None,
    )
