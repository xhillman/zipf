"""The `:` commands, dispatching into the same services the CLI calls.

Free of Textual, like ``views``. A command receives a ``Context`` and returns an
``Outcome``; confirming a spend is a method on the context rather than a screen
push, so the whole registry is testable by handing it a context that always says
yes and one that always says no.

Two rules the registry exists to enforce:

- **No command spends synchronously.** Paid commands price, confirm, and enqueue.
  The runner is the only thing that calls a vendor (R5).
- **Reading what you own is never gated.** A plan that is already answered by
  stored data prints and stops, without a prompt — the same rule ``zipf gap``
  follows since the usability pass.
"""

from __future__ import annotations

import shlex
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from zipf.budget import Budget
from zipf.errors import InvalidRequestError
from zipf.format import plural
from zipf.jobs import queue
from zipf.pricing import PriceEstimate
from zipf.services import budget as budget_service
from zipf.services import gap as gap_service
from zipf.services import ideas as ideas_service
from zipf.services import ranks as ranks_service
from zipf.services import suggest as suggest_service
from zipf.services import volume as volume_service
from zipf.sources.dataforseo import labs
from zipf.tui import views
from zipf.tui.views import View

#: The three levels a notification can carry. Spelled out here rather than
#: imported so this module stays free of Textual, which happens to use the same
#: three words.
type Severity = Literal["information", "warning", "error"]


@dataclass(frozen=True)
class Outcome:
    """What the app should do once a command has run."""

    message: str
    #: Switch to this view, when the command produced something to look at.
    view: View | None = None
    severity: Severity = "information"
    #: Whether the table and status bar need re-reading. False for commands that
    #: only reported something, so a readout does not cause a redraw.
    changed: bool = False
    #: Whether the app should close. Carried here rather than raising, so quitting
    #: goes through the same path as every other command.
    exits: bool = False


class Context(Protocol):
    """What a command is allowed to touch.

    Two connections, deliberately separate: ``read`` is opened ``mode=ro`` and
    backs everything the browser renders, ``write`` is the only handle that can
    enqueue. A command that never spends never sees a writable database.
    """

    @property
    def read(self) -> sqlite3.Connection: ...

    @property
    def write(self) -> sqlite3.Connection: ...

    @property
    def budget(self) -> Budget: ...

    @property
    def own_domain(self) -> str | None: ...

    async def confirm(self, estimate: PriceEstimate, *, what: str, detail: str = "") -> bool:
        """Ask before spending. Returns whether to proceed."""
        ...


def take_flag(args: list[str], flag: str) -> tuple[str | None, list[str]]:
    """Pull ``--flag value`` out of an argument list.

    Hand-rolled rather than ``argparse`` because argparse exits the process on a
    bad argument, which inside a running application is not a recoverable error
    message but a closed window.
    """
    if flag not in args:
        return None, args
    index = args.index(flag)
    if index + 1 >= len(args):
        raise InvalidRequestError(
            f"{flag} needs a value.",
            fix=f"Write it as `{flag} <value>`, or leave the flag out.",
        )
    return args[index + 1], args[:index] + args[index + 2 :]


def take_switch(args: list[str], switch: str) -> tuple[bool, list[str]]:
    """Pull a valueless ``--switch`` out of an argument list."""
    if switch not in args:
        return False, args
    return True, [arg for arg in args if arg != switch]


def _require(args: Sequence[str], *, what: str, example: str) -> None:
    if not args:
        raise InvalidRequestError(f"{what} needs something to work on.", fix=f"Try `{example}`.")


async def _quit(context: Context, args: list[str]) -> Outcome:
    return Outcome(message="closing", exits=True)


async def _help(context: Context, args: list[str]) -> Outcome:
    """List what can be typed. Paid commands are marked as such."""
    listing = "  ".join(
        f":{command.name}{'*' if command.spends else ''}" for command in REGISTRY.values()
    )
    return Outcome(message=f"{listing}   [* costs money]")


async def _budget(context: Context, args: list[str]) -> Outcome:
    """Refresh the vendor balance for real.

    Unlike the status bar on startup, this is a command the user typed to look at
    money, so it is worth a round trip. It is free, and it writes its response
    through the write handle like any other fetch.
    """
    state = await budget_service.status(context.write, context.budget, refresh=True)
    balance = f" · ${state.balance:.2f} at the vendor" if state.balance is not None else ""
    return Outcome(
        message=(
            f"${state.effective_limit:.2f} available · "
            f"${state.spent:.5f} spent of ${state.ceiling:.2f}{balance}"
        ),
        changed=True,
    )


async def _suggest(context: Context, args: list[str]) -> Outcome:
    """Autocomplete suggestions. Tier 0, free, but it does reach the network."""
    questions, args = take_switch(args, "--questions")
    alphabet, args = take_switch(args, "--alphabet")
    _require(args, what="suggest", example=":suggest crm software")

    result = await suggest_service.expand(
        context.write,
        " ".join(args),
        budget=context.budget,
        questions=questions,
        alphabet=alphabet,
    )
    return Outcome(
        message=(
            f"{plural(len(result.suggestions), 'suggestion')} from "
            f"{plural(len(result.results), 'seed')} · {result.cache_hits} cached · $0.00"
        ),
        view=View(views.KEYWORDS),
        changed=True,
    )


async def _vol(context: Context, args: list[str]) -> Outcome:
    """Search volume. Tier 1, paid, and free for keywords already owned."""
    force, args = take_switch(args, "--force")
    _require(args, what="vol", example=':vol "best crm software" "free crm"')

    plan = volume_service.plan(context.read, args, force=force)
    if plan.is_free:
        return Outcome(
            message=f"{plural(len(plan.cached), 'keyword')} already fresh · nothing to buy · $0.00",
            view=View(views.KEYWORDS),
        )

    approved = await context.confirm(
        plan.estimate,
        what=f"search volume · {plural(len(plan.stale), 'keyword')}",
    )
    if not approved:
        return Outcome(message="cancelled · nothing queued, nothing spent")

    job_ids = volume_service.enqueue(context.write, plan)
    return Outcome(
        message=(
            f"queued {plural(len(job_ids), 'job')} for {plural(len(plan.stale), 'keyword')} "
            f"· ~${plan.estimate.usd:.5f} when it runs"
        ),
        changed=True,
    )


async def _ideas(context: Context, args: list[str]) -> Outcome:
    """Discover keywords with volume attached. Tier 1, paid, flat fee.

    The only call here whose price does not move with what you ask for, so the
    message names the seed count: twenty seeds and one seed cost the same.
    """
    force, args = take_switch(args, "--force")
    _require(args, what="ideas", example=':ideas "crm software" "project management"')

    plan = ideas_service.plan(context.read, args, force=force)
    if plan.is_fresh:
        days = plan.age.total_seconds() / 86400 if plan.age else 0.0
        return Outcome(
            message=f"already stored, pulled {days:.1f}d ago · $0.00",
            view=View(views.KEYWORDS),
        )

    approved = await context.confirm(
        plan.estimate,
        what=f"keyword ideas · {plural(len(plan.seeds), 'seed')}",
        detail=", ".join(plan.seeds),
    )
    if not approved:
        return Outcome(message="cancelled · nothing queued, nothing spent")

    job_id = ideas_service.enqueue(context.write, plan)
    return Outcome(
        message=(
            f"queued job {job_id} for {plural(len(plan.seeds), 'seed')} "
            f"· flat ${plan.estimate.usd:.2f} when it runs"
        ),
        changed=True,
    )


async def _ranks(context: Context, args: list[str]) -> Outcome:
    """What a domain already ranks for, yours by default. Tier 1, paid."""
    limit, args = take_flag(args, "--limit")
    force, args = take_switch(args, "--force")

    target = args[0] if args else context.own_domain
    if not target:
        raise InvalidRequestError(
            "No domain to look up.",
            fix="Pass a domain, as in `:ranks example.com`, or set own_domain in your config.",
        )

    plan = ranks_service.plan(
        context.read,
        target,
        limit=_positive_int(limit, "--limit", labs.DEFAULT_LIMIT),
        force=force,
    )
    destination = View(views.DOMAIN, plan.domain)
    if plan.is_fresh:
        days = plan.age.total_seconds() / 86400 if plan.age else 0.0
        return Outcome(message=f"already stored, pulled {days:.1f}d ago · $0.00", view=destination)

    approved = await context.confirm(
        plan.estimate,
        what=f"ranked keywords · {plan.domain}",
        detail=f"what {plan.domain} already ranks for",
    )
    if not approved:
        return Outcome(message="cancelled · nothing queued, nothing spent", view=destination)

    job_id = ranks_service.enqueue(context.write, plan)
    return Outcome(
        message=f"queued job {job_id} · ~${plan.estimate.usd:.5f} when it runs",
        changed=True,
    )


async def _gap(context: Context, args: list[str]) -> Outcome:
    """Keywords a competitor ranks for and you do not. Tier 1, paid.

    Reading a gap already stored is free and silent, which is why the freshness
    check comes before the price is ever mentioned.
    """
    mine, args = take_flag(args, "--mine")
    limit, args = take_flag(args, "--limit")
    force, args = take_switch(args, "--force")
    _require(args, what="gap", example=":gap ahrefs.com")

    own = mine or context.own_domain
    if not own:
        raise InvalidRequestError(
            "A gap needs your domain as well as theirs.",
            fix="Pass `--mine yourdomain.com`, or set own_domain in your config file.",
        )

    plan = gap_service.plan(
        context.read,
        args[0],
        own,
        limit=_positive_int(limit, "--limit", labs.DEFAULT_LIMIT),
        force=force,
    )
    target = View(views.GAP, f"{plan.competitor}|{plan.mine}")
    if plan.is_fresh:
        days = plan.age.total_seconds() / 86400 if plan.age else 0.0
        return Outcome(message=f"already stored, pulled {days:.1f}d ago · $0.00", view=target)

    approved = await context.confirm(
        plan.estimate,
        what=f"keyword gap · {plan.competitor}",
        detail=f"{plan.competitor} ranks for, {plan.mine} does not",
    )
    if not approved:
        return Outcome(message="cancelled · nothing queued, nothing spent", view=target)

    job_id = gap_service.enqueue(context.write, plan)
    return Outcome(
        message=f"queued job {job_id} · ~${plan.estimate.usd:.5f} when it runs",
        changed=True,
    )


async def _cancel(context: Context, args: list[str]) -> Outcome:
    """Pull a queued job before the runner reaches it.

    The app drains continuously, so this is the only way to change your mind
    about something already queued without closing the window. A job that has
    started cannot be cancelled — the money is already committed.
    """
    _require(args, what="cancel", example=":cancel 7")
    if not args[0].isdigit():
        raise InvalidRequestError(
            f"{args[0]!r} is not a job number.",
            fix="Take the number from the jobs pane, as in `:cancel 7`.",
        )

    job_id = int(args[0])
    if queue.cancel(context.write, job_id):
        return Outcome(message=f"cancelled job {job_id} · nothing spent", changed=True)
    return Outcome(
        message=f"job {job_id} is not queued — it has already started or finished",
        severity="warning",
    )


def _positive_int(value: str | None, flag: str, fallback: int) -> int:
    if value is None:
        return fallback
    if not value.isdigit() or int(value) < 1:
        raise InvalidRequestError(
            f"{flag} needs a whole number above zero, not {value!r}.",
            fix=f"Try `{flag} 100`.",
        )
    return int(value)


@dataclass(frozen=True)
class Command:
    name: str
    summary: str
    run: Callable[[Context, list[str]], Awaitable[Outcome]]
    #: Whether running this can cost money. Drives the accent colour: the two
    #: visual registers only mean something if the split is declared, not guessed.
    spends: bool = False


REGISTRY: Final[dict[str, Command]] = {
    command.name: command
    for command in (
        Command("help", "list the commands", _help),
        Command("quit", "close zipf", _quit),
        Command("budget", "refresh spend and vendor balance", _budget),
        Command("cancel", "drop a queued job before it runs", _cancel),
        Command("suggest", "autocomplete suggestions for a seed", _suggest),
        Command("vol", "search volume for keywords", _vol, spends=True),
        Command("ranks", "what a domain already ranks for", _ranks, spends=True),
        Command("ideas", "discover keywords with volume attached", _ideas, spends=True),
        Command("gap", "keywords a competitor ranks for and you do not", _gap, spends=True),
    )
}

#: Short forms. Kept separate from the registry so the help listing shows one
#: line per command rather than one per spelling.
ALIASES: Final[dict[str, str]] = {"q": "quit", "exit": "quit", "b": "budget"}


def resolve(name: str) -> Command:
    """Find a command by name or alias."""
    canonical = ALIASES.get(name, name)
    command = REGISTRY.get(canonical)
    if command is None:
        raise InvalidRequestError(
            f"There is no :{name} command.",
            fix=f"Available: {', '.join(':' + key for key in REGISTRY)}.",
        )
    return command


def parse(text: str) -> tuple[Command, list[str]]:
    """Split typed input into a command and its arguments.

    Quoting is ``shlex``, so a multi-word keyword is written the way it would be
    on the command line: ``:vol "best crm software"``.
    """
    stripped = text.strip().removeprefix(":").strip()
    if not stripped:
        raise InvalidRequestError("No command was typed.", fix="Try `:help` for the list.")
    try:
        tokens = shlex.split(stripped)
    except ValueError as exc:
        raise InvalidRequestError(
            f"That command could not be read: {exc}.",
            fix='Check the quotes are balanced, as in `:vol "best crm software"`.',
        ) from exc
    return resolve(tokens[0]), tokens[1:]


async def execute(context: Context, text: str) -> Outcome:
    """Parse and run one typed command."""
    command, args = parse(text)
    return await command.run(context, args)
