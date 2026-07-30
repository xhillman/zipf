"""What each sidebar node puts in the table.

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

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from rich.text import Text

from zipf import capabilities
from zipf.clock import from_iso, now
from zipf.format import ABSENT, meter, plural
from zipf.services import browse
from zipf.services import gap as gap_service
from zipf.services.budget import BudgetStatus
from zipf.sources.dataforseo import labs

#: Sidebar buckets. The tree order is the order you would ask the questions in:
#: what do I know, who else ranks, where is the space, what do I already own.
KEYWORDS: Final = "keywords"
DOMAINS: Final = "domains"
DOMAIN: Final = "domain"
GAPS: Final = "gaps"
GAP: Final = "gap"
GSC: Final = "gsc"
VISIBILITY: Final = "visibility"

#: What a table's row keys name, which decides what the detail pane can look up.
KEYWORD_KEY: Final = "keyword"
OPAQUE_KEY: Final = "opaque"

#: Which sort keys each view offers, in the order pressing sort cycles through
#: them. Only views backed by a sortable query appear: a gap pull is already
#: ordered by opportunity, and re-sorting 50 clustered rows answers no question.
SORT_CYCLES: Final[dict[str, tuple[str, ...]]] = {
    KEYWORDS: tuple(browse.KEYWORD_SORTS),
    DOMAINS: tuple(browse.DOMAIN_SORTS),
    GSC: tuple(browse.GSC_SORTS),
}

#: Column header to sort key, for sorting by clicking a header. Absent headers
#: are not sortable — ``aio`` has one of three values and ``url`` is incidental.
SORT_BY_COLUMN: Final[dict[str, dict[str, str]]] = {
    KEYWORDS: {"keyword": "keyword", "vol": "volume", "age": "updated", "pos": "position"},
    DOMAINS: {"domain": "domain", "keywords": "keywords", "age": "observed"},
    GSC: {"query": "query", "clicks": "clicks", "impr": "impressions", "pos": "position"},
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
    """The sort key a column header maps to, or ``None`` if it is not sortable."""
    return SORT_BY_COLUMN.get(kind, {}).get(column)


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
    return None


#: How long a stored volume stays true. Used to decide whether an age is worth
#: showing at all: below the TTL the number is current, and printing "3d" next to
#: current data is noise the PRD explicitly rules out ("default to silence").
_VOLUME_TTL: Final = capabilities.get(labs.SEARCH_VOLUME).ttl


@dataclass(frozen=True)
class View:
    """A sidebar selection: which bucket, and which member of it."""

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

    @property
    def is_empty(self) -> bool:
        return not self.rows


def _right(value: str) -> Text:
    return Text(value, justify="right")


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


def _keyword_rows(rows: list[dict[str, Any]]) -> TableSpec:
    """The main keyword table: what it is worth, how old, and where you stand."""
    cells: list[tuple[Text | str, ...]] = []
    for row in rows:
        volume = f"{row['volume']:,}" if row["volume"] is not None else "—"
        position = str(row["position"]) if row["position"] is not None else "—"
        cells.append(
            (
                Text(row["keyword"]),
                _right(volume),
                _right(_stale_age(row["updated_at"])),
                _right(_aio(row["has_aio"])),
                _right(position),
            )
        )
    return TableSpec(
        columns=("keyword", "vol", "age", "aio", "pos"),
        rows=cells,
        caption=plural(len(rows), "keyword"),
        keys=[row["keyword"] for row in rows],
    )


def _domain_rows(rows: list[dict[str, Any]]) -> TableSpec:
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
        columns=("domain", "keywords", "best", "age"),
        rows=cells,
        caption=plural(len(rows), "domain"),
        keys=[row["domain"] for row in rows],
        key_kind=OPAQUE_KEY,
    )


def _domain_keyword_rows(rows: list[dict[str, Any]], domain: str) -> TableSpec:
    cells: list[tuple[Text | str, ...]] = [
        (
            Text(row["keyword"]),
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
    )


def _gap_rows(conn: sqlite3.Connection, pair: str, contains: str | None) -> TableSpec:
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
            Text(cluster.keyword),
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
    )


def _gsc_rows(rows: list[dict[str, Any]]) -> TableSpec:
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
        columns=("query", "clicks", "impr", "pos", "pages"),
        rows=cells,
        caption=plural(len(rows), "query"),
        keys=[row["query"] for row in rows],
    )


def table_for(
    conn: sqlite3.Connection,
    view: View,
    *,
    own_domain: str | None = None,
    contains: str | None = None,
    sort: str | None = None,
) -> TableSpec:
    """Build the table for one sidebar selection.

    ``contains`` and ``sort`` are accepted now and wired to the filter and sort
    commands later; passing them through from the start keeps the signature
    stable rather than rewriting every call site when those land.
    """
    if view.kind == KEYWORDS:
        return _keyword_rows(
            browse.keywords(conn, contains=contains, sort=sort, own_domain=own_domain)
        )
    if view.kind == DOMAINS:
        return _domain_rows(browse.domains(conn, contains=contains, sort=sort))
    if view.kind == DOMAIN and view.arg:
        return _domain_keyword_rows(
            browse.domain_keywords(conn, view.arg, contains=contains), view.arg
        )
    if view.kind == GAPS:
        return _gap_pair_rows(browse.gap_pairs(conn, contains=contains))
    if view.kind == GAP and view.arg:
        return _gap_rows(conn, view.arg, contains)
    if view.kind == GSC:
        return _gsc_rows(browse.gsc_queries(conn, contains=contains, sort=sort))
    return _visibility_placeholder()


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
    )


def status_line(state: BudgetStatus, totals: browse.CacheCounts) -> str:
    """The persistent readout: what is left to spend, and what is stored.

    Leads with the effective limit for the same reason ``zipf budget`` does —
    two limits shown as equals read as confusion. The meter is the same function
    the CLI uses, so the bar cannot disagree between the two shells.
    """
    pending = f" · [bold]{plural(totals.jobs_pending, 'job')}[/]" if totals.jobs_pending else ""
    return (
        f" [bold]zipf[/]  ${state.effective_limit:.2f} left "
        f"{meter(state.used_fraction)}  "
        f"[dim]{totals.keywords:,} kw · {plural(totals.domains, 'domain')}[/]{pending}"
    )


def detail_markup(detail: dict[str, Any] | None, keyword: str) -> str:
    """The detail pane for one keyword. Rich markup, rendered by a Static.

    Returns a message rather than raising when nothing is stored: a highlighted
    row whose keyword has no projection is normal in a gap table, where the
    keywords come from a competitor's ranks and were never priced.
    """
    if detail is None:
        return f"[dim]{keyword}[/]\n[dim]nothing priced for this keyword yet[/]"

    lines = [f"[bold]{detail['keyword']}[/]"]
    volume = f"{detail['volume']:,}" if detail["volume"] is not None else ABSENT
    cpc = f"${detail['cpc']:.2f}" if detail["cpc"] is not None else ABSENT
    lines.append(f"  volume {volume}   cpc {cpc}   updated {detail['updated_at'] or '—'}")

    if detail["ranks"]:
        ranked = "  ".join(
            f"{rank['domain']} [bold]#{rank['position']}[/]" for rank in detail["ranks"][:4]
        )
        lines.append(f"  ranks   {ranked}")
    if detail["gsc"]:
        gsc = detail["gsc"]
        lines.append(
            f"  yours   {gsc['clicks']:,} clicks · {gsc['impressions']:,} impressions "
            f"· {plural(gsc['pages'], 'page')}"
        )
    return "\n".join(lines)
