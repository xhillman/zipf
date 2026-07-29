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

#: The only modules permitted to send. Everything else may build an
#: ``httpx.Request`` and hand it to ``fetch``.
#:
#: ``google_oauth`` is the one documented exception (spec §14): it exchanges
#: credentials, returns no vendor data, and costs nothing. Adding to this list
#: means adding an unmetered path to the network, so it is deliberately a
#: hard-coded list rather than a marker a module can grant itself.
SENDERS_ALLOWED = {"fetch.py", "sources/google_oauth.py"}

#: Every httpx entry point that performs I/O.
SENDING_NAMES = {
    "AsyncClient",
    "Client",
    "request",
    "stream",
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
}


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _httpx_sends(tree: ast.AST) -> set[str]:
    """Names of httpx calls that would perform I/O."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Only attribute access on the httpx module counts: `request.get(...)`
        # on some other object is not a network call.
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "httpx"
            and func.attr in SENDING_NAMES
        ):
            found.add(f"httpx.{func.attr}")
    return found


def test_r1_only_the_metered_door_reaches_the_network() -> None:
    """R1: one metered door, no side entrances.

    Adapters may build an ``httpx.Request``; only an allowlisted module may send.
    """
    offenders: dict[str, set[str]] = {}
    for path in _python_files():
        relative = path.relative_to(SRC).as_posix()
        if relative in SENDERS_ALLOWED:
            continue
        sends = _httpx_sends(ast.parse(path.read_text()))
        if sends:
            offenders[relative] = sends

    assert offenders == {}, f"network calls outside the allowlist: {offenders}"


def test_r1_detector_is_not_vacuous() -> None:
    """A test that can never fail protects nothing."""
    planted = ast.parse("import httpx\nhttpx.post('https://example.com')")
    assert _httpx_sends(planted) == {"httpx.post"}

    innocent = ast.parse("import httpx\nr = httpx.Request('GET', 'https://example.com')")
    assert _httpx_sends(innocent) == set()


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
