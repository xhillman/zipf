"""Command line interface.

The CLI is a permanent peer of the TUI, not scaffolding (spec D2). Both are thin
shells over ``zipf.services``; neither owns logic.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Coroutine
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from zipf.budget import Budget
from zipf.config import DEFAULT_CONFIG_TOML, Paths, load_settings
from zipf.db.connection import connect
from zipf.db.migrate import migrate
from zipf.errors import CredentialMissingError, ZipfError
from zipf.projections.rebuild import rebuild as rebuild_projections
from zipf.services import gsc as gsc_service
from zipf.services import suggest as suggest_service
from zipf.sources import gsc as gsc_source

app = typer.Typer(
    name="zipf",
    help="Keyword research over a local SQLite cache.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(name="db", help="Database maintenance.", no_args_is_help=True)
gsc_app = typer.Typer(name="gsc", help="Search Console. Free, so refreshed greedily.")
app.add_typer(db_app)
app.add_typer(gsc_app)

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
def budget() -> None:
    """Month-to-date spend against the ceiling."""
    limits = _budget()
    with connect(Paths.resolve().db_file, read_only=True) as conn:
        spent = limits.spent_this_month(conn)

    pct = spent / limits.ceiling_usd if limits.ceiling_usd else 0.0
    filled = min(10, int(pct * 10))
    meter = "▓" * filled + "░" * (10 - filled)
    console.print(f"${spent:.2f}/${limits.ceiling_usd:.2f}  {meter}  {pct:.0%}")


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
