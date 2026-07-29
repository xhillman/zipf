"""Projector contract.

A projector turns one ``raw_response`` row into rows in one or more projection
tables. It is a pure derivation: it reads the stored bytes and writes derived
tables, and it never reads the network or mutates ``raw_response``.

Projectors must be **idempotent under replay**. Applying the same raw row twice
must leave the projection tables in the same state as applying it once, because
``rebuild`` replays everything and must be safe to run at any time.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

type Apply = Callable[[sqlite3.Connection, sqlite3.Row], int]


@dataclass(frozen=True)
class Projector:
    """How one capability's stored bytes become projection rows."""

    capability: str
    #: Tables this projector writes. ``rebuild`` clears exactly these.
    tables: tuple[str, ...]
    apply: Apply
