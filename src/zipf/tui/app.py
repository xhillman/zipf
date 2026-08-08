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
from typing import ClassVar, Final

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import DataTable, Footer, Input, Static

from zipf import watchlist
from zipf.budget import Budget
from zipf.errors import ZipfError
from zipf.format import plural
from zipf.jobs import queue as job_queue
from zipf.jobs.runner import JobRunner
from zipf.pricing import PriceEstimate
from zipf.services import browse
from zipf.services import budget as budget_service
from zipf.tui import commands, views
from zipf.tui.confirm import ConfirmModal
from zipf.tui.theme import ZIPF_THEME
from zipf.tui.views import TableSpec, View

#: What fraction of the table the identifying column may take, and the floor it
#: never drops below. A third leaves two thirds for the figures; the floor keeps
#: the column readable in a narrow terminal, where truncating to nine characters
#: would make every row look the same.
_KEY_COLUMN_SHARE: Final = 3
_KEY_COLUMN_MIN: Final = 18


class JobActivity(Message):
    """The runner reported something.

    Posted rather than calling into the widgets directly: the runner is a worker
    task, and routing its output through the message queue keeps every UI update
    on the app's own handling path.
    """

    def __init__(self, note: str) -> None:
        self.note = note
        super().__init__()


class ZipfApp(App[None]):
    """Browses the local cache. Opens free, stays free until a command is typed."""

    CSS_PATH = "zipf.tcss"
    TITLE = "zipf"
    # Below eighty columns the inspector would take at least a third of the
    # workspace, leaving too little room for the table's figures. Textual puts
    # exactly one of these classes on the screen as the terminal is resized.
    # Textual requires a class-level list; annotating the override as ClassVar
    # makes mypy reject the framework's inherited instance-variable type.
    HORIZONTAL_BREAKPOINTS = [  # noqa: RUF012
        (0, "-compact"),
        (80, "-standard"),
    ]
    # Every binding is declared, and `_refresh_footer` decides which are worth
    # showing for the row you are on. Hiding a key outright would make it
    # undiscoverable; showing every key at once is a wall nobody reads.
    BINDINGS: ClassVar[list[BindingType]] = [
        # `priority` because the screen binds tab to focus-next and would
        # otherwise swallow it before the app sees it.
        Binding("tab", "next_section", "section", priority=True),
        Binding("1", "explore_all", "all", show=False),
        Binding("2", "explore_watchlist", "watchlist", show=False),
        Binding("space", "mark", "mark"),
        Binding("w", "watch", "watch"),
        Binding("p", "source", "source"),
        Binding("s", "sort", "sort"),
        Binding("d", "density", "sources"),
        Binding("slash", "filter", "filter"),
        Binding("colon", "command", "command"),
        Binding("r", "reload", "refresh", show=False),
        Binding("escape", "escape", "back"),
        Binding("q", "quit", "quit"),
    ]

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """Make the palette's names known to the stylesheet parser.

        ``zipf.tcss`` is read during startup, before ``on_mount`` can register
        the theme, so without this every custom palette variable is an undefined
        variable at parse time and the app refuses to start.
        """
        return dict(ZIPF_THEME.variables)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Which keys the footer offers for whatever is on screen.

        Marking is meaningless in the ledger and sorting is meaningless in a gap
        pull already ordered by opportunity, so neither is offered there. This is
        the contextual footer, done with Textual's own mechanism rather than a
        hand-drawn line.

        Returning ``None`` hides *and disables* a binding, which is why ``escape``
        is never gated: it closes whichever bar is open, and a key that stops
        working once there is nothing to go back to would strand you in the
        command bar. Everything gated here is something the footer can warn you
        about before you press it, which beats explaining it afterwards.
        """
        if action in {"mark", "source"}:
            return True if self._spec.key_kind == views.KEYWORD_KEY else None
        if action == "watch":
            has_target = bool(self._marked or self._spec.keys)
            return True if self._spec.key_kind == views.KEYWORD_KEY and has_target else None
        if action in {"explore_all", "explore_watchlist"}:
            return True if self._section == views.SECTIONS[0] else None
        if action == "sort":
            return True if views.SORT_CYCLES.get(self._view.kind) else None
        return True

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        write_conn: sqlite3.Connection | None = None,
        budget: Budget | None = None,
        own_domain: str | None = None,
    ) -> None:
        super().__init__()
        self._conn = conn
        # Without a write handle the app is a reader: every `:` command that
        # would enqueue or fetch refuses rather than failing inside SQLite.
        self._write_conn = write_conn
        # Defaults keep the app constructible without a config file, which is
        # what makes it testable and what makes a first run on an empty database
        # show a screen rather than an error.
        self._budget = budget or Budget(ceiling_usd=0.0, threshold_usd=0.0)
        self._own_domain = own_domain
        self._spec = views.TableSpec(columns=(), rows=[], caption="", keys=[])
        # The three things that decide what is on screen. Held together because
        # every one of filter, sort and reload has to rebuild from all three.
        self._view = View(views.KEYWORDS)
        self._contains: str | None = None
        self._sort: str | None = None
        # Marked rows survive a change of view. A mark is a decision about a
        # keyword, not about the table it happened to be visible in, and losing
        # it on navigation would make marking useless for gathering a batch.
        self._marked: set[str] = set()
        # Whether to show where each figure came from. Off by default: the PRD
        # asks for silence, and `raw_id` is only interesting once you doubt
        # something.
        self._forensic = False
        # Where `escape` goes back to, for provenance and drilling.
        self._back: list[View] = []
        # The keyword column's last computed ceiling, so a resize that does
        # not change it does not rebuild the table.
        self._key_width: int | None = None
        # Which section of the interface is selected. Only the title reads it
        # today — the sections other than `explore` have nothing behind them
        # yet, and cycling to one deliberately leaves the view alone.
        self._section = views.SECTIONS[0]
        self._explore_mode = views.EXPLORE_ALL

    def compose(self) -> ComposeResult:
        # Where you are on the left, what you have and what is left on the
        # right. Two widgets so each can align to its own edge.
        with Horizontal(id="status"):
            yield Static(id="brand")
            yield Static(id="sections")
            yield Static(id="balance")
        with Horizontal(id="explore-bar"):
            yield Static(id="explore-options")
        with Horizontal(id="body"), Vertical(id="main"):
            # Two widgets rather than one line of markup, so the hint's
            # spend colour comes from the stylesheet that already defines it
            # rather than from a second copy of the value in Python.
            with Horizontal(id="head"):
                yield Static(id="question")
                yield Static(id="hint")
            # The table and the inspector side by side. The inspector is
            # about one row, so it belongs beside the rows rather than under
            # them, where it competed with the table for vertical space and
            # got six lines to say everything.
            with Horizontal(id="workspace"):
                yield DataTable(id="rows", cursor_type="row")
                with Vertical(id="inspector"):
                    yield Static(id="detail")
                    yield Static(id="jobs")
            yield Static(id="plan")
            yield Input(placeholder="filter", id="filter")
            # The headline rides on the same row as the thing being typed,
            # so the price and the command are read together rather than
            # asking the eye to leave the line it is working on.
            with Horizontal(id="command-row"):
                yield Input(placeholder=":command", id="command")
                yield Static(id="verdict")
        yield Footer()

    async def on_mount(self) -> None:
        # Registered before anything renders, so no widget is ever painted
        # in the stock theme first.
        self.register_theme(ZIPF_THEME)
        self.theme = ZIPF_THEME.name
        self.query_one("#filter", Input).display = False
        self.query_one("#command-row", Horizontal).display = False
        self.query_one("#plan", Static).display = False
        self.show_view(View(views.KEYWORDS))
        self._refresh_jobs()
        await self.refresh_status()
        self._start_runner()

    def _start_runner(self) -> None:
        """Host the job runner in the background (spec D1).

        One runner class, two hosts: ``zipf jobs run`` runs it in the foreground,
        the app runs it here. Work already in the queue was approved when it was
        queued, so draining it needs no second consent — but it does need to be
        visible, which is what the jobs pane is for.

        A reader-only app has nothing to drain with and starts no runner.
        """
        if self._write_conn is None:
            return
        runner = JobRunner(self._write_conn, self._budget, on_event=self._post_job_activity)
        self.run_worker(runner.run_forever(), name="job-runner", exclusive=False)

    def _post_job_activity(self, note: str) -> None:
        self.post_message(JobActivity(note))

    # ------------------------------------------------------------------ table

    def show_view(self, view: View, *, remember: bool = False) -> None:
        """Switch to a new selection, dropping any filter and sort.

        Both are cleared deliberately. A sort key belongs to one view — ordering
        domains by ``volume`` means nothing — and a filter carried into a new
        table produces an empty screen whose cause is off-screen.

        ``remember`` records where you came from, so drilling and following a
        row's source can be reversed.
        """
        if remember:
            self._back.append(self._view)
        self._view = view
        self._contains = None
        self._sort = None
        self._hide_filter()
        self._reload_table()

    def _reload_table(self) -> None:
        """Rebuild the table from the current view, filter and sort.

        Columns are rebuilt rather than reused: two views rarely share a shape,
        and a stale column header over correct data is worse than a redraw.
        """
        spec = views.table_for(
            self._conn,
            self._view,
            own_domain=self._own_domain,
            contains=self._contains,
            sort=self._sort,
            marked=frozenset(self._marked),
            forensic=self._forensic,
            watchlisted_only=self._explore_mode == views.EXPLORE_WATCHLIST,
        )
        table = self.query_one("#rows", DataTable)
        # Which row the cursor was on, by identity rather than by position.
        # Rebuilding clears the table and sends the cursor back to the top, so
        # without this, marking a row would scroll you away from the row you
        # just marked — and sorting would lose your place for no reason.
        previous = (
            self._spec.keys[table.cursor_row] if table.cursor_row < len(self._spec.keys) else None
        )

        self._spec = spec
        table.clear(columns=True)
        key_width = self._key_column_width(spec)
        self._key_width = key_width
        for index, label in enumerate(spec.columns):
            table.add_column(label, width=key_width if index == 0 else None)
        for row in spec.rows:
            table.add_row(*row)

        cursor = spec.keys.index(previous) if previous in spec.keys else 0
        if cursor:
            table.move_cursor(row=cursor)

        self.sub_title = self._caption(spec)
        self._show_title()
        self._show_question(spec)
        self._show_detail(cursor)
        # The footer offers keys per view, so it has to be re-asked whenever the
        # view changes rather than only at mount.
        self.refresh_bindings()

    def _key_column_width(self, spec: TableSpec) -> int | None:
        """A ceiling for the keyword column, or ``None`` to size it to content.

        Only the identifying column holds free text of unbounded length — a
        keyword can run to forty characters and push every figure off the right
        edge, and the figures are what the table is for. A third of the width
        leaves two thirds for them.

        Returns ``None`` before the first layout, when the table has no size to
        take a third of. ``on_resize`` corrects it once there is one.
        """
        if spec.key_kind != views.KEYWORD_KEY:
            return None
        available = self.query_one("#rows", DataTable).size.width
        # Nothing to take a third of yet, or so little that a floor of eighteen
        # would be wider than the whole table. Either way the honest answer is to
        # let the column size itself rather than force an overflow.
        if available < _KEY_COLUMN_MIN * 2:
            return None
        return max(_KEY_COLUMN_MIN, available // _KEY_COLUMN_SHARE)

    def on_resize(self) -> None:
        """Re-take a third of a width that just changed.

        Deferred to after the next refresh because a ``Resize`` arrives *before*
        the new layout is applied — read the table here and it still reports its
        previous size, which on the first one is zero.
        """
        self.call_after_refresh(self._resize_key_column)

    def _resize_key_column(self) -> None:
        """Rebuild only when the third actually came out different.

        A resize event per column dragged would otherwise rebuild the table for
        every intermediate width.
        """
        if not self.is_running or self._key_width == self._key_column_width(self._spec):
            return
        self._reload_table()

    def _caption(self, spec: TableSpec) -> str:
        """The row count, plus whichever of filter, sort and marks is in force.

        State the user set must be visible: a filtered table that does not say it
        is filtered reads as data loss, and a mark that is off-screen is a spend
        waiting to surprise someone.
        """
        parts = [spec.caption]
        if self._contains:
            parts.append(f'matching "{self._contains}"')
        if self._sort:
            parts.append(f"by {self._sort}")
        # Only where the marks mean something. They survive a change of view, but
        # reporting "2 marked" over a table with nothing markable in it describes
        # state the screen cannot show you.
        if self._marked and spec.key_kind == views.KEYWORD_KEY:
            parts.append(f"{len(self._marked)} marked")
        return " · ".join(parts)

    def _show_title(self) -> None:
        """Redraw the title bar: where you are, and which section holds it."""
        self.query_one("#brand", Static).update(views.title_line(self._view, self._section))
        self.query_one("#sections", Static).update(views.section_blocks(self._section))
        explore_bar = self.query_one("#explore-bar", Horizontal)
        explore_bar.display = self._section == views.SECTIONS[0]
        self.query_one("#explore-options", Static).update(
            views.explore_mode_blocks(self._explore_mode)
        )

    def action_next_section(self) -> None:
        """Move to the next section.

        Changes the title and the selector and nothing else. The sections beyond
        ``explore`` have no views behind them yet, and moving the table to
        something unrelated would be a worse answer than leaving it where it is.
        """
        self._section = views.next_section(self._section)
        self._show_title()
        self.refresh_bindings()

    def _select_explore_mode(self, mode: str) -> None:
        """Open one of Explore's keyword collections."""
        if self._section != views.SECTIONS[0]:
            return
        if mode == self._explore_mode and self._view.kind == views.KEYWORDS:
            return
        self._explore_mode = mode
        self.show_view(View(views.KEYWORDS))

    def action_explore_all(self) -> None:
        self._select_explore_mode(views.EXPLORE_ALL)

    def action_explore_watchlist(self) -> None:
        self._select_explore_mode(views.EXPLORE_WATCHLIST)

    def _show_question(self, spec: TableSpec) -> None:
        """The question this view answers, and the command it makes typeable.

        The hint wears the spend register because every command a view hints at
        is one that can cost money — it is a price tag, not a tip.
        """
        self.query_one("#question", Static).update(
            f"[$bright bold]{escape(spec.question)}[/]  [$dim]{escape(self._caption(spec))}[/]"
        )
        self.query_one("#hint", Static).update(escape(spec.hint))

    def _show_detail(self, cursor_row: int) -> None:
        """Fill the detail pane for the highlighted row."""
        detail = self.query_one("#detail", Static)

        # A response's own facts belong to the view rather than to a row: the
        # rows are what it produced, and every one of them has the same source.
        if self._view.kind == views.RESPONSE and self._view.arg:
            stored = browse.response_detail(self._conn, int(self._view.arg))
            if stored is not None:
                detail.update(views.response_markup(stored))
                return

        if self._spec.is_empty or cursor_row >= len(self._spec.keys):
            detail.update(f"[$dim]{escape(self._spec.caption)}[/]")
            return

        key = self._spec.keys[cursor_row]
        if self._spec.key_kind != views.KEYWORD_KEY:
            detail.update(f"[$bright bold]{escape(key)}[/]\n[$dim]{escape(self._spec.caption)}[/]")
            return

        detail.update(views.detail_markup(browse.keyword_detail(self._conn, key), key))

    def _refresh_jobs(self) -> None:
        """Redraw the jobs pane from the queue.

        Read on the browsing handle: WAL means a fresh statement sees whatever
        the runner has committed, without the reader needing write access.
        """
        rows = job_queue.recent(self._conn, limit=4)
        self.query_one("#jobs", Static).update(views.jobs_markup(rows))

    async def refresh_status(self) -> None:
        """Redraw the persistent readout.

        Reads the *cached* vendor balance. The app opening must not make a
        network call, however free — the home screen is the cache, and a status
        bar that blocks on a vendor round-trip contradicts that.
        """
        state = await budget_service.status(self._conn, self._budget, refresh=False)
        self.query_one("#balance", Static).update(
            views.status_line(state, browse.keyword_count(self._conn))
        )

    # ---------------------------------------------------------------- actions

    def action_filter(self) -> None:
        """Open the filter bar. Filtering is local SQL, so it is free and instant."""
        bar = self.query_one("#filter", Input)
        bar.display = True
        bar.focus()

    def action_escape(self) -> None:
        """Back out of whichever bar is open, drop the filter, or go back.

        In that order, innermost first: escape should undo the most recent thing
        you did, and closing a bar you just opened is more recent than the
        navigation that preceded it.
        """
        if self.query_one("#command-row", Horizontal).display:
            self._hide_command()
            return
        if self._contains is not None or self.query_one("#filter", Input).display:
            self._contains = None
            self._hide_filter()
            self._reload_table()
            return
        if self._back:
            self.show_view(self._back.pop())

    def action_mark(self) -> None:
        """Mark or unmark the highlighted keyword.

        Marks are what a bare ``:vol`` reads. This is the whole of the selection
        model: the cursor is the selection, and a command with no arguments takes
        what is marked rather than inventing a selection language.
        """
        key = self._cursor_key()
        if key is None or self._spec.key_kind != views.KEYWORD_KEY:
            self.notify("only keyword rows can be marked", severity="information")
            return
        if key in self._marked:
            self._marked.discard(key)
        else:
            self._marked.add(key)
        self._reload_table()

    def action_watch(self) -> None:
        """Toggle the marked keywords, or the highlighted keyword when unmarked."""
        targets = self._watch_targets()
        if not targets:
            self.notify("no keyword to watch", severity="information")
            return

        try:
            watching = watchlist.toggle(self.write, targets)
        except ZipfError as exc:
            self.notify(exc.fix or exc.problem, title=exc.problem, severity="warning")
            return

        self._reload_table()
        count = plural(len(targets), "keyword")
        message = f"watching {count}" if watching else f"removed {count} from watchlist"
        self.notify(message, severity="information")

    def _watch_targets(self) -> frozenset[str]:
        if self._marked:
            return frozenset(self._marked)
        key = self._cursor_key()
        return frozenset((key,)) if key else frozenset()

    def action_source(self) -> None:
        """Open the response that produced the highlighted row.

        The ``raw_id`` edge walked backwards. It is on the row in the schema and
        nothing else in the interface follows it, which is why a keyword can show
        a volume without being able to say where the volume came from.
        """
        table = self.query_one("#rows", DataTable)
        raw_id = self._spec.source_of(table.cursor_row)
        if raw_id is None:
            self.notify("no stored response behind this row", severity="information")
            return
        self.show_view(View(views.RESPONSE, str(raw_id)), remember=True)

    def action_density(self) -> None:
        """Show or hide where each figure came from.

        Two levels rather than three. The middle one in most designs is the
        default with a label attached, and naming it invites picking it.
        """
        self._forensic = not self._forensic
        self._reload_table()
        self.notify("showing sources" if self._forensic else "sources hidden")

    def _cursor_key(self) -> str | None:
        cursor = self.query_one("#rows", DataTable).cursor_row
        if cursor >= len(self._spec.keys):
            return None
        return self._spec.keys[cursor]

    def _hide_filter(self) -> None:
        """Put the filter bar away and empty it.

        Hidden before cleared, in that order: assigning ``value`` posts an
        ``Input.Changed`` that would otherwise reload the table a second time
        with the same result. ``on_input_changed`` ignores a hidden bar.
        """
        bar = self.query_one("#filter", Input)
        bar.display = False
        bar.value = ""
        self.query_one("#rows", DataTable).focus()

    def action_sort(self) -> None:
        """Advance to this view's next sort key.

        Says so when a view has nothing worth sorting, rather than consuming the
        keypress silently and leaving you unsure whether the key registered.
        """
        following = views.next_sort(self._view.kind, self._sort)
        if following is None:
            self.notify("nothing to sort in this view", severity="information")
            return
        self._sort = following
        self._reload_table()

    def action_command(self) -> None:
        """Open the command bar.

        Spending is typed, never clicked (PRD §9.1). This bar is the only route
        to a paid action in the whole interface — which is exactly why it shows
        the bill while you type rather than only once you have committed.
        """
        self.query_one("#command-row", Horizontal).display = True
        bar = self.query_one("#command", Input)
        self._show_plan(bar.value)
        bar.focus()

    async def action_reload(self) -> None:
        """Re-read everything from the database. Free, and never touches the network.

        The vendor balance shown is the cached one. Refreshing it live writes a
        ``raw_response`` row, which the browsing connection cannot do — that
        arrives with the read-write handle the job runner brings.
        """
        self._reload_table()
        await self.refresh_status()

    # ----------------------------------------------------------------- events

    async def on_job_activity(self, event: JobActivity) -> None:
        """A job finished, failed, or was recovered: re-read everything it touched.

        The table is rebuilt because a completed pull is new rows, and the status
        bar because a completed pull is money spent. Both come from the database
        rather than from the event, so what is on screen is what was stored.

        A job can finish while the app is closing — the runner is cancelled, but
        a message it already posted still arrives. Redrawing widgets that are
        being torn down raises, so a shutting-down app takes the news and stops.
        The job itself is unaffected: it was recorded before the message was sent.
        """
        if not self.is_running:
            return
        self._refresh_jobs()
        self._reload_table()
        await self.refresh_status()
        self.notify(event.note, severity="information")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_detail(event.cursor_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter drills into a row that names a container."""
        if event.cursor_row >= len(self._spec.keys):
            return
        target = views.drill_target(self._view, self._spec.keys[event.cursor_row])
        if target is None:
            self.notify("nothing deeper to open for this row", severity="information")
            return
        self.show_view(target, remember=True)

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """Clicking a column header sorts by it, where that column is sortable."""
        column = str(event.label)
        key = views.sort_for_column(self._view.kind, column)
        if key is None:
            self.notify(f"cannot sort by {column}", severity="information")
            return
        self._sort = key
        self._reload_table()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter as you type, and price as you type.

        Every browse query is bounded and sub-millisecond, so re-querying per
        keystroke costs less than debouncing would. A hidden bar is being cleared
        programmatically and its event is not input the user gave.

        Commands are still not *run* as you type — but they are priced, which is
        a different thing and a free one. Every ``plan`` in ``services/`` is a
        pure read against the cache, so a half-typed ``:gap a.com`` can be
        described in full without anything being fetched or queued.
        """
        if event.input.id == "command":
            # The bar itself is always visible; its row is what gets hidden, so
            # that is what says whether this event is input the user gave.
            if self.query_one("#command-row", Horizontal).display:
                self._show_plan(event.value)
            return
        if event.input.id == "filter" and event.input.display:
            self._contains = event.value or None
            self._reload_table()

    def _show_plan(self, text: str) -> None:
        """Render what the typed command would do, without doing any of it.

        The register decides the colour, and only ``spend`` gets the reserved
        one. That is what makes the bar readable before it is read: a command
        that would cost money looks different from one that would not, while it
        is still being typed.
        """
        result = commands.preview(self, text)
        plan = self.query_one("#plan", Static)
        verdict = self.query_one("#verdict", Static)

        # User text reaches both of these — a keyword may contain brackets — so
        # everything derived from input is escaped before it becomes markup.
        # The register is a class, so the colour it resolves to stays in the
        # stylesheet next to the rule reserving it.
        verdict.classes = f"-{result.register}"
        verdict.update(escape(result.headline))

        plan.classes = f"-{result.register}"
        plan.update(
            "\n".join(f"[$dim]{label:<14}[/]{escape(value)}" for label, value in result.lines)
        )
        plan.display = bool(result.lines)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter applies the filter, or runs the command."""
        if event.input.id == "command":
            typed = event.value
            self._hide_command()
            if typed.strip():
                self.run_worker(self._run_command(typed), exclusive=True)
            return

        self.query_one("#filter", Input).display = False
        self.query_one("#rows", DataTable).focus()

    def _hide_command(self) -> None:
        """Put the command bar away, and the plan with it.

        Hidden before cleared, for the same reason the filter bar is: assigning
        ``value`` posts a ``Changed`` that would otherwise re-render the plan as
        an idle prompt on the way out.
        """
        self.query_one("#command-row", Horizontal).display = False
        bar = self.query_one("#command", Input)
        bar.value = ""
        self.query_one("#plan", Static).display = False
        self.query_one("#rows", DataTable).focus()

    # --------------------------------------------------------------- commands

    async def _run_command(self, text: str) -> None:
        """Run one typed command and apply whatever it reports.

        Runs in a worker so a command that reaches the network leaves the cache
        browsable while it waits. Expected failures become a message; anything
        else is left to propagate, because a bug should not be reported as
        ordinary user error.
        """
        try:
            outcome = await commands.execute(self, text)
        except ZipfError as exc:
            self.notify(exc.fix or exc.problem, title=exc.problem, severity="warning")
            return

        if outcome.clears_marks:
            self._marked.clear()
        if outcome.view is not None:
            self.show_view(outcome.view)
        if outcome.changed:
            self._reload_table()
            await self.refresh_status()
        self.notify(outcome.message, severity=outcome.severity)
        if outcome.exits:
            self.exit()

    # The app is its own command context: it already holds both connections, the
    # budget, and the only thing that can put a modal on screen.

    @property
    def read(self) -> sqlite3.Connection:
        return self._conn

    @property
    def write(self) -> sqlite3.Connection:
        if self._write_conn is None:
            raise ZipfError(
                "This copy of zipf was opened read-only.",
                fix="Start it with `zipf` rather than constructing it without a write handle.",
            )
        return self._write_conn

    @property
    def budget(self) -> Budget:
        return self._budget

    @property
    def own_domain(self) -> str | None:
        return self._own_domain

    @property
    def marked(self) -> frozenset[str]:
        """Rows the user has marked, which a command with no arguments reads."""
        return frozenset(self._marked)

    async def confirm(self, estimate: PriceEstimate, *, what: str, detail: str = "") -> bool:
        """Show the bill and wait for a keypress, when the amount warrants asking.

        Below the threshold nothing is asked — the same rule the CLI follows —
        but the price still reaches the readout, so a spend is never silent.
        """
        if estimate.is_free or not self._budget.needs_confirmation(estimate):
            return True

        modal = ConfirmModal(
            estimate, conn=self._conn, budget=self._budget, what=what, detail=detail
        )
        # Annotated rather than returned directly: the result type is inferred
        # from context, and returning into `bool` would solve it as `object`.
        approved: bool = await self.push_screen_wait(modal)
        return approved
