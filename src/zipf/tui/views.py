"""What each application view puts in the table.

Deliberately free of Textual imports. A view is a pure function of the database
plus a selection, returning columns and cells, so the mapping is testable without
a terminal emulator and the app file stays about layout and events.

Cells carry their own alignment because Textual's ``DataTable`` has no per-column
justify. Values derived from stored data are wrapped in ``Text`` rather than left
as strings: ``DataTable`` runs bare strings through ``Text.from_markup``, so a
keyword like ``best crm [free]`` would otherwise be parsed as markup and lose
part of itself. Markup is used only for strings this module writes itself.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final

from rich.text import Text

from zipf import capabilities
from zipf.clock import as_days, from_iso, now
from zipf.format import ABSENT, meter, plural
from zipf.jobs.describe import job_subject
from zipf.services import browse, volume
from zipf.services import gap as gap_service
from zipf.services.budget import BudgetStatus
from zipf.sources.dataforseo import labs

#: The available application views.
KEYWORDS: Final = "keywords"
DOMAINS: Final = "domains"
DOMAIN: Final = "domain"
GAPS: Final = "gaps"
GAP: Final = "gap"
STALE: Final = "stale"
GSC: Final = "gsc"
VISIBILITY: Final = "visibility"
RESPONSES: Final = "responses"
RESPONSE: Final = "response"

#: What a table's row keys name, which decides what the detail pane can look up.
KEYWORD_KEY: Final = "keyword"
OPAQUE_KEY: Final = "opaque"
RESPONSE_KEY: Final = "response"

#: The question each view exists to answer, shown where a title would go.
#:
#: A bucket name tells you which table you are looking at, which you already
#: know — you just selected it. The question tells you what the table is for,
#: which is the part that is easy to lose halfway through a session.
QUESTIONS: Final[dict[str, str]] = {
    KEYWORDS: "Which keywords are worth pursuing?",
    DOMAINS: "Who else ranks in this space?",
    DOMAIN: "What does {arg} already rank for?",
    GAPS: "Where is the space I have not taken?",
    GAP: "What do they rank for that I do not?",
    STALE: "Is fresh data worth buying?",
    GSC: "What am I already getting clicks for?",
    VISIBILITY: "Where am I cited by answer engines?",
    RESPONSES: "Can I trust where this data came from?",
    RESPONSE: "What did response #{arg} produce?",
}

#: The three sections the interface is organised into. Only ``explore`` has views
#: behind it today; the other two are named so the shape is visible before the
#: work that fills them lands, and so the title can say which one you are in.
SECTIONS: Final[tuple[str, ...]] = ("explore", "research", "data")

#: The two keyword collections available inside Explore.
EXPLORE_ALL: Final = "all"
EXPLORE_WATCHLIST: Final = "watchlist"

#: What the title bar calls each view. Short, because it shares one line with
#: the balance — the question below it is where a view explains itself.
NAMES: Final[dict[str, str]] = {
    KEYWORDS: "keyword explorer",
    DOMAINS: "domains",
    DOMAIN: "domain · {arg}",
    GAPS: "gaps",
    GAP: "gap",
    STALE: "refresh planner",
    GSC: "search console",
    VISIBILITY: "visibility",
    RESPONSES: "acquisition ledger",
    RESPONSE: "response #{arg}",
}

#: Appended to the header of whichever column the table is sorted by. Stripped
#: again by ``sort_for_column``, so clicking a sorted header still resolves.
SORT_MARK: Final = " ↓"

#: Which sort keys each view offers, in the order pressing sort cycles through
#: them. Only views backed by a sortable query appear: a gap pull is already
#: ordered by opportunity, and re-sorting 50 clustered rows answers no question.
SORT_CYCLES: Final[dict[str, tuple[str, ...]]] = {
    KEYWORDS: tuple(browse.KEYWORD_SORTS),
    DOMAINS: tuple(browse.DOMAIN_SORTS),
    GSC: tuple(browse.GSC_SORTS),
    RESPONSES: tuple(browse.RESPONSE_SORTS),
}

#: Column header to sort key, for sorting by clicking a header. Absent headers
#: are not sortable — ``aio`` has one of three values and ``url`` is incidental.
SORT_BY_COLUMN: Final[dict[str, dict[str, str]]] = {
    KEYWORDS: {
        "keyword": "keyword",
        "volume": "volume",
        "intent": "intent",
        "difficulty": "difficulty",
        "cpc": "cpc",
    },
    DOMAINS: {"domain": "domain", "keywords": "keywords", "age": "observed"},
    GSC: {"query": "query", "clicks": "clicks", "impr": "impressions", "pos": "position"},
    RESPONSES: {"fetched": "fetched", "cost": "cost", "bytes": "bytes"},
}


def next_sort(kind: str, current: str | None) -> str | None:
    """The sort key after ``current`` for this view, wrapping around.

    Returns ``None`` for a view with nothing worth sorting, so the caller can say
    so rather than silently doing nothing.
    """
    cycle = SORT_CYCLES.get(kind)
    if not cycle:
        return None
    if current is None or current not in cycle:
        return cycle[0]
    return cycle[(cycle.index(current) + 1) % len(cycle)]


def sort_for_column(kind: str, column: str) -> str | None:
    """The sort key a column header maps to, or ``None`` if it is not sortable.

    Strips the sort marker first, so the column currently being sorted by is
    still clickable — otherwise sorting by a column once would make it inert.
    """
    return SORT_BY_COLUMN.get(kind, {}).get(column.removesuffix(SORT_MARK))


#: Which projected table each row of a response's detail opens. Values are view
#: kinds rather than ``View`` instances because this is read before the dataclass
#: below exists. Tables with no browsable view are absent rather than mapped to
#: something approximate.
_TABLE_VIEWS: Final[dict[str, str]] = {
    "keyword": KEYWORDS,
    "keyword_month": KEYWORDS,
    "domain_keyword": DOMAINS,
    "gsc_query": GSC,
    "observation": VISIBILITY,
}


def drill_target(view: View, key: str) -> View | None:
    """Where pressing enter on a row goes, or ``None`` when there is nowhere.

    Only rows that name a container have somewhere to go. A keyword row will
    open its SERP once that milestone lands; until then the honest answer is
    nothing, which the caller reports rather than silently ignoring.
    """
    if view.kind == DOMAINS:
        return View(DOMAIN, key)
    if view.kind == GAPS:
        return View(GAP, key)
    if view.kind == RESPONSES:
        return View(RESPONSE, key)
    if view.kind == RESPONSE:
        kind = _TABLE_VIEWS.get(key)
        return View(kind) if kind else None
    return None


#: How long a stored volume stays true. Used to decide whether an age is worth
#: showing at all: below the TTL the number is current, and printing "3d" next to
#: current data is noise the PRD explicitly rules out ("default to silence").
_VOLUME_TTL: Final = capabilities.get(labs.SEARCH_VOLUME).ttl


@dataclass(frozen=True)
class View:
    """A table selection: which view, and which member of it."""

    kind: str
    arg: str | None = None

    @property
    def key(self) -> str:
        return self.kind if self.arg is None else f"{self.kind}:{self.arg}"


@dataclass(frozen=True)
class TableSpec:
    """Everything the table widget needs, and nothing about the widget."""

    columns: tuple[str, ...]
    rows: list[tuple[Text | str, ...]]
    caption: str
    #: The value in each row that identifies it, for the detail pane. Parallel to
    #: ``rows`` rather than inside it, so a row's identity is not also a column.
    keys: list[str]
    #: What those keys name. The detail pane looks up a keyword but not a domain,
    #: and asking for the volume of "ahrefs.com" would report it as unpriced —
    #: an answer that is wrong rather than merely empty.
    key_kind: str = KEYWORD_KEY
    #: What this view is for. Shown where a bucket name would otherwise go.
    question: str = ""
    #: A command this view exists to make typeable, where it has one. Belongs to
    #: the view rather than to the highlighted row: a planner that names its
    #: command only when the table is empty names it exactly when it is useless.
    hint: str = ""
    #: The ``raw_id`` behind each row, parallel to ``keys``. Empty where the rows
    #: are not projections of a single response — a gap cluster collapses several
    #: — in which case the source key does nothing rather than guessing.
    source_ids: list[int | None] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.rows

    def source_of(self, index: int) -> int | None:
        """The response that produced row ``index``, if the row names just one."""
        if index >= len(self.source_ids):
            return None
        return self.source_ids[index]


def _right(value: str) -> Text:
    return Text(value, justify="right")


def _headers(kind: str, columns: tuple[str, ...], sort: str | None) -> tuple[str, ...]:
    """Column labels, with a marker on whichever one the table is ordered by.

    ``dev/notes.md`` asks for a "sorted by" indicator. It goes on the column
    rather than only in the caption because the column is where you look when
    you are wondering what the order means.
    """
    if sort is None:
        return columns
    sorted_column = next(
        (column for column, key in SORT_BY_COLUMN.get(kind, {}).items() if key == sort), None
    )
    return tuple(column + SORT_MARK if column == sorted_column else column for column in columns)


#: Two columns reserved in front of every markable row: the glyph, and a space.
#: Always present, so marking a row cannot shift the column beside it.
MARK: Final = "◆"
_GUTTER: Final = 2


def _key_cell(value: str, marked: frozenset[str]) -> Text:
    """A row's identifying cell, with a gutter saying whether it is marked.

    The gutter is reserved whether or not anything is marked, so the table does
    not reflow the moment you mark something. The glyph carries the state and
    bold reinforces it — in a table that already has zebra stripes and a cursor
    highlight, weight alone is too easy to miss.

    The value is still wrapped in ``Text`` rather than interpolated into markup,
    so a keyword containing brackets reaches the widget intact.
    """
    is_marked = value in marked
    cell = Text(f"{MARK if is_marked else ' '} ")
    cell.append(value, style="bold" if is_marked else "")
    return cell


#: What the forensic columns add. Kept together so the two places that build
#: them cannot disagree about the order.
_FORENSIC_COLUMNS: Final[tuple[str, ...]] = ("src", "fetched")


def _forensic_cells(raw_id: int | None, stored: str | None) -> tuple[Text, ...]:
    return (
        _right(f"#{raw_id}" if raw_id is not None else "—"),
        _right((stored or "")[:10]),
    )


def _stale_age(stored: str | None, ttl: timedelta = _VOLUME_TTL) -> str:
    """An age, but only once the data is past its TTL.

    Below the TTL this returns empty: the figure is current, and an age column
    full of "3d" trains you to ignore the column that is supposed to warn you.
    """
    if not stored:
        return ""
    age = now() - from_iso(stored)
    if age <= ttl:
        return ""
    return f"{int(age.total_seconds() // 86400)}d!"


def _aio(has_aio: int | None) -> str:
    """Whether an AI Overview was seen. Blank until the SERP milestone fills it."""
    if has_aio is None:
        return ""
    return "●" if has_aio else "○"


def _keyword_rows(
    rows: list[dict[str, Any]],
    *,
    sort: str | None = None,
    marked: frozenset[str] = frozenset(),
    forensic: bool = False,
) -> TableSpec:
    """The main keyword table: what it is worth, how old, and where you stand."""
    cells: list[tuple[Text | str, ...]] = []
    for row in rows:
        volume = f"{row['volume']:,}" if row["volume"] is not None else "—"
        difficulty = str(row["difficulty"]) if row["difficulty"] is not None else "—"
        cpc = f"${row['cpc']:.2f}" if row["cpc"] is not None else "—"
        cells.append(
            (
                _key_cell(row["keyword"], marked),
                _right(volume),
                Text(row["intent"] or "—"),
                _right(difficulty),
                _right(cpc),
                *(_forensic_cells(row["raw_id"], row["updated_at"]) if forensic else ()),
            )
        )
    columns = ("keyword", "volume", "intent", "difficulty", "cpc") + (
        _FORENSIC_COLUMNS if forensic else ()
    )
    return TableSpec(
        columns=_headers(KEYWORDS, columns, sort),
        rows=cells,
        caption=plural(len(rows), "keyword"),
        keys=[row["keyword"] for row in rows],
        question=QUESTIONS[KEYWORDS],
        source_ids=[row["raw_id"] for row in rows],
    )


def _domain_rows(rows: list[dict[str, Any]], *, sort: str | None = None) -> TableSpec:
    cells: list[tuple[Text | str, ...]] = [
        (
            Text(row["domain"]),
            _right(f"{row['keywords']:,}"),
            _right(str(row["best_position"]) if row["best_position"] is not None else "—"),
            _right(_stale_age(row["last_observed"], capabilities.get(labs.RANKED_KEYWORDS).ttl)),
        )
        for row in rows
    ]
    return TableSpec(
        columns=_headers(DOMAINS, ("domain", "keywords", "best", "age"), sort),
        rows=cells,
        caption=plural(len(rows), "domain"),
        keys=[row["domain"] for row in rows],
        key_kind=OPAQUE_KEY,
        question=QUESTIONS[DOMAINS],
    )


def _domain_keyword_rows(
    rows: list[dict[str, Any]], domain: str, *, marked: frozenset[str] = frozenset()
) -> TableSpec:
    cells: list[tuple[Text | str, ...]] = [
        (
            _key_cell(row["keyword"], marked),
            _right(str(row["position"]) if row["position"] is not None else "—"),
            _right(f"{row['volume']:,}" if row["volume"] is not None else "—"),
            Text((row["url"] or "").removeprefix("https://").removeprefix("http://")),
        )
        for row in rows
    ]
    return TableSpec(
        columns=("keyword", "pos", "vol", "url"),
        rows=cells,
        caption=f"{domain} · {plural(len(rows), 'keyword')}",
        keys=[row["keyword"] for row in rows],
        question=QUESTIONS[DOMAIN].format(arg=domain),
    )


def _gap_pair_rows(rows: list[dict[str, Any]]) -> TableSpec:
    cells: list[tuple[Text | str, ...]] = [
        (
            Text(f"{row['competitor']} vs {row['mine']}"),
            _right(_stale_age(row["last_pulled"], capabilities.get(labs.DOMAIN_INTERSECTION).ttl)),
            _right(str(row["pulls"])),
        )
        for row in rows
    ]
    return TableSpec(
        columns=("comparison", "age", "pulls"),
        rows=cells,
        caption=plural(len(rows), "gap pull"),
        keys=[f"{row['competitor']}|{row['mine']}" for row in rows],
        key_kind=OPAQUE_KEY,
        question=QUESTIONS[GAPS],
    )


def _gap_rows(
    conn: sqlite3.Connection,
    pair: str,
    contains: str | None,
    *,
    marked: frozenset[str] = frozenset(),
) -> TableSpec:
    """One gap pull, with restatements of the same query collapsed.

    Clustered rather than flat because that is what the CLI shows and what the
    number actually means: a hundred bought rows are often twenty real
    opportunities.

    Filtering happens here rather than in SQL because clustering happens after
    the query: a term matching a collapsed variant should still show the cluster
    that contains it. The list is capped at fifty, so the cost is nil.
    """
    competitor, _, mine = pair.partition("|")
    clusters = gap_service.read(conn, competitor, mine)
    if contains:
        term = contains.casefold()
        clusters = [
            cluster
            for cluster in clusters
            if term in cluster.keyword.casefold()
            or any(term in variant.casefold() for variant in cluster.variants)
        ]
    cells: list[tuple[Text | str, ...]] = [
        (
            _key_cell(cluster.keyword, marked),
            _right(f"{cluster.volume:,}" if cluster.volume is not None else "—"),
            _right(str(cluster.position) if cluster.position is not None else "—"),
            _right(f"+{cluster.variant_count - 1}" if cluster.has_variants else ""),
            Text((cluster.url or "").removeprefix("https://").removeprefix("http://")),
        )
        for cluster in clusters
    ]
    return TableSpec(
        columns=("keyword", "vol", "pos", "also", "their url"),
        rows=cells,
        caption=f"{competitor} ranks, {mine} does not · {plural(len(clusters), 'distinct query')}",
        keys=[cluster.keyword for cluster in clusters],
        question=f"What does {competitor} rank for that {mine} does not?",
    )


def _gsc_rows(rows: list[dict[str, Any]], *, sort: str | None = None) -> TableSpec:
    cells: list[tuple[Text | str, ...]] = [
        (
            Text(row["query"]),
            _right(f"{row['clicks']:,}"),
            _right(f"{row['impressions']:,}"),
            _right(f"{row['position']:.1f}"),
            _right(str(row["pages"])),
        )
        for row in rows
    ]
    return TableSpec(
        columns=_headers(GSC, ("query", "clicks", "impr", "pos", "pages"), sort),
        rows=cells,
        caption=plural(len(rows), "query"),
        keys=[row["query"] for row in rows],
        question=QUESTIONS[GSC],
    )


def _stale_rows(
    conn: sqlite3.Connection, contains: str | None, *, marked: frozenset[str] = frozenset()
) -> TableSpec:
    """The refresh planner: what has aged out, and what one batch would cost.

    A view rather than a control. It prices the work with the same ``plan`` the
    command would use and then names that command, which keeps the decision to
    spend where the PRD puts it — typed, not clicked (§9.1).
    """
    rows = browse.stale_keywords(conn, contains=contains)
    batch = volume.plan(conn, [row["keyword"] for row in rows], force=True)

    cells: list[tuple[Text | str, ...]] = [
        (
            _key_cell(row["keyword"], marked),
            _right(f"{row['volume']:,}" if row["volume"] is not None else "—"),
            _right(_stale_age(row["updated_at"])),
            _right((row["measured_at"] or "")[:10]),
            _right(f"#{row['raw_id']}" if row["raw_id"] is not None else "—"),
        )
        for row in rows
    ]

    if not rows:
        caption = f"everything measured is inside its {as_days(_VOLUME_TTL):.0f}d TTL"
    else:
        caption = (
            f"{plural(len(rows), 'keyword')} past the {as_days(_VOLUME_TTL):.0f}d TTL "
            f"· {plural(len(batch.batches), 'call')} · ~${batch.estimate.usd:.5f}"
        )

    return TableSpec(
        columns=("keyword", "vol", "age", "measured", "src"),
        rows=cells,
        caption=caption,
        keys=[row["keyword"] for row in rows],
        question=QUESTIONS[STALE],
        hint=f":vol --stale  ·  ~${batch.estimate.usd:.5f}" if rows else "",
        source_ids=[row["raw_id"] for row in rows],
    )


def _response_rows(rows: list[dict[str, Any]], *, sort: str | None = None) -> TableSpec:
    """The acquisition ledger: bought once, owned forever, and rebuildable."""
    cells: list[tuple[Text | str, ...]] = [
        (
            _right(f"#{row['id']}"),
            Text(row["capability"]),
            Text(_response_subject(row)),
            _right((row["fetched_at"] or "")[:10]),
            _right("free" if not row["cost_usd"] else f"${row['cost_usd']:.5f}"),
            _right(f"{row['bytes']:,}"),
        )
        for row in rows
    ]
    paid = sum(row["cost_usd"] for row in rows)
    columns = ("id", "capability", "subject", "fetched", "cost", "bytes")
    return TableSpec(
        columns=_headers(RESPONSES, columns, sort),
        rows=cells,
        caption=f"{plural(len(rows), 'stored response')} · ${paid:.5f} paid once",
        keys=[str(row["id"]) for row in rows],
        key_kind=RESPONSE_KEY,
        question=QUESTIONS[RESPONSES],
        source_ids=[row["id"] for row in rows],
    )


def _response_subject(row: dict[str, Any]) -> str:
    """What a stored response was about, read from the params it was fetched with.

    The capability alone is not enough: three gap pulls all read
    ``labs.domain_intersection`` and are otherwise indistinguishable, which is
    the same problem ``jobs list`` had before the usability pass.
    """
    try:
        params = json.loads(row["params_json"])
    except (TypeError, ValueError):
        return ""
    return job_subject(params)


def _response_detail_rows(conn: sqlite3.Connection, response_id: str) -> TableSpec:
    """One response and everything downstream of it: the ``raw_id`` edge forwards."""
    if not response_id.isdigit():
        return _empty(RESPONSE, "that is not a response id", arg=response_id)

    detail = browse.response_detail(conn, int(response_id))
    if detail is None:
        return _empty(RESPONSE, f"no response #{response_id} is stored", arg=response_id)

    cells: list[tuple[Text | str, ...]] = [
        (Text(project["table"]), _right(f"+{project['rows']:,}")) for project in detail["projects"]
    ]
    cost = "free" if not detail["cost_usd"] else f"${detail['cost_usd']:.5f}"
    caption = f"{detail['capability']} · {cost} · {detail['bytes']:,} bytes"
    return TableSpec(
        columns=("projected into", "rows"),
        rows=cells,
        caption=caption if cells else f"{caption} · read but not projected",
        keys=[project["table"] for project in detail["projects"]],
        key_kind=OPAQUE_KEY,
        question=QUESTIONS[RESPONSE].format(arg=response_id),
    )


def table_for(
    conn: sqlite3.Connection,
    view: View,
    *,
    own_domain: str | None = None,
    contains: str | None = None,
    sort: str | None = None,
    marked: frozenset[str] = frozenset(),
    forensic: bool = False,
    watchlisted_only: bool = False,
) -> TableSpec:
    """Build the table for one view selection.

    ``marked``, ``forensic``, and ``watchlisted_only`` are display state the app
    owns: which rows the user selected, whether to show where figures came from,
    and which Explore collection is active. They are passed in rather than read
    here so this stays testable without a terminal.
    """
    if view.kind == KEYWORDS:
        return _keyword_rows(
            browse.keywords(
                conn,
                contains=contains,
                sort=sort,
                own_domain=own_domain,
                watchlisted_only=watchlisted_only,
            ),
            sort=sort,
            marked=marked,
            forensic=forensic,
        )
    if view.kind == DOMAINS:
        return _domain_rows(browse.domains(conn, contains=contains, sort=sort), sort=sort)
    if view.kind == DOMAIN and view.arg:
        return _domain_keyword_rows(
            browse.domain_keywords(conn, view.arg, contains=contains), view.arg, marked=marked
        )
    if view.kind == GAPS:
        return _gap_pair_rows(browse.gap_pairs(conn, contains=contains))
    if view.kind == GAP and view.arg:
        return _gap_rows(conn, view.arg, contains, marked=marked)
    if view.kind == STALE:
        return _stale_rows(conn, contains, marked=marked)
    if view.kind == RESPONSES:
        return _response_rows(browse.responses(conn, contains=contains, sort=sort), sort=sort)
    if view.kind == RESPONSE and view.arg:
        return _response_detail_rows(conn, view.arg)
    if view.kind == GSC:
        return _gsc_rows(browse.gsc_queries(conn, contains=contains, sort=sort), sort=sort)
    return _visibility_placeholder()


def _empty(kind: str, caption: str, *, arg: str = "") -> TableSpec:
    """A view that has a question but nothing to answer it with."""
    return TableSpec(
        columns=("",),
        rows=[],
        caption=caption,
        keys=[],
        key_kind=OPAQUE_KEY,
        question=QUESTIONS.get(kind, "").format(arg=arg),
    )


def _visibility_placeholder() -> TableSpec:
    """Visibility has no rows until the LLM panel milestone.

    Shown as an empty bucket rather than hidden: an absent node reads as "this
    tool does not do that", which is a different and wrong message.
    """
    return TableSpec(
        columns=("subject", "surface", "rate"),
        rows=[],
        caption="nothing sampled yet",
        keys=[],
        key_kind=OPAQUE_KEY,
        question=QUESTIONS[VISIBILITY],
    )


def view_name(view: View) -> str:
    """What the title bar calls this view."""
    name = NAMES.get(view.kind, view.kind)
    return name.format(arg=view.arg) if view.arg else name.partition(" · {arg}")[0]


def next_section(current: str) -> str:
    """The section after this one, wrapping. Unknown input starts at the first."""
    if current not in SECTIONS:
        return SECTIONS[0]
    return SECTIONS[(SECTIONS.index(current) + 1) % len(SECTIONS)]


def section_blocks(active: str) -> str:
    """The section selector: three blocks, one of them filled.

    The active block uses the neutral panel and bright text. Every hue this
    interface uses already means something, so a selector must not borrow one.

    Every block is padded identically whether or not it is active, so the ones
    beside it do not shift along the bar as you cycle through them.
    """
    return " ".join(
        f"[$bright on $panel bold] {name} [/]" if name == active else f"[$dim] {name} [/]"
        for name in SECTIONS
    )


def explore_mode_blocks(active: str) -> str:
    """The numbered All/Watchlist selector shown inside Explore."""
    labels = ((EXPLORE_ALL, "1 All"), (EXPLORE_WATCHLIST, "2 Watchlist"))
    return " ".join(
        f"[$bright on $panel bold] {label} [/]" if mode == active else f"[$dim] {label} [/]"
        for mode, label in labels
    )


def title_line(view: View, section: str = SECTIONS[0]) -> str:
    """The left of the title bar: the tool, and where in it you are.

    Only ``explore`` has views behind it, so it names the view on screen. The
    other two name themselves, because naming a view they do not own yet would
    say the section had changed something when it has not.
    """
    where = view_name(view) if section == SECTIONS[0] else section
    return f"[$bright bold]zipf[/] [$dim]/[/] [$bright]{where}[/]"


def status_line(state: BudgetStatus, keywords: int) -> str:
    """The right of the title bar: what is stored, and what is left to spend.

    Leads the money with the effective limit for the same reason ``zipf budget``
    does — two limits shown as equals read as confusion. The meter is the same
    function the CLI uses, so the bar cannot disagree between the two shells.

    Domains and pending jobs are deliberately absent. The readout keeps the
    cache summary to one leading measure, while the jobs pane already shows the
    second.
    """
    return (
        f"{plural(keywords, 'keyword')} [$dim]·[/] "
        f"[$bright bold]${state.effective_limit:.2f}[/] [$dim]left[/] "
        f"{meter(state.used_fraction)}"
    )


#: A glyph per status, so the pane is scannable down the left edge without
#: reading. Truncating the words instead produced "queu", which is not a word.
_JOB_MARKS: Final[dict[str, str]] = {
    "done": "●",
    "failed": "✕",
    "queued": "○",
    "running": "◐",
    "cancelled": "◌",
}

_JOB_STYLES: Final[dict[str, str]] = {
    "done": "$free",
    "queued": "$spend",
    "running": "$spend",
    "failed": "$dim",
    "cancelled": "$dim",
}


def jobs_markup(rows: Sequence[sqlite3.Row]) -> str:
    """The jobs pane: what is queued, what is running, what it cost.

    Shows the subject rather than the capability — three gap pulls all reading
    ``labs.domain_intersection`` are indistinguishable, which is the problem the
    usability pass fixed in ``jobs list`` and would be inherited here.
    """
    if not rows:
        return "[$dim]no jobs[/]"

    lines = ["[$bright bold]jobs[/]"]
    for row in rows:
        params = json.loads(row["params_json"])
        status = row["status"]
        style = _JOB_STYLES.get(status, "$dim")

        # An estimate and a charge are different facts, so the tilde stays until
        # the vendor has said what it actually cost.
        actual, estimated = row["actual_cost"], row["estimated_cost"]
        cost = actual if actual is not None else estimated
        amount = f"{'' if actual is not None else '~'}${cost:.4f}" if cost is not None else ""

        lines.append(f"[{style}]{_JOB_MARKS.get(status, '·')}[/] {job_subject(params)[:19]}")
        lines.append(f"  [$dim]{status}[/] [$dim]{amount}[/]")
    return "\n".join(lines)


#: Eight levels of block, for the monthly series. The same idea as ``meter`` in
#: ``format``: one character per reading, so a year fits in twelve columns.
_SPARK: Final = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[int]) -> str:
    """A monthly series as one line of blocks.

    Scaled against the series' own maximum rather than an absolute one, because
    the question it answers is "when in the year is this searched for", not "is
    this big". A flat series therefore renders flat, which is the right answer.
    """
    if not values:
        return ""
    top = max(values)
    if top <= 0:
        return _SPARK[0] * len(values)
    return "".join(
        _SPARK[min(len(_SPARK) - 1, value * len(_SPARK) // (top + 1))] for value in values
    )


def detail_markup(detail: dict[str, Any] | None, keyword: str) -> str:
    """The detail pane for one keyword. Rich markup, rendered by a Static.

    Returns a message rather than raising when nothing is stored: a highlighted
    row whose keyword has no projection is normal in a gap table, where the
    keywords come from a competitor's ranks and were never priced.
    """
    if detail is None:
        return f"[$bright bold]{keyword}[/]\n\n[$dim]nothing priced for this keyword yet[/]"

    volume = f"{detail['volume']:,}" if detail["volume"] is not None else ABSENT
    cpc = f"${detail['cpc']:.2f}" if detail["cpc"] is not None else ABSENT
    # Difficulty is the only organic figure stored (migration 006). It sits with
    # volume and cpc deliberately: those two describe an advertising market, and
    # reading them without it is how a keyword looks worth writing about right up
    # until you see it is a 78.
    difficulty = str(detail["difficulty"]) if detail.get("difficulty") is not None else ABSENT

    lines = [f"[$bright bold]{detail['keyword']}[/]", ""]
    lines.append(_pair("volume", volume))
    lines.append(_pair("difficulty", difficulty))
    lines.append(_pair("intent", _intent(detail)))
    lines.append(_pair("cpc", cpc))
    lines.append(_pair("updated", (detail["updated_at"] or "—")[:10]))

    if detail["ranks"]:
        lines.append("")
        for index, rank in enumerate(detail["ranks"][:3]):
            label = "ranks" if index == 0 else ""
            lines.append(_pair(label, f"{rank['domain']} [$bright bold]#{rank['position']}[/]"))
    if detail.get("months"):
        series = [int(month["volume"]) for month in detail["months"]]
        lines.append("")
        lines.append(_pair(f"{len(series)}mo", f"[$bright bold]{sparkline(series)}[/]"))
        lines.append(_pair("", f"[$dim]{min(series):,} to {max(series):,}[/]"))
    if detail["gsc"]:
        gsc = detail["gsc"]
        lines.append("")
        lines.append(_pair("yours", f"{gsc['clicks']:,} clicks"))
        lines.append(_pair("", f"{gsc['impressions']:,} impressions"))
        lines.append(_pair("", plural(gsc["pages"], "page")))
    if detail.get("raw_id") is not None:
        lines.append("")
        lines.extend(_provenance(detail))
    return "\n".join(lines)


#: Width of the label column in the inspector. Wide enough for "difficulty",
#: which is the longest label any of these panes uses.
_LABEL: Final = 11


def _pair(label: str, value: str) -> str:
    """One label-and-value row of the inspector.

    The inspector is tall and narrow, so facts stack rather than running across.
    A blank label continues the row above, which is how a list of ranks reads as
    one fact rather than three.
    """
    return f"[$dim]{label:<{_LABEL}}[/]{value}"


def _intent(detail: dict[str, Any]) -> str:
    """What the searcher wanted, with the confidence behind the label.

    The probability is shown because the label alone hides the difference
    between a keyword classified at 97% and one at 51%, which is a coin toss
    wearing a label (migration 006).
    """
    intent = detail.get("intent")
    if not intent:
        return "[$dim]intent unknown[/]"
    probability = detail.get("intent_probability")
    if probability is None:
        return str(intent)
    return f"{intent} [$dim]{probability:.0%}[/]"


def _provenance(detail: dict[str, Any]) -> list[str]:
    """Which stored response this keyword's figures came from, and what it cost."""
    source = detail.get("source")
    if source is None:
        return [_pair("source", f"#{detail['raw_id']}")]
    cost = "free" if not source["cost_usd"] else f"${source['cost_usd']:.5f}"
    return [
        _pair("source", f"#{detail['raw_id']} · {cost}"),
        _pair("", f"[$dim]{source['capability']}[/]"),
        _pair("", f"[$dim]{source['fetched_at'][:10]} · [$bright bold]p[/] opens it[/]"),
    ]


def response_markup(detail: dict[str, Any]) -> str:
    """The inspector for one stored response.

    States what it cost and that everything downstream can be rebuilt from it.
    That second fact is the reason the table is worth opening: it is the
    difference between owning data and having merely seen it.
    """
    cost = "free" if not detail["cost_usd"] else f"${detail['cost_usd']:.5f}"
    lines = [
        f"[$bright bold]raw_response #{detail['id']}[/]",
        "[$dim]immutable bytes[/]",
        "",
        _pair("capability", detail["capability"]),
        _pair("cost", cost),
        _pair("bytes", f"{detail['bytes']:,}"),
        _pair("fetched", detail["fetched_at"][:16].replace("T", " ")),
        _pair("params", f"[$dim]{detail['params_hash'][:12]}[/]"),
    ]
    job = detail.get("job")
    if job is not None:
        lines.append(_pair("job", f"{job['id']} · {job['status']}"))
    lines.append("")
    lines.append("[$dim]every row here can be rebuilt from these bytes · nothing re-fetched[/]")
    return "\n".join(lines)
