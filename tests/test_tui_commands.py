"""The `:` commands.

The registry takes a ``Context``, so these run without Textual: a fake context
that always approves, and one that always declines, cover both sides of the
confirmation gate. The invariant that matters most is asserted directly — no
command writes a ``raw_response`` row, because commands enqueue and the runner
spends (R5).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

import httpx
import pytest
import respx

from zipf.budget import Budget
from zipf.clock import now_iso
from zipf.errors import InvalidRequestError
from zipf.jobs import queue
from zipf.pricing import PriceEstimate
from zipf.projections.rebuild import count_rows
from zipf.sources.autocomplete import ENDPOINT
from zipf.tui import commands, views


def _stored(conn: sqlite3.Connection, capability: str, params: str) -> int:
    """Record a vendor response, the way a completed fetch would have."""
    cursor = conn.execute(
        "INSERT INTO raw_response (capability, params_hash, params_json, body, cost_usd, "
        "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        (capability, f"h{params}", params, b"{}", 0.05, now_iso()),
    )
    return int(cursor.lastrowid or 0)


@dataclass
class FakeContext:
    """A command context that answers the confirmation without a screen."""

    read: sqlite3.Connection
    write: sqlite3.Connection
    budget: Budget = field(default_factory=lambda: Budget(ceiling_usd=20.0, threshold_usd=0.25))
    own_domain: str | None = "mine.com"
    approve: bool = True
    asked: list[str] = field(default_factory=list)

    async def confirm(self, estimate: PriceEstimate, *, what: str, detail: str = "") -> bool:
        self.asked.append(what)
        return self.approve


@pytest.fixture
def context(db: sqlite3.Connection) -> FakeContext:
    return FakeContext(read=db, write=db)


def test_parsing_keeps_quoted_keywords_whole() -> None:
    command, args = commands.parse(':vol "best crm software" "free crm"')
    assert command.name == "vol"
    assert args == ["best crm software", "free crm"]


def test_the_leading_colon_is_optional() -> None:
    assert commands.parse("budget")[0].name == "budget"
    assert commands.parse(":budget")[0].name == "budget"


def test_aliases_resolve_to_one_command() -> None:
    assert commands.parse(":q")[0].name == "quit"
    assert commands.parse(":exit")[0].name == "quit"


def test_an_unknown_command_lists_the_real_ones() -> None:
    with pytest.raises(InvalidRequestError) as caught:
        commands.parse(":frobnicate")
    assert ":gap" in str(caught.value.fix)


def test_unbalanced_quotes_are_reported_not_raised_raw() -> None:
    """shlex raises ValueError; the user needs a sentence, not a traceback."""
    with pytest.raises(InvalidRequestError) as caught:
        commands.parse(':vol "unclosed')
    assert "quotes" in str(caught.value.fix)


def test_flags_are_pulled_out_of_the_arguments() -> None:
    value, rest = commands.take_flag(["ahrefs.com", "--mine", "me.com"], "--mine")
    assert value == "me.com"
    assert rest == ["ahrefs.com"]

    switch, rest = commands.take_switch(["a", "--force", "b"], "--force")
    assert switch is True
    assert rest == ["a", "b"]


def test_a_flag_without_a_value_says_so() -> None:
    with pytest.raises(InvalidRequestError):
        commands.take_flag(["ahrefs.com", "--mine"], "--mine")


async def test_a_command_with_no_arguments_gives_an_example(context: FakeContext) -> None:
    with pytest.raises(InvalidRequestError) as caught:
        await commands.execute(context, ":gap")
    assert ":gap ahrefs.com" in str(caught.value.fix)


async def test_gap_without_a_domain_of_your_own_names_both_remedies(
    db: sqlite3.Connection,
) -> None:
    context = FakeContext(read=db, write=db, own_domain=None)
    with pytest.raises(InvalidRequestError) as caught:
        await commands.execute(context, ":gap ahrefs.com")
    assert "--mine" in str(caught.value.fix)
    assert "own_domain" in str(caught.value.fix)


async def test_gap_asks_before_spending_and_enqueues_when_approved(
    context: FakeContext,
) -> None:
    outcome = await commands.execute(context, ":gap ahrefs.com")
    assert context.asked == ["keyword gap · ahrefs.com"]
    assert "queued job" in outcome.message
    assert context.write.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"] == 1


async def test_declining_queues_nothing(db: sqlite3.Connection) -> None:
    context = FakeContext(read=db, write=db, approve=False)
    outcome = await commands.execute(context, ":gap ahrefs.com")
    assert "nothing queued, nothing spent" in outcome.message
    assert context.write.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"] == 0


async def test_no_command_spends_synchronously(context: FakeContext) -> None:
    """R5: a paid command enqueues and returns. Only the runner calls a vendor."""
    before = count_rows(context.write, "raw_response")
    await commands.execute(context, ":gap ahrefs.com")
    await commands.execute(context, ':vol "best crm software"')
    assert count_rows(context.write, "raw_response") == before


async def test_a_gap_already_stored_is_read_without_asking(context: FakeContext) -> None:
    """Reading what you own is free and promptless, as the CLI has been since U1."""
    _stored(
        context.write,
        "labs.domain_intersection",
        '{"target1": "ahrefs.com", "target2": "mine.com", "limit": 100, "intersections": 0}',
    )
    outcome = await commands.execute(context, ":gap ahrefs.com")
    assert context.asked == []  # never priced, never prompted
    assert "$0.00" in outcome.message
    assert outcome.view == views.View(views.GAP, "ahrefs.com|mine.com")


async def test_vol_for_keywords_already_owned_costs_nothing(context: FakeContext) -> None:
    """Freshness needs a *measured* keyword, not merely a known one.

    The keyword is linked to a ``labs.search_volume`` response deliberately: a
    keyword that autocomplete only suggested must still be priced when asked for,
    which is the guard ``fresh_keywords`` exists to provide.
    """
    raw_id = _stored(context.write, "labs.search_volume", '{"keywords": ["free crm"]}')
    context.write.execute(
        "INSERT INTO keyword (keyword, volume, updated_at, raw_id) VALUES (?, ?, ?, ?)",
        ("free crm", 9900, now_iso(), raw_id),
    )
    outcome = await commands.execute(context, ':vol "free crm"')
    assert context.asked == []
    assert "$0.00" in outcome.message


async def test_a_merely_suggested_keyword_is_still_priced(context: FakeContext) -> None:
    """The other half of the same rule: known is not the same as measured."""
    raw_id = _stored(context.write, "autocomplete.suggest", '{"seed": "crm"}')
    context.write.execute(
        "INSERT INTO keyword (keyword, updated_at, raw_id) VALUES (?, ?, ?)",
        ("free crm", now_iso(), raw_id),
    )
    await commands.execute(context, ':vol "free crm"')
    assert context.asked == ["search volume · 1 keyword"]


async def test_limit_must_be_a_positive_number(context: FakeContext) -> None:
    with pytest.raises(InvalidRequestError) as caught:
        await commands.execute(context, ":gap ahrefs.com --limit nonsense")
    assert "--limit 100" in str(caught.value.fix)


async def test_quit_reports_that_it_exits(context: FakeContext) -> None:
    assert (await commands.execute(context, ":q")).exits is True


async def test_help_marks_which_commands_cost_money(context: FakeContext) -> None:
    message = (await commands.execute(context, ":help")).message
    assert ":gap*" in message and ":vol*" in message
    assert ":help*" not in message  # free commands carry no marker


async def test_suggest_stores_keywords_and_reports_the_cost(
    context: FakeContext, blocked_network: respx.MockRouter
) -> None:
    """Tier 0 reaches the network but never costs anything, and says so."""
    route = blocked_network.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            content=json.dumps(["crm", ["best crm software", "free crm"], [], [], {}]).encode(),
        )
    )

    outcome = await commands.execute(context, ":suggest crm")

    assert route.called
    assert "$0.00" in outcome.message
    assert outcome.view == views.View(views.KEYWORDS)
    assert count_rows(context.write, "keyword") == 2


async def test_suggest_a_second_time_makes_no_request(
    context: FakeContext, blocked_network: respx.MockRouter
) -> None:
    """The whole thesis, reached through the command bar this time."""
    route = blocked_network.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200, content=json.dumps(["crm", ["free crm"], [], [], {}]).encode()
        )
    )

    await commands.execute(context, ":suggest crm")
    await commands.execute(context, ":suggest crm")

    assert route.call_count == 1


async def test_cancel_drops_a_queued_job(context: FakeContext) -> None:
    """The stop button for a command typed by mistake.

    Tested here rather than through the app because the app hosts a runner that
    would race the cancel — which is itself the reason this command exists.
    """
    job_id = queue.enqueue(
        context.write, "labs.search_volume", {"keywords": ["x"]}, estimated_cost=0.012
    )

    outcome = await commands.execute(context, f":cancel {job_id}")

    assert "nothing spent" in outcome.message
    row = context.write.execute("SELECT status FROM job WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "cancelled"


async def test_cancelling_a_job_that_already_ran_says_so(context: FakeContext) -> None:
    """A started job cannot be un-spent, and the message must not imply it can."""
    job_id = queue.enqueue(
        context.write, "labs.search_volume", {"keywords": ["x"]}, estimated_cost=0.012
    )
    context.write.execute("UPDATE job SET status = 'done' WHERE id = ?", (job_id,))

    outcome = await commands.execute(context, f":cancel {job_id}")

    assert outcome.severity == "warning"
    assert "already started or finished" in outcome.message


async def test_cancel_needs_a_number(context: FakeContext) -> None:
    with pytest.raises(InvalidRequestError) as caught:
        await commands.execute(context, ":cancel ahrefs.com")
    assert ":cancel 7" in str(caught.value.fix)


async def test_ranks_defaults_to_your_own_domain(context: FakeContext) -> None:
    """The domain worth knowing is yours, so it needs no argument."""
    await commands.execute(context, ":ranks")
    assert context.asked == ["ranked keywords · mine.com"]


async def test_ranks_accepts_an_explicit_domain(context: FakeContext) -> None:
    outcome = await commands.execute(context, ":ranks ahrefs.com --limit 200")
    assert context.asked == ["ranked keywords · ahrefs.com"]
    assert "queued job" in outcome.message

    row = context.write.execute("SELECT capability, params_json FROM job").fetchone()
    assert row["capability"] == "labs.ranked_keywords"
    assert json.loads(row["params_json"]) == {"domain": "ahrefs.com", "limit": 200}


async def test_ranks_without_any_domain_names_both_remedies(db: sqlite3.Connection) -> None:
    context = FakeContext(read=db, write=db, own_domain=None)
    with pytest.raises(InvalidRequestError) as caught:
        await commands.execute(context, ":ranks")
    assert ":ranks example.com" in str(caught.value.fix)
    assert "own_domain" in str(caught.value.fix)


async def test_a_stored_rank_pull_is_read_without_asking(context: FakeContext) -> None:
    """Reading ranks you already own is free and promptless."""
    _stored(context.write, "labs.ranked_keywords", '{"domain": "mine.com", "limit": 100}')
    outcome = await commands.execute(context, ":ranks")
    assert context.asked == []
    assert "$0.00" in outcome.message
    assert outcome.view == views.View(views.DOMAIN, "mine.com")


async def test_a_deeper_stored_pull_covers_a_shallower_request(context: FakeContext) -> None:
    """A 1,000-row pull already contains the first 100. Do not charge twice."""
    _stored(context.write, "labs.ranked_keywords", '{"domain": "mine.com", "limit": 1000}')
    await commands.execute(context, ":ranks --limit 100")
    assert context.asked == []


async def test_a_shallower_stored_pull_does_not_cover_a_deeper_request(
    context: FakeContext,
) -> None:
    """The other direction: 100 rows do not answer a request for 1,000."""
    _stored(context.write, "labs.ranked_keywords", '{"domain": "mine.com", "limit": 100}')
    await commands.execute(context, ":ranks --limit 1000")
    assert context.asked == ["ranked keywords · mine.com"]


async def test_ideas_prices_flat_and_names_the_seed_count(context: FakeContext) -> None:
    """The one call whose price does not move with what you ask for."""
    outcome = await commands.execute(context, ':ideas "crm software" "project management"')

    assert context.asked == ["keyword ideas · 2 seeds"]
    assert "flat $0.09" in outcome.message

    row = context.write.execute("SELECT capability, estimated_cost FROM job").fetchone()
    assert row["capability"] == "keywords_data.keywords_for_keywords"
    assert row["estimated_cost"] == 0.09


async def test_ideas_with_no_seeds_gives_an_example(context: FakeContext) -> None:
    with pytest.raises(InvalidRequestError) as caught:
        await commands.execute(context, ":ideas")
    assert ":ideas" in str(caught.value.fix)


async def test_a_covering_pull_is_read_without_asking(context: FakeContext) -> None:
    """Twenty seeds bought once should not be re-bought one seed at a time."""
    _stored(
        context.write,
        "keywords_data.keywords_for_keywords",
        json.dumps({"seeds": ["crm software", "project management"]}),
    )
    outcome = await commands.execute(context, ':ideas "crm software"')

    assert context.asked == []
    assert "$0.00" in outcome.message


async def test_declining_ideas_queues_nothing(db: sqlite3.Connection) -> None:
    context = FakeContext(read=db, write=db, approve=False)
    outcome = await commands.execute(context, ':ideas "crm software"')

    assert "nothing queued, nothing spent" in outcome.message
    assert context.write.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"] == 0
