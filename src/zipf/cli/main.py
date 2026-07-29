"""Command line interface.

The CLI is a permanent peer of the TUI, not scaffolding (spec D2). Both are thin
shells over ``zipf.services``; neither owns logic.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from zipf.budget import Budget
from zipf.config import DEFAULT_CONFIG_TOML, Paths, load_settings
from zipf.db.connection import connect
from zipf.db.migrate import migrate
from zipf.errors import ZipfError
from zipf.services.suggest import suggest as suggest_service

app = typer.Typer(
    name="zipf",
    help="Keyword research over a local SQLite cache.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _budget() -> Budget:
    settings = load_settings()
    return Budget(
        ceiling_usd=settings.monthly_ceiling_usd,
        threshold_usd=settings.confirm_threshold_usd,
    )


@app.command()
def init() -> None:
    """Create the database, run migrations, and scaffold a config file."""
    paths = Paths.resolve()
    paths.ensure()

    with connect(paths.db_file) as conn:
        applied = migrate(conn)

    if not paths.config_file.exists():
        paths.config_file.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        console.print(f"config   [green]created[/]  {paths.config_file}")
    else:
        console.print(f"config   [dim]exists[/]   {paths.config_file}")

    state = f"[green]{len(applied)} migration(s)[/]" if applied else "[dim]up to date[/]"
    console.print(f"database {state}  {paths.db_file}")


@app.command()
def suggest(
    seed: str = typer.Argument(..., help="Seed keyword to expand."),
    country: str = typer.Option("us", "--country", help="Two-letter market code."),
    lang: str = typer.Option("en", "--lang", help="Two-letter language code."),
    force: bool = typer.Option(False, "--force", help="Ignore a fresh cache entry."),
) -> None:
    """Keyword suggestions from Google Autocomplete. Tier 0, free."""
    paths = Paths.resolve()

    async def run() -> None:
        with connect(paths.db_file) as conn:
            result = await suggest_service(
                conn, seed, budget=_budget(), lang=lang, country=country, force=force
            )

        origin = (
            f"[dim]cache · {result.age_days:.1f}d old[/]" if result.cached else "[cyan]fetched[/]"
        )
        table = Table(title=f"{seed}  ({len(result.suggestions)} suggestions · {origin})")
        table.add_column("#", justify="right", style="dim")
        table.add_column("suggestion")
        for i, suggestion in enumerate(result.suggestions, 1):
            table.add_row(str(i), suggestion)
        console.print(table)

    asyncio.run(run())


@app.command()
def budget() -> None:
    """Month-to-date spend against the ceiling."""
    paths = Paths.resolve()
    limits = _budget()
    with connect(paths.db_file, read_only=True) as conn:
        spent = limits.spent_this_month(conn)

    pct = spent / limits.ceiling_usd if limits.ceiling_usd else 0.0
    filled = min(10, int(pct * 10))
    meter = "▓" * filled + "░" * (10 - filled)
    console.print(f"${spent:.2f}/${limits.ceiling_usd:.2f}  {meter}  {pct:.0%}")


def main() -> None:
    """Entry point that turns expected failures into a message and an exit code."""
    try:
        app()
    except ZipfError as exc:
        console.print(f"[red]error[/] {exc}")
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    main()
