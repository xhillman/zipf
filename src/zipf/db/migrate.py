"""Forward-only migration runner.

Migrations are numbered ``.sql`` files applied in filename order and recorded in
``schema_migration``. There is no down path: this database holds paid data, and
the recovery story for a bad projection is a rebuild, never a rollback.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

MIGRATIONS_PACKAGE = "zipf.db.migrations"

# Migration filenames are inlined into SQL below, so the shape is constrained
# rather than trusted. They come from our own package directory, but a name that
# cannot be quoted safely should fail loudly rather than be escaped cleverly.
NAME_PATTERN = re.compile(r"^\d{3}_[a-z0-9_]+\.sql$")

_CREATE_LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migration (
  name        TEXT PRIMARY KEY,
  applied_at  TEXT NOT NULL
)
"""


class MigrationError(RuntimeError):
    """A migration file is malformed or could not be applied."""


@dataclass(frozen=True)
class Migration:
    name: str
    sql: str


def _load_migrations() -> list[Migration]:
    files = resources.files(MIGRATIONS_PACKAGE)
    names = sorted(f.name for f in files.iterdir() if f.name.endswith(".sql"))

    for name in names:
        if not NAME_PATTERN.match(name):
            raise MigrationError(f"migration filename {name!r} must match {NAME_PATTERN.pattern}")

    return [Migration(name=n, sql=(files / n).read_text(encoding="utf-8")) for n in names]


def _applied(conn: sqlite3.Connection) -> set[str]:
    conn.execute(_CREATE_LEDGER)
    rows = conn.execute("SELECT name FROM schema_migration").fetchall()
    return {row["name"] for row in rows}


def pending_names(conn: sqlite3.Connection) -> list[str]:
    """Migrations this database has not had applied, in order.

    Safe on a read-only connection, which ``_applied`` is not: that one creates
    the ledger table if it is absent, and creating a table is a write. The
    absence of the ledger is checked explicitly rather than by catching the
    error, so a genuinely broken database still raises.
    """
    ledger = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migration'"
    ).fetchone()

    applied: set[str] = set()
    if ledger is not None:
        applied = {row["name"] for row in conn.execute("SELECT name FROM schema_migration")}

    return [migration.name for migration in _load_migrations() if migration.name not in applied]


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply every unapplied migration. Returns the names applied, in order.

    Each migration and its ledger row commit together. ``executescript`` commits
    any pending transaction before it runs, so the transaction is declared inside
    the script rather than wrapped around the call.
    """
    already = _applied(conn)
    applied: list[str] = []

    for migration in _load_migrations():
        if migration.name in already:
            continue

        script = (
            "BEGIN;\n"
            f"{migration.sql}\n"
            "INSERT INTO schema_migration (name, applied_at) "
            f"VALUES ('{migration.name}', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"migration {migration.name} failed: {exc}") from exc

        applied.append(migration.name)

    return applied


def migrate_path(path: Path) -> list[str]:
    """Open the database at ``path`` and migrate it."""
    from zipf.db.connection import connect

    with connect(path) as conn:
        return migrate(conn)
