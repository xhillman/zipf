"""Command line interface.

The CLI is a permanent peer of the TUI, not scaffolding (spec D2). Both are thin
shells over ``zipf.services``; neither owns logic.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from zipf.budget import Budget
from zipf.cli.paid import confirm_spend
from zipf.config import DEFAULT_CONFIG_TOML, Paths, load_settings
from zipf.db.connection import connect
from zipf.db.migrate import migrate
from zipf.errors import CredentialMissingError, ZipfError
from zipf.jobs import queue as job_queue
from zipf.jobs.runner import JobRunner
from zipf.projections.rebuild import rebuild as rebuild_projections
from zipf.services import budget as budget_service
from zipf.services import gap as gap_service
from zipf.services import gsc as gsc_service
from zipf.services import suggest as suggest_service
from zipf.services import volume as volume_service
from zipf.services.cluster import Cluster
from zipf.sources import gsc as gsc_source

app = typer.Typer(
    name="zipf",
    help="Keyword research over a local SQLite cache.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(name="db", help="Database maintenance.", no_args_is_help=True)
gsc_app = typer.Typer(name="gsc", help="Search Console. Free, so refreshed greedily.")
jobs_app = typer.Typer(name="jobs", help="The work queue. Paid work runs here, never inline.")
app.add_typer(db_app)
app.add_typer(gsc_app)
app.add_typer(jobs_app)

console = Console()


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
    report = asyncio.run(runner.drain())
    console.print(
        f"[green]{report.done} done[/] · {report.failed} failed · {report.claimed} claimed"
    )


def _number(value: float | None, fmt: str = ",") -> str:
    return f"{value:{fmt}}" if value is not None else "[dim]—[/]"


def _money(value: float | None) -> str:
    return f"${value:.2f}" if value is not None else "[dim]—[/]"


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
            _number(cluster.volume),
            _money(best.get("cpc")),
            _number(best.get("competition"), ".2f"),
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
            _number(row["volume"]),
            _money(row["cpc"]),
            _number(row["competition"], ".2f"),
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


@app.command()
def init() -> None:
    """Create the database, run migrations, and scaffold a config file."""
    paths = Paths.resolve()
    paths.ensure()

    with connect(paths.db_file) as conn:
        applied = migrate(conn)

    if paths.config_file.exists():
        console.print(f"config   [dim]exists[/]   {paths.config_file}")
    else:
        paths.config_file.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        console.print(f"config   [green]created[/]  {paths.config_file}")

    state = f"[green]{len(applied)} migration(s)[/]" if applied else "[dim]up to date[/]"
    console.print(f"database {state}  {paths.db_file}")


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

    async def work(conn: sqlite3.Connection) -> suggest_service.ExpansionResult:
        return await suggest_service.expand(
            conn,
            seed,
            budget=_budget(),
            questions=questions,
            alphabet=alphabet,
            lang=lang,
            country=country,
            force=force,
        )

    result = _with_db(work)
    terms = result.suggestions

    table = Table(title=f"{seed} · {len(terms)} suggestions")
    table.add_column("#", justify="right", style="dim")
    table.add_column("suggestion")
    for i, term in enumerate(terms[:limit], 1):
        table.add_row(str(i), term)
    console.print(table)

    console.print(
        f"[dim]{len(result.results)} seeds · {result.cache_hits} from cache · "
        f"{len(terms)} distinct · $0.00[/]"
    )
    if len(terms) > limit:
        console.print(f"[dim]showing {limit}; all {len(terms)} are stored[/]")
    for bad_seed, reason in result.failures.items():
        console.print(f"[yellow]skipped[/] {bad_seed}: {reason}")


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
        raise CredentialMissingError(capability="gsc.search_analytics", variable="gsc_site_url")

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


@db_app.command("rebuild")
def db_rebuild(
    capability: str = typer.Option(None, "--capability", help="Rebuild one capability only."),
) -> None:
    """Drop projection tables and replay every stored response. Free."""
    with connect(Paths.resolve().db_file) as conn:
        stats = rebuild_projections(conn, capability)

    console.print(
        f"[green]rebuilt[/] {', '.join(stats.tables_cleared)} from "
        f"{stats.rows_replayed:,} stored response(s) → {stats.rows_written:,} row(s)"
    )
    console.print(f"[dim]capabilities replayed: {', '.join(stats.capabilities)}[/]")


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
) -> None:
    """Search volume for keywords. Tier 1, paid.

    Pass every keyword you care about in one call: Labs charges a per-call base
    that dwarfs the per-keyword cost, so many small calls are the expensive
    mistake.
    """

    def run(conn: sqlite3.Connection) -> None:
        plan = volume_service.plan(conn, keywords, force=force)
        console.print(
            f"[dim]{len(plan.requested)} keyword(s) · {len(plan.cached)} still fresh · "
            f"{len(plan.stale)} to buy in {len(plan.batches)} call(s)[/]"
        )

        if plan.is_free:
            console.print("[green]all cached[/] nothing to buy")
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
            return

        rows = volume_service.read_rows(conn, plan.requested)
        if flat:
            _print_volumes_flat(rows)
        else:
            _print_volumes(volume_service.read(conn, plan.requested), len(rows))

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
) -> None:
    """Keywords a competitor ranks for and you do not. Tier 1, paid."""
    own = mine or load_settings().own_domain
    if not own:
        raise CredentialMissingError(capability="labs.domain_intersection", variable="own_domain")

    def run(conn: sqlite3.Connection) -> None:
        plan = gap_service.plan(competitor, own, limit=limit)

        if dry_run:
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
            return

        rows = gap_service.read_rows(conn, plan.competitor, plan.mine)
        if flat:
            _print_gap_flat(rows)
        else:
            _print_gap(gap_service.read(conn, plan.competitor, plan.mine), len(rows))

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
    for column in ("id", "capability", "status", "try", "est", "actual"):
        table.add_column(column)
    styles = {"done": "green", "failed": "red", "queued": "yellow", "running": "cyan"}
    for row in rows:
        actual = f"${row['actual_cost']:.5f}" if row["actual_cost"] is not None else "—"
        estimate = f"${row['estimated_cost']:.5f}" if row["estimated_cost"] is not None else "—"
        table.add_row(
            str(row["id"]),
            row["capability"],
            f"[{styles.get(row['status'], 'white')}]{row['status']}[/]",
            str(row["attempts"]),
            estimate,
            actual,
        )
    console.print(table)
    for row in rows:
        if row["error"]:
            console.print(f"[red]job {row['id']}[/] {row['error']}")


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


def _meter(fraction: float, width: int = 10) -> str:
    filled = min(width, max(0, int(fraction * width)))
    return "▓" * filled + "░" * (width - filled)


@app.command()
def budget(
    cached: bool = typer.Option(False, "--cached", help="Skip the free vendor balance lookup."),
) -> None:
    """Spend against the ceiling, and the vendor balance that actually limits it."""
    limits = _budget()

    async def work(conn: sqlite3.Connection) -> Any:
        return await budget_service.status(conn, limits, refresh=not cached)

    state = _with_db(work)

    used = state.spent / state.ceiling if state.ceiling else 0.0
    console.print(
        f"spent    ${state.spent:.2f}/${state.ceiling:.2f}  {_meter(used)}  {used:.0%} of ceiling"
    )

    if state.balance is not None:
        age = "" if cached else " [dim]live[/]"
        if state.balance_age and state.balance_age.total_seconds() > 60:
            age = f" [dim]{state.balance_age.total_seconds() / 60:.0f}m old[/]"
        console.print(f"balance  ${state.balance:.2f} at DataForSEO{age}")

    if state.ceiling_exceeds_balance:
        console.print(
            f"[yellow]ceiling is decorative[/] ${state.ceiling:.2f} allowed but only "
            f"${state.balance:.2f} funded; the real stop is ${state.effective_limit:.2f}"
        )

    gate = "every spend" if state.threshold <= 0 else f"spends over ${state.threshold:.2f}"
    console.print(f"[dim]confirms {gate}[/]")

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
        console.print(f"[red]error[/] {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
