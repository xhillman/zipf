"""The Textual application.

The home screen is the cache, so the app opens on data that is already paid for
and nothing reaches the network until a command is typed (PRD §9.1).

Two connections, for one reason each:

- **Browsing is read-only**, opened ``mode=ro`` so SQLite refuses a write no
  matter what the UI asks for. Sorting, filtering and drilling are local SQL
  against this handle and can neither cost money nor damage anything.
- **Spending is read-write**, and arrives with the job runner. A ``:`` command
  enqueues on that handle; the runner drains it.

Both are passed in already open rather than resolved here, so the caller
releases them on every exit path — including a crash inside the event loop.
"""

from __future__ import annotations

import sqlite3
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Footer, Header, Static

from zipf.format import plural
from zipf.projections.rebuild import count_rows


class ZipfApp(App[None]):
    """Browses the local cache. Opens free, stays free until a command is typed."""

    CSS_PATH = "zipf.tcss"
    TITLE = "zipf"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._stored_summary(), id="cache-summary")
        yield Footer()

    def _stored_summary(self) -> str:
        """What the cache holds, read on the read-only handle."""
        responses = count_rows(self._conn, "raw_response")
        keywords = count_rows(self._conn, "keyword")
        return f"{plural(keywords, 'keyword')} from {plural(responses, 'stored response')}"
