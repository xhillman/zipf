"""Shared fixtures.

Two guarantees this file exists to provide:

1. Every test runs against its own database under ``ZIPF_HOME``. Nothing touches
   the real one.
2. A default ``pytest`` run makes zero network calls and costs $0. Any request
   that is not explicitly mocked raises.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import respx

from zipf.budget import Budget
from zipf.db.connection import connect
from zipf.db.migrate import migrate


@pytest.fixture(autouse=True)
def zipf_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every path at a scratch directory and migrate a fresh database."""
    monkeypatch.setenv("ZIPF_HOME", str(tmp_path))
    # The repo .env holds real credentials; tests must not inherit them.
    for name in ("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    with connect(tmp_path / "zipf.db") as conn:
        migrate(conn)
    return tmp_path


@pytest.fixture(autouse=True)
def blocked_network(request: pytest.FixtureRequest) -> Iterator[respx.MockRouter | None]:
    """Fail any request that a test has not explicitly mocked.

    Tests marked ``live`` opt out, and are themselves skipped unless ZIPF_LIVE=1.
    """
    if request.node.get_closest_marker("live"):
        if os.environ.get("ZIPF_LIVE") != "1":
            pytest.skip("live test; set ZIPF_LIVE=1 to run against real vendors")
        yield None
        return

    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def db(zipf_home: Path) -> Iterator[sqlite3.Connection]:
    with connect(zipf_home / "zipf.db") as conn:
        yield conn


@pytest.fixture
def budget() -> Budget:
    return Budget(ceiling_usd=20.0, threshold_usd=0.25)
