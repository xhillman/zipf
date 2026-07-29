"""The invariants from PRD §10, enforced as tests.

Each of these exists because breaking it produces either a surprise bill or an
unrecoverable gap in the record. Conventions do not survive a tired evening;
tests do.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "zipf"
METERED_DOOR = SRC / "fetch.py"


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _instantiates_async_client(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "AsyncClient":
            return True
        if isinstance(func, ast.Name) and func.id == "AsyncClient":
            return True
    return False


def test_r1_only_fetch_opens_the_network() -> None:
    """R1: one metered door, no side entrances.

    Adapters may build an ``httpx.Request``; only ``fetch`` may send one.
    """
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in _python_files()
        if path != METERED_DOOR and _instantiates_async_client(ast.parse(path.read_text()))
    ]
    assert offenders == [], f"httpx.AsyncClient instantiated outside fetch.py: {offenders}"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE raw_response SET cost_usd = 0.0",
        "DELETE FROM raw_response",
        "UPDATE raw_response SET body = x'ff' WHERE id = 1",
    ],
)
def test_r2_raw_response_is_append_only(db: sqlite3.Connection, statement: str) -> None:
    """R2 and R4: the only thing here that was paid for cannot be edited or pruned."""
    db.execute(
        "INSERT INTO raw_response "
        "(capability, params_hash, params_json, body, cost_usd, fetched_at) "
        "VALUES ('t', 'h', '{}', x'00', 1.5, '2026-07-01T00:00:00Z')"
    )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute(statement)

    row = db.execute("SELECT cost_usd FROM raw_response").fetchone()
    assert row["cost_usd"] == 1.5


def test_r2_inserts_are_still_allowed(db: sqlite3.Connection) -> None:
    """Append-only means append. The triggers must not block the append."""
    db.execute(
        "INSERT INTO raw_response "
        "(capability, params_hash, params_json, body, cost_usd, fetched_at) "
        "VALUES ('t', 'h', '{}', x'00', 0.0, '2026-07-01T00:00:00Z')"
    )
    assert db.execute("SELECT COUNT(*) AS n FROM raw_response").fetchone()["n"] == 1
