"""What the four paid commands print, pinned exactly as they print it today.

These are characterization tests. They exist because H5 rewrites the control flow
shared by ``vol``, ``gap``, ``ideas`` and ``ranks`` — eight copies of one eight-step
sequence — and ``cli/main.py`` had no test of any kind before this file.

They record the contract; they do not endorse it. Where today's output is wrong,
it is pinned wrong on purpose and the test says so, because a refactor must be
provably behaviour-preserving before anything is corrected. Fixing a defect and
restructuring the code that produces it are two changes, and doing them together
means neither can be reviewed.

Three things are asserted for every path:

1. **The closing effect line**, which is what a user actually reads.
2. **That nothing was spent.** No paid command may reach a vendor: they price,
   confirm and enqueue, and the runner is the only thing that spends (R5).
3. **What reached the queue**, so a declined prompt is provably distinct from an
   approved one rather than merely printing differently.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zipf.cli.main import app
from zipf.clock import now_iso
from zipf.projections.rebuild import project

#: Wide enough that Rich never wraps the effect line. A wrapped line would split
#: across two entries and the assertion would be testing the terminal width.
CONSOLE_WIDTH = "200"


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", CONSOLE_WIDTH)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _envelope(items: list[dict[str, object]], *, nested: bool = True) -> bytes:
    """A DataForSEO success envelope. Labs nests ``items``; Google Ads does not."""
    result = [{"items": items}] if nested else items
    return json.dumps(
        {"status_code": 20000, "cost": 0.012, "tasks": [{"status_code": 20000, "result": result}]}
    ).encode()


def _keyword_block(keyword: str, volume: int) -> dict[str, object]:
    return {
        "keyword": keyword,
        "keyword_info": {"search_volume": volume, "cpc": 2.5, "competition": 0.4},
    }


def _store(
    conn: sqlite3.Connection, capability: str, params: dict[str, object], body: bytes
) -> int:
    cursor = conn.execute(
        "INSERT INTO raw_response (capability, params_hash, params_json, body, cost_usd, "
        "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        (capability, capability + json.dumps(params), json.dumps(params), body, 0.012, now_iso()),
    )
    return int(cursor.lastrowid or 0)


@pytest.fixture
def owned(db: sqlite3.Connection) -> sqlite3.Connection:
    """One stored pull per paid capability, so the cached path has something to read.

    Projected as well as stored: the cached path prints a table, and a fixture
    that only stored bytes would exercise the freshness check without the read.
    """
    project(
        db,
        _store(
            db,
            "labs.search_volume",
            {"keywords": ["free crm"]},
            _envelope([_keyword_block("free crm", 9900)]),
        ),
    )
    project(
        db,
        _store(
            db,
            "labs.domain_intersection",
            {"target1": "rival.com", "target2": "mine.com", "limit": 100, "intersections": False},
            _envelope(
                [
                    {
                        "keyword_data": _keyword_block("crm pricing", 5000),
                        "first_domain_serp_element": {
                            "rank_absolute": 4,
                            "url": "https://rival.com/a",
                        },
                    }
                ]
            ),
        ),
    )
    project(
        db,
        _store(
            db,
            "labs.ranked_keywords",
            {"domain": "mine.com", "limit": 100},
            _envelope(
                [
                    {
                        "keyword_data": _keyword_block("crm tools", 700),
                        "ranked_serp_element": {
                            "serp_item": {"rank_absolute": 8, "url": "https://mine.com/b"}
                        },
                    }
                ]
            ),
        ),
    )
    project(
        db,
        _store(
            db,
            "keywords_data.keywords_for_keywords",
            {"seeds": ["crm"]},
            _envelope(
                [
                    {
                        "keyword": "crm software",
                        "search_volume": 8100,
                        "cpc": 3.0,
                        "competition_index": 50,
                    }
                ],
                nested=False,
            ),
        ),
    )
    return db


def _effect_line(stdout: str) -> str:
    """The closing ``→`` line, which every command ends with (spec U4)."""
    lines = [line for line in stdout.splitlines() if line.startswith("→")]
    assert len(lines) == 1, f"expected exactly one effect line, got {len(lines)}:\n{stdout}"
    return " ".join(lines[0].removeprefix("→").split())


def _spent(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0.0) AS spent FROM raw_response").fetchone()
    return float(row["spent"])


def _queued(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT capability FROM job ORDER BY id").fetchall()
    return [row["capability"] for row in rows]


#: command line, stdin, the phrase that identifies the path, the effect line.
#: Read off the real CLI on 2026-08-01 and pinned verbatim.
CASES = [
    pytest.param(
        ["vol", "free crm"],
        None,
        "all cached nothing to buy",
        "1 keyword asked for · 1 measured · 1 already fresh · $0.00",
        id="vol-cached",
    ),
    pytest.param(
        ["vol", "brand new kw", "--dry-run"],
        None,
        "dry run would buy 1 keyword(s) for $0.01212 · spent $0.00",
        "1 keyword asked for · 0 measured · 0 already fresh · $0.00",
        id="vol-dry-run",
    ),
    pytest.param(
        ["vol", "brand new kw"],
        "n\n",
        "cancelled nothing queued, nothing spent",
        "1 keyword asked for · 0 measured · 0 already fresh · $0.00",
        id="vol-declined",
    ),
    pytest.param(
        ["vol", "brand new kw"],
        "y\n",
        "queued job(s) 1",
        "1 keyword asked for · 0 measured · 0 already fresh · $0.00",
        id="vol-queued",
    ),
    pytest.param(
        ["gap", "rival.com", "--mine", "mine.com"],
        None,
        "cached pulled 0.0d ago · nothing to buy · $0.00",
        "1 distinct query from 1 row · rival.com not matched by mine.com · $0.00",
        id="gap-cached",
    ),
    pytest.param(
        ["gap", "other.com", "--mine", "mine.com", "--dry-run"],
        None,
        "dry run would buy up to 100 rows for $0.02400 · spent $0.00",
        "0 distinct queries from 0 rows · other.com not matched by mine.com · $0.00",
        id="gap-dry-run",
    ),
    pytest.param(
        ["gap", "other.com", "--mine", "mine.com"],
        "n\n",
        "cancelled nothing queued, nothing spent",
        "0 distinct queries from 0 rows · other.com not matched by mine.com · $0.00",
        id="gap-declined",
    ),
    pytest.param(
        ["gap", "other.com", "--mine", "mine.com"],
        "y\n",
        "queued job 1",
        "0 distinct queries from 0 rows · other.com not matched by mine.com · $0.00",
        id="gap-queued",
    ),
    pytest.param(
        ["ranks", "mine.com"],
        None,
        "cached pulled 0.0d ago · nothing to buy · $0.00",
        "1 distinct query from 1 row · 1 ranked · $0.00",
        id="ranks-cached",
    ),
    pytest.param(
        ["ranks", "other.com", "--dry-run"],
        None,
        "dry run would buy up to 100 rows for $0.02400 · spent $0.00",
        "0 distinct queries from 0 rows · 0 ranked · $0.00",
        id="ranks-dry-run",
    ),
    pytest.param(
        ["ranks", "other.com"],
        "n\n",
        "cancelled nothing queued, nothing spent",
        "0 distinct queries from 0 rows · 0 ranked · $0.00",
        id="ranks-declined",
    ),
    pytest.param(
        ["ranks", "other.com"],
        "y\n",
        "queued job 1",
        "0 distinct queries from 0 rows · 0 ranked · $0.00",
        id="ranks-queued",
    ),
    pytest.param(
        ["ideas", "crm"],
        None,
        # The seed list that covered this pull is missing from the line: main.py
        # wraps it in square brackets, which Rich parses as a markup tag and
        # swallows. Pinned as it prints. See the dedicated test below.
        "cached pulled 0.0d ago from  · $0.00",
        "1 keyword shown · 0 seasonal · $0.00",
        id="ideas-cached",
    ),
    pytest.param(
        ["ideas", "widgets", "--dry-run"],
        None,
        "dry run would buy up to 20,000 keywords for $0.09 · spent $0.00",
        "0 keywords shown · 0 seasonal · $0.00",
        id="ideas-dry-run",
    ),
    pytest.param(
        ["ideas", "widgets"],
        "n\n",
        "cancelled nothing queued, nothing spent",
        "0 keywords shown · 0 seasonal · $0.00",
        id="ideas-declined",
    ),
    pytest.param(
        ["ideas", "widgets"],
        "y\n",
        "queued job 1",
        "0 keywords shown · 0 seasonal · $0.00",
        id="ideas-queued",
    ),
]


@pytest.mark.parametrize(("args", "stdin", "phrase", "effect"), CASES)
def test_paid_command_output_is_unchanged(
    runner: CliRunner,
    owned: sqlite3.Connection,
    args: list[str],
    stdin: str | None,
    phrase: str,
    effect: str,
) -> None:
    before = _spent(owned)

    result = runner.invoke(app, args, input=stdin)

    assert result.exit_code == 0, result.stdout
    assert phrase in result.stdout, f"the path was not taken as expected:\n{result.stdout}"
    assert _effect_line(result.stdout) == effect
    assert _spent(owned) == before, "a paid command reached a vendor; only the runner may spend"


@pytest.mark.parametrize(
    ("args", "capability"),
    [
        (["vol", "brand new kw"], "labs.search_volume"),
        (["gap", "other.com", "--mine", "mine.com"], "labs.domain_intersection"),
        (["ranks", "other.com"], "labs.ranked_keywords"),
        (["ideas", "widgets"], "keywords_data.keywords_for_keywords"),
    ],
)
def test_approving_enqueues_and_declining_does_not(
    runner: CliRunner, owned: sqlite3.Connection, args: list[str], capability: str
) -> None:
    """The prompt is the only thing standing between a plan and a queued job."""
    assert runner.invoke(app, args, input="n\n").exit_code == 0
    assert _queued(owned) == [], "a declined prompt still queued work"

    assert runner.invoke(app, args, input="y\n").exit_code == 0
    assert _queued(owned) == [capability]
    assert _spent(owned) == pytest.approx(0.048), "queueing is not spending"


@pytest.mark.parametrize(
    "args",
    [
        ["vol", "free crm"],
        ["gap", "rival.com", "--mine", "mine.com"],
        ["ranks", "mine.com"],
        ["ideas", "crm"],
    ],
)
def test_reading_owned_data_never_prompts(
    runner: CliRunner, owned: sqlite3.Connection, args: list[str]
) -> None:
    """U1's rule, which the TUI inherited: what you already paid for is free to read.

    Run with empty stdin. A command that tried to prompt would read EOF and fail,
    so this passing is itself the assertion that nothing asked.
    """
    result = runner.invoke(app, args, input="")

    assert result.exit_code == 0, result.stdout
    assert "pull [y/N]" not in result.stdout
    assert _queued(owned) == []


def test_ideas_loses_the_covering_seeds_to_rich_markup(
    runner: CliRunner, owned: sqlite3.Connection
) -> None:
    """A defect, pinned rather than fixed.

    ``main.py`` builds ``f"...from [{covered}] · $0.00"``. Rich reads ``[crm]``
    as a markup tag and removes it, so the line that exists to say *which seeds
    already cover this request* names none of them. Every other command's cached
    line survives because none of them brackets a value.

    Pinned here so H5 cannot change it silently. Fixing it is a separate change:
    the bracket needs escaping, or the value belongs outside markup.
    """
    result = runner.invoke(app, ["ideas", "crm"], input="")

    assert "cached pulled 0.0d ago from  · $0.00" in result.stdout
    assert "crm" not in result.stdout.split("cached pulled")[1].split("\n")[0]


@pytest.mark.parametrize(
    "args",
    [
        ["vol", "free crm"],
        ["gap", "rival.com", "--mine", "mine.com"],
        ["ranks", "mine.com"],
        ["ideas", "crm"],
    ],
)
def test_the_effect_line_always_states_a_cost(
    runner: CliRunner, owned: sqlite3.Connection, args: list[str]
) -> None:
    """U4: cost is always stated, including when it is zero.

    "$0.00" is information and its absence is not, so a command that ended
    without naming a figure would be a regression however tidy it looked.
    """
    result = runner.invoke(app, args, input="")

    assert result.exit_code == 0, result.stdout
    assert _effect_line(result.stdout).endswith("$0.00")


def test_the_database_is_untouched_by_a_dry_run(
    runner: CliRunner, owned: sqlite3.Connection, zipf_home: Path
) -> None:
    """--dry-run prints the bill and changes nothing at all."""
    before = (
        _spent(owned),
        owned.execute("SELECT COUNT(*) AS n FROM raw_response").fetchone()["n"],
        _queued(owned),
    )

    for args in (
        ["vol", "brand new kw", "--dry-run"],
        ["gap", "other.com", "--mine", "mine.com", "--dry-run"],
        ["ranks", "other.com", "--dry-run"],
        ["ideas", "widgets", "--dry-run"],
    ):
        assert runner.invoke(app, args, input="").exit_code == 0

    after = (
        _spent(owned),
        owned.execute("SELECT COUNT(*) AS n FROM raw_response").fetchone()["n"],
        _queued(owned),
    )
    assert before == after
