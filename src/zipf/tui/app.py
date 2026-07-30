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

This file owns layout and events only. What belongs in the table for a given
selection lives in ``views``, which imports nothing from Textual and is tested
without a terminal.
"""

from __future__ import annotations

import sqlite3
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Static, Tree
from textual.widgets.tree import TreeNode

from zipf.budget import Budget
from zipf.services import browse
from zipf.services import budget as budget_service
from zipf.tui import views
from zipf.tui.views import View


class ZipfApp(App[None]):
    """Browses the local cache. Opens free, stays free until a command is typed."""

    CSS_PATH = "zipf.tcss"
    TITLE = "zipf"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "quit"),
    ]

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        budget: Budget | None = None,
        own_domain: str | None = None,
    ) -> None:
        super().__init__()
        self._conn = conn
        # Defaults keep the app constructible without a config file, which is
        # what makes it testable and what makes a first run on an empty database
        # show a screen rather than an error.
        self._budget = budget or Budget(ceiling_usd=0.0, threshold_usd=0.0)
        self._own_domain = own_domain
        self._spec = views.TableSpec(columns=(), rows=[], caption="", keys=[])

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Horizontal(id="body"):
            yield Tree("cache", id="sidebar")
            with Vertical(id="main"):
                yield DataTable(id="rows", cursor_type="row", zebra_stripes=True)
                yield Static(id="detail")
        yield Footer()

    async def on_mount(self) -> None:
        self._build_sidebar()
        self.show_view(View(views.KEYWORDS))
        await self.refresh_status()

    # ---------------------------------------------------------------- sidebar

    def _build_sidebar(self) -> None:
        """One node per bucket, labelled with what it holds.

        Counts go in the label rather than a separate column because the sidebar
        is narrow and a bucket with nothing in it is the single most useful thing
        the tree can tell you at a glance.
        """
        totals = browse.counts(self._conn)
        tree: Tree[View] = self.query_one("#sidebar", Tree)
        tree.root.data = View(views.KEYWORDS)
        tree.root.set_label(f"cache  {totals.keywords:,}")

        domains = tree.root.add(f"domains  {totals.domains}", data=View(views.DOMAINS))
        for row in browse.domains(self._conn):
            domains.add_leaf(
                f"{row['domain']}  {row['keywords']:,}",
                data=View(views.DOMAIN, row["domain"]),
            )

        gaps = tree.root.add(f"gaps  {totals.gap_pairs}", data=View(views.GAPS))
        for pair in browse.gap_pairs(self._conn):
            gaps.add_leaf(
                f"{pair['competitor']}",
                data=View(views.GAP, f"{pair['competitor']}|{pair['mine']}"),
            )

        tree.root.add_leaf(f"gsc  {totals.gsc_queries:,}", data=View(views.GSC))
        tree.root.add_leaf(f"visibility  {totals.observations}", data=View(views.VISIBILITY))
        tree.root.expand()

    # ------------------------------------------------------------------ table

    def show_view(self, view: View) -> None:
        """Replace the table with one sidebar selection.

        Columns are rebuilt rather than reused: two views rarely share a shape,
        and a stale column header over correct data is worse than a redraw.
        """
        spec = views.table_for(self._conn, view, own_domain=self._own_domain)
        self._spec = spec

        table = self.query_one("#rows", DataTable)
        table.clear(columns=True)
        table.add_columns(*spec.columns)
        for row in spec.rows:
            table.add_row(*row)

        self.sub_title = spec.caption
        self._show_detail(0)

    def _show_detail(self, cursor_row: int) -> None:
        """Fill the detail pane for the highlighted row."""
        detail = self.query_one("#detail", Static)
        if self._spec.is_empty or cursor_row >= len(self._spec.keys):
            detail.update(f"[dim]{self._spec.caption}[/]")
            return

        key = self._spec.keys[cursor_row]
        if self._spec.key_kind != views.KEYWORD_KEY:
            detail.update(f"[bold]{key}[/]\n[dim]{self._spec.caption}[/]")
            return

        detail.update(views.detail_markup(browse.keyword_detail(self._conn, key), key))

    async def refresh_status(self) -> None:
        """Redraw the persistent readout.

        Reads the *cached* vendor balance. The app opening must not make a
        network call, however free — the home screen is the cache, and a status
        bar that blocks on a vendor round-trip contradicts that.
        """
        state = await budget_service.status(self._conn, self._budget, refresh=False)
        self.query_one("#status", Static).update(
            views.status_line(state, browse.counts(self._conn))
        )

    # ----------------------------------------------------------------- events

    def on_tree_node_selected(self, event: Tree.NodeSelected[View]) -> None:
        node: TreeNode[View] = event.node
        if node.data is not None:
            self.show_view(node.data)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_detail(event.cursor_row)
