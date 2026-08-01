"""Command line interface.

The CLI is a permanent peer of the TUI, not scaffolding (spec D2). Both are thin
shells over ``zipf.services``; neither owns logic.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from zipf import prune as prune_cache
from zipf.budget import Budget
from zipf.cli.paid import confirm_spend
from zipf.clock import age_of, age_of_delta, elapsed_between
from zipf.config import DEFAULT_CONFIG_TOML, Paths, load_settings
from zipf.db.connection import connect
from zipf.db.migrate import migrate
from zipf.errors import ConfigMissingError, ZipfError
from zipf.format import meter, money, number, plural
from zipf.jobs import queue as job_queue
from zipf.jobs.describe import job_depth, job_kind, job_subject
from zipf.jobs.runner import JobRunner
from zipf.projections.rebuild import count_rows
from zipf.projections.rebuild import rebuild as rebuild_projections
from zipf.services import budget as budget_service
from zipf.services import gap as gap_service
from zipf.services import gsc as gsc_service
from zipf.services import ideas as ideas_service
from zipf.services import ranks as ranks_service
from zipf.services import suggest as suggest_service
from zipf.services import volume as volume_service
from zipf.services.cluster import Cluster
from zipf.sources import gsc as gsc_source
from zipf.sources.dataforseo import keywords_data

app = typer.Typer(
    name="zipf",
    help="Keyword research over a local SQLite cache. Run bare to browse the cache.",
    invoke_without_command=True,
    add_completion=False,
)
db_app = typer.Typer(name="db", help="Database maintenance.", no_args_is_help=True)
gsc_app = typer.Typer(name="gsc", help="Search Console. Free, so refreshed greedily.")
jobs_app = typer.Typer(name="jobs", help="The work queue. Paid work runs here, never inline.")
app.add_typer(db_app)
app.add_typer(gsc_app)
app.add_typer(jobs_app)

console = Console()

_JOB_STYLES = {"done": "green", "failed": "red", "queued": "yellow", "running": "cyan"}


def _cost_cell(row: sqlite3.Row) -> str:
    """What a job cost, falling back to the estimate while it is still pending."""
    if row["actual_cost"] is not None:
        return f"${row['actual_cost']:.5f}"
    if row["estimated_cost"] is not None:
        return f"[dim]~${row['estimated_cost']:.5f}[/]"
    return "[dim]—[/]"


def _budget() -> Budget:
    settings = load_settings()
    return Budget(
        ceiling_usd=settings.monthly_ceiling_usd,
        threshold_usd=settings.confirm_threshold_usd,
    )


def _with_db[T](work: Callable[[sqlite3.Connection], Coroutine[Any, Any, T]]) -> T:
    """Run async work against the database, closing the connection on every path."""

    async def run() -> T:
        with connect(Paths.resolve().db_file) as conn:
            return await work(conn)

    return asyncio.run(run())


def _with_db_sync(work: Callable[[sqlite3.Connection], None]) -> None:
    """Run synchronous work against the database, closing it on every path."""
    with connect(Paths.resolve().db_file) as conn:
        work(conn)


def _drain(conn: sqlite3.Connection) -> None:
    """Host the runner in the foreground until the queue empties (spec D1)."""
    runner = JobRunner(
        conn, _budget(), on_event=lambda message: console.print(f"[dim]{message}[/]")
    )
    spent_before = _budget().spent_this_month(conn)
    report = asyncio.run(runner.drain())
    _effect(
        f"[green]{report.done} done[/]",
        f"{report.failed} failed" if report.failed else "",
        f"{report.requeued} retrying" if report.requeued else "",
        _spend_since(conn, spent_before),
    )


def _effect(*parts: str) -> None:
    """The closing line of a command: what changed, and what it cost.

    Every command ends with one of these so the outcome is never inferred from
    the absence of an error. Cost is always stated, including when it is zero —
    "$0.00" is information, and its absence is not.
    """
    console.print("[bold]→[/] " + " · ".join(part for part in parts if part))


def _spend_since(conn: sqlite3.Connection, before: float) -> str:
    """What this command actually cost, measured rather than predicted.

    Derived from the same ``raw_response`` sum the budget uses, so the figure a
    command reports and the figure ``zipf budget`` reports cannot disagree.
    """
    delta = _budget().spent_this_month(conn) - before
    return f"${delta:.5f}" if delta > 0 else "$0.00"


def _print_volumes(clusters: list[Cluster], total_rows: int) -> None:
    if not clusters:
        console.print("[dim]nothing stored for these keywords yet[/]")
        return

    collapsed = total_rows - len(clusters)
    title = f"{len(clusters)} distinct quer{'y' if len(clusters) == 1 else 'ies'}"
    if collapsed:
        title += f" · {collapsed} restatement(s) collapsed"

    table = Table(title=title)
    table.add_column("keyword")
    table.add_column("volume", justify="right")
    table.add_column("cpc", justify="right")
    table.add_column("comp", justify="right")
    table.add_column("also", justify="right", style="dim")
    for cluster in clusters:
        best = cluster.representative
        table.add_row(
            cluster.keyword,
            number(cluster.volume),
            money(best.get("cpc")),
            number(best.get("competition"), ".2f"),
            f"+{cluster.variant_count - 1}" if cluster.has_variants else "",
        )
    console.print(table)


def _print_volumes_flat(rows: list[dict[str, Any]]) -> None:
    table = Table(title=f"{len(rows)} keyword(s), every phrasing")
    table.add_column("keyword")
    table.add_column("volume", justify="right")
    table.add_column("cpc", justify="right")
    table.add_column("comp", justify="right")
    for row in rows:
        table.add_row(
            row["keyword"],
            number(row["volume"]),
            money(row["cpc"]),
            number(row["competition"], ".2f"),
        )
    console.print(table)


def _print_gap(clusters: list[Cluster], total_rows: int) -> None:
    if not clusters:
        console.print("[dim]no gap rows stored yet; run the job first[/]")
        return

    collapsed = total_rows - len(clusters)
    title = f"{len(clusters)} distinct quer{'y' if len(clusters) == 1 else 'ies'}"
    if collapsed:
        title += f" · {collapsed} restatement(s) collapsed"

    table = Table(title=title)
    table.add_column("keyword")
    table.add_column("vol", justify="right")
    table.add_column("pos", justify="right")
    table.add_column("also", justify="right", style="dim")
    table.add_column("their url")
    for cluster in clusters:
        volume = f"{cluster.volume:,}" if cluster.volume is not None else "[dim]—[/]"
        also = f"+{cluster.variant_count - 1}" if cluster.has_variants else ""
        table.add_row(
            cluster.keyword,
            volume,
            str(cluster.position) if cluster.position is not None else "—",
            also,
            (cluster.url or "")[:46],
        )
    console.print(table)


def _print_gap_flat(rows: list[dict[str, Any]]) -> None:
    table = Table(title=f"{len(rows)} gap keyword(s), every phrasing")
    table.add_column("keyword")
    table.add_column("vol", justify="right")
    table.add_column("pos", justify="right")
    for row in rows:
        volume = f"{row['volume']:,}" if row["volume"] is not None else "[dim]—[/]"
        table.add_row(row["keyword"], volume, str(row["position"]))
    console.print(table)


@app.callback()
def default(ctx: typer.Context) -> None:
    """Open the cache browser when no subcommand is given.

    Browsing is read-only and free, so the bare command is the safe default.
    Anything that spends money is a subcommand or a typed `:` command.
    """
    if ctx.invoked_subcommand is not None:
        return

    from zipf.tui.app import ZipfApp

    settings = load_settings()
    paths = Paths.resolve()
    # Two handles, both released here on every exit path. Browsing uses the
    # read-only one so the screen cannot write; only a typed command reaches the
    # writable one.
    with (
        connect(paths.db_file, read_only=True) as reader,
        connect(paths.db_file) as writer,
    ):
        ZipfApp(
            reader,
            write_conn=writer,
            budget=_budget(),
            own_domain=settings.own_domain,
        ).run()


@app.command()
def init() -> None:
    """Create the database, run migrations, and scaffold a config file."""
    paths = Paths.resolve()
    paths.ensure()

    with connect(paths.db_file) as conn:
        applied = migrate(conn)
        stored = count_rows(conn, "raw_response")

    if paths.config_file.exists():
        console.print(f"config   [dim]exists[/]   {paths.config_file}")
    else:
        paths.config_file.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        console.print(f"config   [green]created[/]  {paths.config_file}")

    state = f"[green]{len(applied)} migration(s)[/]" if applied else "[dim]up to date[/]"
    console.print(f"database {state}  {paths.db_file}")
    _effect(
        "ready" if not applied else f"{plural(len(applied), 'migration')} applied",
        plural(stored, "stored response"),
        "$0.00",
    )


@app.command()
def suggest(
    seed: str = typer.Argument(..., help="Seed keyword to expand."),
    questions: bool = typer.Option(False, "--questions", help="Add question-word seeds."),
    alphabet: bool = typer.Option(False, "--alphabet", help="Add a-z suffix seeds."),
    country: str = typer.Option("us", "--country", help="Two-letter market code."),
    lang: str = typer.Option("en", "--lang", help="Two-letter language code."),
    force: bool = typer.Option(False, "--force", help="Ignore fresh cache entries."),
    limit: int = typer.Option(40, "--limit", help="Rows to print."),
) -> None:
    """Keyword suggestions from Google Autocomplete. Tier 0, free."""

    async def work(conn: sqlite3.Connection) -> tuple[suggest_service.ExpansionResult, int]:
        known_before = count_rows(conn, "keyword")
        expansion = await suggest_service.expand(
            conn,
            seed,
            budget=_budget(),
            questions=questions,
            alphabet=alphabet,
            lang=lang,
            country=country,
            force=force,
        )
        return expansion, count_rows(conn, "keyword") - known_before

    result, newly_stored = _with_db(work)
    terms = result.suggestions

    table = Table(title=f"{seed} · {len(terms)} suggestions")
    table.add_column("#", justify="right", style="dim")
    table.add_column("suggestion")
    for i, term in enumerate(terms[:limit], 1):
        table.add_row(str(i), term)
    console.print(table)

    if len(terms) > limit:
        console.print(f"[dim]showing {limit}; all {len(terms)} are stored[/]")
    for bad_seed, reason in result.failures.items():
        console.print(f"[yellow]skipped[/] {bad_seed}: {reason}")

    _effect(
        f"{plural(len(terms), 'suggestion')} from {plural(len(result.results), 'seed')}",
        f"{result.cache_hits} cached",
        f"{plural(newly_stored, 'new keyword')} stored",
        "$0.00",
    )


@gsc_app.command("auth")
def gsc_auth() -> None:
    """Authorise Search Console access once, in a browser."""
    from zipf.sources import google_oauth

    Paths.resolve().ensure()
    google_oauth.authorise()
    console.print("[green]authorised[/] token stored in the state directory, mode 0600")


@gsc_app.command("sites")
def gsc_sites() -> None:
    """List the Search Console properties this account can read. Free."""
    from zipf.fetch import fetch

    async def work(conn: sqlite3.Connection) -> list[dict[str, str]]:
        result = await fetch(conn, gsc_source.SITES_CAPABILITY, {}, budget=_budget())
        sites: list[dict[str, str]] = result.parsed()
        return sites

    sites = _with_db(work)
    if not sites:
        console.print("[yellow]no properties[/] this account cannot read any Search Console site")
        return

    table = Table(title=f"{len(sites)} Search Console propert(ies)")
    table.add_column("property id")
    table.add_column("permission", style="dim")
    for site in sites:
        table.add_row(site["site_url"], site["permission"])
    console.print(table)
    console.print("[dim]put one in config.toml as gsc_site_url, or pass --site[/]")


@gsc_app.command("import")
def gsc_import(
    days: int = typer.Option(90, "--days", help="Trailing window to import."),
    site_url: str = typer.Option(
        None, "--site", help="Property id, e.g. sc-domain:example.com. Defaults to config."
    ),
    force: bool = typer.Option(False, "--force", help="Ignore fresh cache entries."),
) -> None:
    """Import your own Search Console queries. Free."""
    target = site_url or load_settings().gsc_site_url
    if not target:
        raise ConfigMissingError(
            setting="gsc_site_url",
            config_path=str(Paths.resolve().config_file),
            flag="--site",
        )

    async def work(conn: sqlite3.Connection) -> gsc_service.ImportResult:
        return await gsc_service.import_queries(
            conn, site_url=target, budget=_budget(), days=days, force=force
        )

    result = _with_db(work)
    console.print(
        f"[green]{result.rows:,}[/] rows · {result.pages_read} page(s) · "
        f"{result.cache_hits} from cache · {result.start_date} to {result.end_date} · $0.00"
    )
    if result.truncated:
        console.print(
            f"[yellow]truncated[/] stopped at the {gsc_source.MAX_PAGES}-page bound; "
            "narrow --days to see the rest"
        )
    for error in result.projection_errors:
        console.print(f"[yellow]projection failed[/] {error} — bytes kept, run `zipf db rebuild`")

    _effect(
        f"{plural(result.rows, 'query row')} imported",
        f"{plural(result.pages_read, 'page')} read, {result.cache_hits} cached",
        f"{result.start_date} to {result.end_date}",
        "$0.00",
    )


@db_app.command("rebuild")
def db_rebuild(
    capability: str = typer.Option(None, "--capability", help="Rebuild one capability only."),
) -> None:
    """Drop projection tables and replay every stored response. Free."""
    with connect(Paths.resolve().db_file) as conn:
        stats = rebuild_projections(conn, capability)

    table = Table(title="projections rebuilt")
    table.add_column("table")
    table.add_column("before", justify="right")
    table.add_column("after", justify="right")
    table.add_column("change", justify="right")
    for name in stats.tables_cleared:
        delta = stats.net_change[name]
        style = "red" if delta < 0 else ("green" if delta > 0 else "dim")
        table.add_row(
            name,
            f"{stats.before.get(name, 0):,}",
            f"{stats.after[name]:,}",
            f"[{style}]{delta:+,}[/]" if delta else "[dim]—[/]",
        )
    console.print(table)

    if stats.lost_rows:
        console.print(
            f"[red]rows lost[/] {stats.lost_rows} — a rebuild should never shrink a table"
        )

    _effect(
        f"{plural(len(stats.tables_cleared), 'table')} replayed",
        f"from {plural(stats.rows_replayed, 'stored response')}",
        "$0.00",
    )


@db_app.command("prune")
def db_prune(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would go. Removes nothing."),
) -> None:
    """Drop free, superseded responses that nothing is derived from. Free.

    Only ever removes rows that cost nothing, that no projection reads, and that
    are neither the newest answer to their question nor still inside their TTL.
    Anything paid for is refused by the database itself.
    """
    paths = Paths.resolve()
    with connect(paths.db_file) as conn:
        planned = prune_cache.preview(conn)

        if planned.is_empty:
            console.print(
                "[green]nothing to prune[/] no free response is both stale and superseded"
            )
            _effect("nothing removed", "$0.00")
            return

        table = Table(
            title=f"{plural(planned.rows, 'response')}, {planned.bytes / 1_048_576:.2f} MB"
        )
        table.add_column("capability")
        table.add_column("rows", justify="right")
        for name, count in planned.by_capability.items():
            table.add_row(name, f"{count:,}")
        console.print(table)

        if dry_run:
            console.print("[cyan]dry run[/] nothing was removed")
            _effect(f"{plural(planned.rows, 'response')} would go", "$0.00")
            return

        prune_cache.prune(conn)
        reclaimed = prune_cache.reclaim(conn, paths.db_file)

    _effect(
        f"{plural(planned.rows, 'response')} removed",
        f"{reclaimed / 1_048_576:.2f} MB reclaimed",
        "$0.00",
    )


@app.command()
def vol(
    keywords: Annotated[
        list[str], typer.Argument(help="Keywords to price. Pass many; batching is cheap.")
    ],
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan and the bill. Spend $0."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
    force: bool = typer.Option(False, "--force", help="Re-buy even keywords that are still fresh."),
    wait: bool = typer.Option(False, "--wait", help="Drain the queue before returning."),
    flat: bool = typer.Option(False, "--flat", help="Show every phrasing, uncollapsed."),
    cached: bool = typer.Option(
        False, "--cached", help="Read stored data only. Never prompts, never spends."
    ),
) -> None:
    """Search volume for keywords. Tier 1, paid.

    Pass every keyword you care about in one call: Labs charges a per-call base
    that dwarfs the per-keyword cost, so many small calls are the expensive
    mistake.
    """

    def run(conn: sqlite3.Connection) -> None:
        spent_before = _budget().spent_this_month(conn)
        plan = volume_service.plan(conn, keywords, force=force)
        console.print(
            f"[dim]{len(plan.requested)} keyword(s) · {len(plan.cached)} still fresh · "
            f"{len(plan.stale)} to buy in {len(plan.batches)} call(s)[/]"
        )

        if plan.is_free:
            console.print("[green]all cached[/] nothing to buy")
        elif cached:
            console.print(
                f"[dim]--cached: {len(plan.stale)} keyword(s) not stored, "
                f"skipping the ${plan.estimate.usd:.5f} pull[/]"
            )
        elif dry_run:
            console.print(
                f"[cyan]dry run[/] would buy {len(plan.stale)} keyword(s) "
                f"for ${plan.estimate.usd:.5f} · spent $0.00"
            )
        elif confirm_spend(
            conn,
            _budget(),
            plan.estimate,
            what=f"search volume · {len(plan.stale)} keyword(s)",
            assume_yes=yes,
        ):
            job_ids = volume_service.enqueue(conn, plan)
            console.print(f"[green]queued[/] job(s) {', '.join(str(i) for i in job_ids)}")
            if wait:
                _drain(conn)
        else:
            console.print("[yellow]cancelled[/] nothing queued, nothing spent")

        # Always show what is owned, including after a decline. Refusing to buy
        # more is not a reason to hide what was already paid for.
        rows = volume_service.read_rows(conn, plan.requested)
        if flat:
            _print_volumes_flat(rows)
        else:
            _print_volumes(volume_service.read(conn, plan.requested), len(rows))

        measured = sum(1 for row in rows if row["volume"] is not None)
        _effect(
            f"{plural(len(plan.requested), 'keyword')} asked for",
            f"{measured} measured",
            f"{len(plan.cached)} already fresh",
            _spend_since(conn, spent_before),
        )

    _with_db_sync(run)


@app.command()
def gap(
    competitor: str = typer.Argument(..., help="The domain you want to take keywords from."),
    mine: str = typer.Option(None, "--mine", help="Your domain. Defaults to config own_domain."),
    limit: int = typer.Option(
        100, "--limit", help="Rows to buy. You pay for the depth you ask for."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan and the bill. Spend $0."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
    wait: bool = typer.Option(False, "--wait", help="Drain the queue before returning."),
    flat: bool = typer.Option(False, "--flat", help="Show every phrasing, uncollapsed."),
    force: bool = typer.Option(False, "--force", help="Re-buy even if a fresh pull is stored."),
    cached: bool = typer.Option(
        False, "--cached", help="Read stored data only. Never prompts, never spends."
    ),
) -> None:
    """Keywords a competitor ranks for and you do not. Tier 1, paid.

    Reading a gap you already own is free and silent. You are only asked to pay
    when the stored pull is missing or past its TTL.
    """
    own = mine or load_settings().own_domain
    if not own:
        raise ConfigMissingError(
            setting="own_domain",
            config_path=str(Paths.resolve().config_file),
            flag="--mine",
        )

    def run(conn: sqlite3.Connection) -> None:
        spent_before = _budget().spent_this_month(conn)
        plan = gap_service.plan(conn, competitor, own, limit=limit, force=force)

        if plan.is_fresh:
            age_days = (plan.age.total_seconds() / 86400) if plan.age else 0.0
            console.print(f"[green]cached[/] pulled {age_days:.1f}d ago · nothing to buy · $0.00")
        elif cached:
            console.print(
                f"[dim]--cached: no stored pull for {plan.competitor}, "
                f"skipping the ${plan.estimate.usd:.5f} pull[/]"
            )
        elif dry_run:
            console.print(
                f"[cyan]dry run[/] would buy up to {plan.limit:,} rows for "
                f"${plan.estimate.usd:.5f} · spent $0.00"
            )
        elif confirm_spend(
            conn,
            _budget(),
            plan.estimate,
            what=f"keyword gap · {plan.competitor}",
            detail=f"{plan.competitor} ranks for, {plan.mine} does not",
            assume_yes=yes,
        ):
            job_id = gap_service.enqueue(conn, plan)
            console.print(f"[green]queued[/] job {job_id}")
            if wait:
                _drain(conn)
        else:
            console.print("[yellow]cancelled[/] nothing queued, nothing spent")

        rows = gap_service.read_rows(conn, plan.competitor, plan.mine)
        clusters = gap_service.read(conn, plan.competitor, plan.mine)
        if flat:
            _print_gap_flat(rows)
        else:
            _print_gap(clusters, len(rows))

        _effect(
            f"{plural(len(clusters), 'distinct query')} from {plural(len(rows), 'row')}",
            f"{plan.competitor} not matched by {plan.mine}",
            _spend_since(conn, spent_before),
        )

    _with_db_sync(run)


def _print_ranks(clusters: list[Cluster], total_rows: int, domain: str) -> None:
    if not clusters:
        console.print(f"[dim]nothing stored for {domain} yet[/]")
        return

    collapsed = total_rows - len(clusters)
    title = f"{domain} · {plural(len(clusters), 'distinct query')}"
    if collapsed:
        title += f" · {collapsed} restatement(s) collapsed"

    table = Table(title=title)
    table.add_column("keyword")
    table.add_column("pos", justify="right")
    table.add_column("vol", justify="right")
    table.add_column("also", justify="right", style="dim")
    table.add_column("url")
    for cluster in clusters:
        table.add_row(
            cluster.keyword,
            str(cluster.position) if cluster.position is not None else "—",
            number(cluster.volume),
            f"+{cluster.variant_count - 1}" if cluster.has_variants else "",
            (cluster.url or "").removeprefix("https://").removeprefix("http://")[:46],
        )
    console.print(table)


def _print_ideas(rows: list[dict[str, Any]]) -> None:
    if not rows:
        console.print("[dim]nothing stored for these seeds yet; run the job first[/]")
        return

    seasonal = sum(1 for row in rows if row.get("peak"))
    title = f"{plural(len(rows), 'keyword')}"
    if seasonal:
        title += f" · {seasonal} seasonal"

    table = Table(title=title)
    table.add_column("keyword")
    table.add_column("vol", justify="right")
    table.add_column("cpc", justify="right")
    table.add_column("comp", justify="right")
    # Rendered only where there is a real peak. A column of month names on flat
    # data would read as a recommendation about when to publish.
    table.add_column("peak", justify="right", style="dim")
    for row in rows:
        table.add_row(
            row["keyword"],
            number(row["volume"]),
            money(row["cpc"]),
            number(row["competition"], ".2f"),
            row.get("peak", ""),
        )
    console.print(table)


@app.command()
def ideas(
    seeds: Annotated[
        list[str],
        typer.Argument(help="Seed keywords. Pass up to 20; one call is one flat charge."),
    ],
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan and the bill. Spend $0."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
    wait: bool = typer.Option(False, "--wait", help="Drain the queue before returning."),
    force: bool = typer.Option(False, "--force", help="Re-buy even if a covering pull is stored."),
    limit: int = typer.Option(100, "--limit", help="Rows to print. All of them are stored."),
    cached: bool = typer.Option(
        False, "--cached", help="Read stored data only. Never prompts, never spends."
    ),
) -> None:
    """Discover keywords, with volume and twelve months of history attached.

    Unlike everything else here, the price does not depend on how much you ask
    for: one call is a flat charge whether it returns thirty keywords or twenty
    thousand. Pass every seed you care about at once — asking one question at a
    time costs the same as asking twenty.
    """

    def run(conn: sqlite3.Connection) -> None:
        spent_before = _budget().spent_this_month(conn)
        plan = ideas_service.plan(conn, seeds, force=force)
        console.print(
            f"[dim]{plural(len(plan.seeds), 'seed')} in one call · "
            f"flat ${plan.estimate.usd:.2f}, however many come back[/]"
        )

        if plan.is_fresh:
            age_days = (plan.age.total_seconds() / 86400) if plan.age else 0.0
            covered = ", ".join(plan.covered_by or [])
            console.print(f"[green]cached[/] pulled {age_days:.1f}d ago from [{covered}] · $0.00")
        elif cached:
            console.print(
                f"[dim]--cached: no stored pull covers these seeds, "
                f"skipping the ${plan.estimate.usd:.2f} call[/]"
            )
        elif dry_run:
            console.print(
                f"[cyan]dry run[/] would buy up to {keywords_data.MAX_SEEDS * 1000:,} keywords "
                f"for ${plan.estimate.usd:.2f} · spent $0.00"
            )
        elif confirm_spend(
            conn,
            _budget(),
            plan.estimate,
            what=f"keyword ideas · {plural(len(plan.seeds), 'seed')}",
            detail=", ".join(plan.seeds),
            assume_yes=yes,
        ):
            job_id = ideas_service.enqueue(conn, plan)
            console.print(f"[green]queued[/] job {job_id}")
            if wait:
                _drain(conn)
        else:
            console.print("[yellow]cancelled[/] nothing queued, nothing spent")

        rows = ideas_service.read_rows(conn, plan.seeds, limit=limit)
        _print_ideas(rows)

        _effect(
            f"{plural(len(rows), 'keyword')} shown",
            f"{sum(1 for row in rows if row.get('peak'))} seasonal",
            _spend_since(conn, spent_before),
        )

    _with_db_sync(run)


@app.command()
def ranks(
    domain: str = typer.Argument(None, help="Domain to look up. Defaults to config own_domain."),
    limit: int = typer.Option(
        100, "--limit", help="Rows to buy. You pay for the depth you ask for."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan and the bill. Spend $0."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
    wait: bool = typer.Option(False, "--wait", help="Drain the queue before returning."),
    force: bool = typer.Option(False, "--force", help="Re-buy even if a fresh pull is stored."),
    cached: bool = typer.Option(
        False, "--cached", help="Read stored data only. Never prompts, never spends."
    ),
) -> None:
    """What a domain already ranks for. Tier 1, paid.

    Defaults to your own domain, which is the one worth knowing: without it every
    other view can tell you what a competitor ranks for and nothing about where
    you stand.
    """
    target = domain or load_settings().own_domain
    if not target:
        raise ConfigMissingError(
            setting="own_domain",
            config_path=str(Paths.resolve().config_file),
            flag="a domain argument",
        )

    def run(conn: sqlite3.Connection) -> None:
        spent_before = _budget().spent_this_month(conn)
        plan = ranks_service.plan(conn, target, limit=limit, force=force)

        if plan.is_fresh:
            age_days = (plan.age.total_seconds() / 86400) if plan.age else 0.0
            console.print(f"[green]cached[/] pulled {age_days:.1f}d ago · nothing to buy · $0.00")
        elif cached:
            console.print(
                f"[dim]--cached: no stored pull for {plan.domain}, "
                f"skipping the ${plan.estimate.usd:.5f} pull[/]"
            )
        elif dry_run:
            console.print(
                f"[cyan]dry run[/] would buy up to {plan.limit:,} rows for "
                f"${plan.estimate.usd:.5f} · spent $0.00"
            )
        elif confirm_spend(
            conn,
            _budget(),
            plan.estimate,
            what=f"ranked keywords · {plan.domain}",
            detail=f"what {plan.domain} already ranks for",
            assume_yes=yes,
        ):
            job_id = ranks_service.enqueue(conn, plan)
            console.print(f"[green]queued[/] job {job_id}")
            if wait:
                _drain(conn)
        else:
            console.print("[yellow]cancelled[/] nothing queued, nothing spent")

        rows = ranks_service.read_rows(conn, plan.domain)
        clusters = ranks_service.read(conn, plan.domain)
        _print_ranks(clusters, len(rows), plan.domain)

        ranked = sum(1 for row in rows if row["position"] is not None)
        _effect(
            f"{plural(len(clusters), 'distinct query')} from {plural(len(rows), 'row')}",
            f"{ranked} ranked",
            _spend_since(conn, spent_before),
        )

    _with_db_sync(run)


@jobs_app.command("run")
def jobs_run() -> None:
    """Drain the queue. This is where money is actually spent."""

    def run(conn: sqlite3.Connection) -> None:
        _drain(conn)

    _with_db_sync(run)


@jobs_app.command("list")
def jobs_list(limit: int = typer.Option(20, "--limit")) -> None:
    """Recent jobs, newest first."""
    with connect(Paths.resolve().db_file, read_only=True) as conn:
        rows = job_queue.recent(conn, limit)

    if not rows:
        console.print("[dim]no jobs[/]")
        return

    table = Table(title=f"{len(rows)} recent job(s)")
    table.add_column("id", justify="right", style="dim")
    # One line per job: a queue is scanned vertically, so wrapping costs more
    # than the truncated tail is worth. Only "what" may be shortened; a
    # truncated status or cost would be actively misleading.
    table.add_column("what", max_width=28, no_wrap=True, overflow="ellipsis")
    table.add_column("kind", style="dim", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("when", justify="right", style="dim", no_wrap=True)
    table.add_column("cost", justify="right", no_wrap=True)

    for row in rows:
        params = json.loads(row["params_json"])
        # Attempts only carry information once a retry has happened.
        retries = f" [yellow]x{row['attempts']}[/]" if row["attempts"] > 1 else ""
        table.add_row(
            str(row["id"]),
            job_subject(params),
            job_kind(row["capability"]),
            f"[{_JOB_STYLES.get(row['status'], 'white')}]{row['status']}[/]{retries}",
            age_of(row["created_at"]),
            _cost_cell(row),
        )
    console.print(table)

    failures = [row for row in rows if row["error"]]
    for row in failures:
        console.print(f"[red]job {row['id']}[/] {row['error']}")
    if failures:
        console.print(f"[dim]`zipf jobs show {failures[0]['id']}` for the full record[/]")


@jobs_app.command("show")
def jobs_show(job_id: int = typer.Argument(..., help="Job to inspect.")) -> None:
    """Everything recorded about one job."""
    with connect(Paths.resolve().db_file, read_only=True) as conn:
        row = job_queue.get(conn, job_id)

    if row is None:
        console.print(f"[yellow]no job {job_id}[/] try `zipf jobs list`")
        return

    params = json.loads(row["params_json"])
    estimated, actual = row["estimated_cost"], row["actual_cost"]

    fields: list[tuple[str, str]] = [
        ("what", job_subject(params)),
        ("depth", job_depth(params)),
        ("capability", row["capability"]),
        ("status", f"[{_JOB_STYLES.get(row['status'], 'white')}]{row['status']}[/]"),
        ("attempts", str(row["attempts"])),
        ("estimated", f"${estimated:.5f}" if estimated is not None else "—"),
        ("actual", f"${actual:.5f}" if actual is not None else "—"),
    ]
    if estimated is not None and actual is not None:
        fields.append(("drift", f"${actual - estimated:+.5f}"))
    fields += [
        ("queued", f"{row['created_at']}  ({age_of(row['created_at'])} ago)"),
        ("started", row["started_at"] or "—"),
        ("finished", row["finished_at"] or "—"),
        ("took", elapsed_between(row["started_at"], row["finished_at"])),
        ("stored as", f"raw_response {row['raw_id']}" if row["raw_id"] else "—"),
    ]
    if row["vendor_task_id"]:
        fields.append(("vendor task", row["vendor_task_id"]))

    width = max(len(label) for label, _ in fields)
    body = "\n".join(f"  [dim]{label:<{width}}[/]  {value}" for label, value in fields)
    body += f"\n  [dim]{'params':<{width}}[/]  {json.dumps(params, sort_keys=True)}"
    if row["error"]:
        body += f"\n  [dim]{'error':<{width}}[/]  [red]{row['error']}[/]"

    console.print(Panel(body, title=f"job {job_id}", title_align="left", expand=False))


@jobs_app.command("cancel")
def jobs_cancel(
    job_id: int = typer.Argument(..., help="Job to cancel. Must not have started."),
) -> None:
    """Cancel a queued job."""
    with connect(Paths.resolve().db_file) as conn:
        cancelled = job_queue.cancel(conn, job_id)
    if cancelled:
        console.print(f"[green]cancelled[/] job {job_id}")
    else:
        console.print(f"[yellow]not cancelled[/] job {job_id} is not queued")


#: Every table worth counting. Listed rather than derived: `observation` has no
#: projector until the SERP and LLM milestones, and a stats view that silently
#: omitted it would be misleading.
_TABLES = (
    "raw_response",
    "keyword",
    "keyword_month",
    "domain_keyword",
    "gsc_query",
    "observation",
    "job",
)


@db_app.command("stats")
def db_stats() -> None:
    """What the database holds. Free, and needs no sqlite3."""
    paths = Paths.resolve()
    with connect(paths.db_file, read_only=True) as conn:
        counts = {name: count_rows(conn, name) for name in _TABLES}
        summary = conn.execute(
            "SELECT COUNT(*) AS responses, COUNT(DISTINCT capability) AS sources, "
            "COALESCE(SUM(cost_usd), 0.0) AS paid, MIN(fetched_at) AS oldest "
            "FROM raw_response"
        ).fetchone()
        prunable = prune_cache.preview(conn)

    size_mb = paths.db_file.stat().st_size / 1_048_576
    console.print(f"database  {paths.db_file}  [dim]{size_mb:.2f} MB[/]")
    console.print(
        f"stored    {plural(summary['responses'], 'response')} "
        f"from {plural(summary['sources'], 'source')} · "
        f"[bold]${summary['paid']:.5f}[/] paid to date"
    )
    if summary["oldest"]:
        console.print(f"oldest    {summary['oldest']}  [dim]({age_of(summary['oldest'])} ago)[/]")
    # Only mentioned when there is something to do about it. A line reading
    # "0 prunable" every time trains you to stop reading the block.
    if not prunable.is_empty:
        console.print(
            f"prunable  {plural(prunable.rows, 'free response')} "
            f"[dim]{prunable.bytes / 1_048_576:.2f} MB · `zipf db prune`[/]"
        )

    table = Table(show_header=True)
    table.add_column("table")
    table.add_column("rows", justify="right")
    for name, count in counts.items():
        table.add_row(name, f"{count:,}" if count else "[dim]0[/]")
    console.print(table)

    _effect(f"{plural(sum(counts.values()), 'row')} across {len(_TABLES)} tables", "$0.00")


@app.command()
def budget(
    cached: bool = typer.Option(False, "--cached", help="Skip the free vendor balance lookup."),
) -> None:
    """What you can still spend, and which limit is actually stopping you."""
    limits = _budget()

    async def work(conn: sqlite3.Connection) -> Any:
        return await budget_service.status(conn, limits, refresh=not cached)

    state = _with_db(work)

    # One headline. Two limits shown as equals read as confusion, so the smaller
    # of them leads and the rest explains it.
    console.print(
        f"[bold]${state.effective_limit:.2f} available[/]  "
        f"{meter(state.used_fraction)}  [dim]{state.used_fraction:.0%} used[/]"
    )

    if state.balance is None:
        console.print(f"  [dim]limited by your ${state.ceiling:.2f} monthly ceiling[/]")
    elif state.limited_by == "balance":
        console.print(
            f"  [yellow]limited by your DataForSEO balance[/][dim], not the "
            f"${state.ceiling:.2f} monthly ceiling[/]"
        )
    else:
        console.print(
            f"  [dim]limited by your ${state.ceiling:.2f} monthly ceiling, "
            f"not the ${state.balance:.2f} balance[/]"
        )

    console.print()
    console.print(f"  spent this month  ${state.spent:.5f} of ${state.ceiling:.2f}")
    if state.balance is not None:
        freshness = "live" if not cached else age_of_delta(state.balance_age)
        console.print(f"  vendor balance    ${state.balance:.2f} at DataForSEO [dim]{freshness}[/]")
    gate = "every spend" if state.threshold <= 0 else f"spends over ${state.threshold:.2f}"
    console.print(f"  confirms          {gate}")

    if state.balance_error:
        console.print(f"[yellow]balance lookup failed[/] {state.balance_error}")


def main() -> None:
    """Entry point that turns expected failures into a message and an exit code.

    ``typer.Exit`` is only meaningful inside a command, so this raises
    ``SystemExit`` directly; raising ``typer.Exit`` here would surface as an
    unhandled traceback rather than a message.
    """
    try:
        app()
    except ZipfError as exc:
        console.print(f"[red]error[/] {exc.problem}")
        if exc.fix:
            console.print(f"      [dim]{exc.fix}[/]")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
