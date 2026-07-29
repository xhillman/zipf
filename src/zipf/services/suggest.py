"""Keyword suggestions. Tier 0, free.

Service functions are the one place both the CLI and the TUI call into. Neither
interface owns logic; both are shells over this layer.

Expansion is a loop over many single-seed fetches rather than one batched call.
Each expanded seed is therefore its own cache entry with its own TTL, so a
repeated expansion re-requests only the seeds that actually went stale.
"""

from __future__ import annotations

import asyncio
import sqlite3
import string
from collections.abc import Sequence
from dataclasses import dataclass, field

from zipf.budget import Budget
from zipf.fetch import fetch
from zipf.sources import autocomplete

#: Prefixes that turn a seed into question-shaped queries.
QUESTION_WORDS: tuple[str, ...] = (
    "how",
    "what",
    "why",
    "when",
    "where",
    "which",
    "who",
    "is",
    "can",
    "does",
)

#: Bound on one expansion. Alphabet expansion is 26 seeds, questions are 10.
#: Both are free, but an unbounded loop over user input is still a loop over
#: user input.
MAX_SEEDS: int = 64


@dataclass(frozen=True)
class SuggestResult:
    seed: str
    suggestions: list[str]
    cached: bool
    age_days: float | None


@dataclass(frozen=True)
class ExpansionResult:
    """One expansion run across many seeds."""

    seed: str
    results: list[SuggestResult] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def suggestions(self) -> list[str]:
        """Every distinct suggestion found, in stable order."""
        seen: dict[str, None] = {}
        for result in self.results:
            for term in result.suggestions:
                seen.setdefault(term, None)
        return list(seen)

    @property
    def cache_hits(self) -> int:
        return sum(1 for r in self.results if r.cached)


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


def expansion_seeds(seed: str, *, questions: bool = False, alphabet: bool = False) -> list[str]:
    """Build the seed list for an expansion, bounded by ``MAX_SEEDS``."""
    seeds = [seed]
    if questions:
        seeds.extend(f"{word} {seed}" for word in QUESTION_WORDS)
    if alphabet:
        seeds.extend(f"{seed} {letter}" for letter in string.ascii_lowercase)

    deduped = list(dict.fromkeys(seeds))
    return deduped[:MAX_SEEDS]


async def expand(
    conn: sqlite3.Connection,
    seed: str,
    *,
    budget: Budget,
    questions: bool = False,
    alphabet: bool = False,
    lang: str = "en",
    country: str = "us",
    force: bool = False,
) -> ExpansionResult:
    """Expand a seed across question and alphabet modifiers.

    Seeds are fetched sequentially. The bottleneck is a free endpoint's
    tolerance, not throughput, and one seed failing must not abort the rest:
    failures are collected and reported alongside the results.
    """
    seeds: Sequence[str] = expansion_seeds(seed, questions=questions, alphabet=alphabet)
    results: list[SuggestResult] = []
    failures: dict[str, str] = {}

    for one in seeds:
        try:
            results.append(
                await suggest(conn, one, budget=budget, lang=lang, country=country, force=force)
            )
        # One bad seed must not lose the rest; every failure is reported.
        except Exception as exc:
            failures[one] = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(0)  # yield so a TUI hosting this stays responsive

    return ExpansionResult(seed=seed, results=results, failures=failures)
