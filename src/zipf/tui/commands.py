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
from dataclasses import dataclass, replace
from typing import Final, Literal, Protocol

from zipf.budget import Budget
from zipf.clock import as_days
from zipf.errors import InvalidRequestError, ZipfError
from zipf.format import plural
from zipf.jobs import queue
from zipf.pricing import Plan, PriceEstimate
from zipf.services import browse
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
    #: Whether the marked rows were consumed. Set only when a command actually
    #: queued work from them: marks that produced nothing are still a selection
    #: the user made, and clearing them on a declined spend would lose it.
    clears_marks: bool = False


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

    @property
    def marked(self) -> frozenset[str]:
        """Rows the user has marked, which a command with no arguments reads."""
        ...

    async def confirm(self, estimate: PriceEstimate, *, what: str, detail: str = "") -> bool:
        """Ask before spending. Returns whether to proceed."""
        ...


#: What a preview says about a request, which is also what decides its colour.
#: ``spend`` is the only one allowed the reserved register.
type Register = Literal["idle", "info", "free", "spend", "error"]


@dataclass(frozen=True)
class Preview:
    """What a typed command would do, computed without doing any of it.

    Exists because pricing is free: every ``plan`` in ``services/`` is a pure
    read against the local cache, so the answer to "what would this cost" can be
    recomputed on every keystroke. That was forced by R5 — only the runner
    spends — and this is the interface that gets paid back for it.

    Deliberately holds no plan object. A preview is a description for a human;
    running the command re-plans against the database as it is at that moment,
    so nothing can be bought against a price computed several keystrokes ago.
    """

    register: Register
    headline: str
    #: Label and value pairs, shown under the bar while the command is being
    #: typed. Empty for commands whose headline says everything.
    lines: tuple[tuple[str, str], ...] = ()
    usd: float = 0.0

    @property
    def spends(self) -> bool:
        return self.register == "spend"


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


def _sample(values: Sequence[str], most: int = 4) -> str:
    """The first few of a list, saying how many were left out.

    The preview line has one row to work with, and a truncated list that does
    not admit it was truncated misrepresents what is about to be bought.
    """
    if len(values) <= most:
        return ", ".join(values)
    return f"{', '.join(values[:most])}, +{len(values) - most} more"


@dataclass(frozen=True)
class Purchase:
    """One priced request, and what the app should do at each branch.

    The mirror of ``cli.paid.Purchase``. Both shells take the same three-way
    decision — already owned, declined, queued — over the same plans, and both
    say something different at every branch of it.
    """

    plan: Plan
    #: Names the purchase in the confirmation modal's title.
    what: str
    #: Reported when the request is already answered by stored data.
    owned: str
    #: Queues the work and describes what was queued. Called only after the gate
    #: has said yes, so a command cannot enqueue by taking the wrong branch, and
    #: it is the only thing here handed the writable connection.
    queue_work: Callable[[sqlite3.Connection], str]
    #: Extra context under the modal's title, where the request needs it.
    detail: str = ""
    #: Where to go when the request is already owned.
    owned_view: View | None = None
    #: Where to go when the spend is declined. Deliberately a separate field:
    #: ``:gap`` and ``:ranks`` return you to the domain you asked about, while
    #: ``:vol`` and ``:ideas`` leave the view alone. That asymmetry looks
    #: accidental, but changing where a declined command lands is a behaviour
    #: change and not this refactor's business.
    declined_view: View | None = None


async def buy(context: Context, purchase: Purchase) -> Outcome:
    """Take the buy-or-not decision for one typed command.

    Nothing here spends. Reading what you already own is never gated, a decline
    queues nothing, and an approval enqueues for the runner to execute (R5).
    """
    if purchase.plan.is_free:
        return Outcome(message=purchase.owned, view=purchase.owned_view)

    approved = await context.confirm(
        purchase.plan.estimate, what=purchase.what, detail=purchase.detail
    )
    if not approved:
        return Outcome(
            message="cancelled · nothing queued, nothing spent", view=purchase.declined_view
        )

    return Outcome(message=purchase.queue_work(context.write), changed=True)


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


@dataclass(frozen=True)
class _VolRequest:
    """A parsed ``:vol``, and where its keywords came from.

    ``source`` exists for the preview. "9 keywords · ~$0.01308" is ambiguous
    about *which* nine when the keywords were never typed, and a price whose
    subject is off-screen is exactly the thing the preview is meant to prevent.
    """

    plan: volume_service.VolumePlan
    source: str


def _vol_request(context: Context, args: list[str]) -> _VolRequest:
    """Resolve ``:vol`` arguments into a plan. Reads only; costs nothing.

    Three ways to name keywords, in the order they are checked: ``--stale``
    takes everything the refresh planner is showing, marked rows are used when
    nothing was typed, and otherwise the arguments are the keywords.
    """
    force, args = take_switch(args, "--force")
    whole_stale, args = take_switch(args, "--stale")

    if whole_stale:
        stale = browse.stale_keywords(context.read)
        if not stale:
            raise InvalidRequestError(
                "Nothing has aged out.",
                fix="Every measured keyword is still inside its TTL, so there is nothing to buy.",
            )
        keywords = [row["keyword"] for row in stale]
        source = "the stale view"
    elif args:
        keywords, source = args, "typed"
    elif context.marked:
        keywords = sorted(context.marked)
        source = f"{plural(len(keywords), 'marked row')}"
    else:
        raise InvalidRequestError(
            "vol needs something to work on.",
            fix=(
                'Try `:vol "best crm software"`, mark rows with space, '
                "or `:vol --stale` for everything past its TTL."
            ),
        )

    # `--stale` rows are stale by definition, so the freshness check they would
    # otherwise repeat is skipped rather than run twice.
    return _VolRequest(
        plan=volume_service.plan(context.read, keywords, force=force or whole_stale),
        source=source,
    )


def _preview_vol(context: Context, args: list[str]) -> Preview:
    request = _vol_request(context, args)
    plan = request.plan
    if plan.is_free:
        return Preview(
            register="free",
            headline=(
                f"{plural(len(plan.cached), 'keyword')} already fresh · nothing to buy · $0.00"
            ),
            lines=(
                ("within TTL", f"{as_days(volume_service.VOLUME_TTL):.0f}d"),
                ("source", request.source),
            ),
        )
    return Preview(
        register="spend",
        headline=(
            f"{plural(len(plan.stale), 'keyword')} · {plural(len(plan.batches), 'job')} "
            f"· ~${plan.estimate.usd:.5f}"
        ),
        lines=(
            ("from", request.source),
            (
                "already fresh",
                f"{len(plan.cached)} skipped, $0.00" if plan.cached else "none",
            ),
            ("would fetch", _sample(plan.stale)),
        ),
        usd=plan.estimate.usd,
    )


async def _vol(context: Context, args: list[str]) -> Outcome:
    """Search volume. Tier 1, paid, and free for keywords already owned."""
    request = _vol_request(context, args)
    plan = request.plan

    def queue_batches(conn: sqlite3.Connection) -> str:
        """One job per batch, so a partial failure loses one call, not all of them."""
        job_ids = volume_service.enqueue(conn, plan)
        return (
            f"queued {plural(len(job_ids), 'job')} for {plural(len(plan.stale), 'keyword')} "
            f"· ~${plan.estimate.usd:.5f} when it runs"
        )

    outcome = await buy(
        context,
        Purchase(
            plan=plan,
            what=f"search volume · {plural(len(plan.stale), 'keyword')}",
            detail=f"from {request.source}" if request.source != "typed" else "",
            owned=(f"{plural(len(plan.cached), 'keyword')} already fresh · nothing to buy · $0.00"),
            owned_view=View(views.KEYWORDS),
            queue_work=queue_batches,
        ),
    )
    if outcome.changed and context.marked:
        return replace(outcome, clears_marks=True)
    return outcome


def _ideas_plan(context: Context, args: list[str]) -> ideas_service.IdeasPlan:
    force, args = take_switch(args, "--force")
    _require(args, what="ideas", example=':ideas "crm software" "project management"')
    return ideas_service.plan(context.read, args, force=force)


def _preview_ideas(context: Context, args: list[str]) -> Preview:
    plan = _ideas_plan(context, args)
    if plan.is_free:
        return Preview(
            register="free",
            headline=f"already stored, pulled {as_days(plan.age):.1f}d ago · $0.00",
            lines=(("seeds", _sample(plan.seeds)),),
        )
    return Preview(
        register="spend",
        headline=f"{plural(len(plan.seeds), 'seed')} · flat ${plan.estimate.usd:.2f}",
        lines=(
            ("seeds", _sample(plan.seeds)),
            ("flat fee", "twenty seeds and one seed cost the same"),
            ("rows", "unknown until it arrives"),
        ),
        usd=plan.estimate.usd,
    )


async def _ideas(context: Context, args: list[str]) -> Outcome:
    """Discover keywords with volume attached. Tier 1, paid, flat fee.

    The only call here whose price does not move with what you ask for, so the
    message names the seed count: twenty seeds and one seed cost the same.
    """
    plan = _ideas_plan(context, args)

    def queue_call(conn: sqlite3.Connection) -> str:
        return (
            f"queued job {ideas_service.enqueue(conn, plan)} "
            f"for {plural(len(plan.seeds), 'seed')} "
            f"· flat ${plan.estimate.usd:.2f} when it runs"
        )

    return await buy(
        context,
        Purchase(
            plan=plan,
            what=f"keyword ideas · {plural(len(plan.seeds), 'seed')}",
            detail=", ".join(plan.seeds),
            owned=f"already stored, pulled {as_days(plan.age):.1f}d ago · $0.00",
            owned_view=View(views.KEYWORDS),
            queue_work=queue_call,
        ),
    )


def _ranks_plan(context: Context, args: list[str]) -> ranks_service.RanksPlan:
    limit, args = take_flag(args, "--limit")
    force, args = take_switch(args, "--force")

    target = args[0] if args else context.own_domain
    if not target:
        raise InvalidRequestError(
            "No domain to look up.",
            fix="Pass a domain, as in `:ranks example.com`, or set own_domain in your config.",
        )

    return ranks_service.plan(
        context.read,
        target,
        limit=_positive_int(limit, "--limit", labs.DEFAULT_LIMIT),
        force=force,
    )


def _preview_ranks(context: Context, args: list[str]) -> Preview:
    plan = _ranks_plan(context, args)
    if plan.is_free:
        return Preview(
            register="free",
            headline=f"already stored, pulled {as_days(plan.age):.1f}d ago · $0.00",
            lines=(("opens", plan.domain),),
        )
    return Preview(
        register="spend",
        headline=(f"{plan.domain} · {plural(plan.limit, 'row')} · ~${plan.estimate.usd:.5f}"),
        lines=(
            ("asks", f"what {plan.domain} already ranks for"),
            ("depth", f"limit {plan.limit} (max {labs.MAX_LIMIT})"),
        ),
        usd=plan.estimate.usd,
    )


async def _ranks(context: Context, args: list[str]) -> Outcome:
    """What a domain already ranks for, yours by default. Tier 1, paid."""
    plan = _ranks_plan(context, args)
    destination = View(views.DOMAIN, plan.domain)
    return await buy(
        context,
        Purchase(
            plan=plan,
            what=f"ranked keywords · {plan.domain}",
            detail=f"what {plan.domain} already ranks for",
            owned=f"already stored, pulled {as_days(plan.age):.1f}d ago · $0.00",
            owned_view=destination,
            declined_view=destination,
            queue_work=lambda conn: (
                f"queued job {ranks_service.enqueue(conn, plan)} "
                f"· ~${plan.estimate.usd:.5f} when it runs"
            ),
        ),
    )


def _gap_plan(context: Context, args: list[str]) -> gap_service.GapPlan:
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

    return gap_service.plan(
        context.read,
        args[0],
        own,
        limit=_positive_int(limit, "--limit", labs.DEFAULT_LIMIT),
        force=force,
    )


def _preview_gap(context: Context, args: list[str]) -> Preview:
    plan = _gap_plan(context, args)
    if plan.is_free:
        return Preview(
            register="free",
            headline=f"already stored, pulled {as_days(plan.age):.1f}d ago · $0.00",
            lines=(("opens", f"{plan.competitor} ranks, {plan.mine} does not"),),
        )
    return Preview(
        register="spend",
        headline=(
            f"{plan.competitor} vs {plan.mine} · {plural(plan.limit, 'row')} "
            f"· ~${plan.estimate.usd:.5f}"
        ),
        lines=(
            ("asks", f"what {plan.competitor} ranks for and {plan.mine} does not"),
            ("depth", f"limit {plan.limit} (max {labs.MAX_LIMIT})"),
        ),
        usd=plan.estimate.usd,
    )


async def _gap(context: Context, args: list[str]) -> Outcome:
    """Keywords a competitor ranks for and you do not. Tier 1, paid.

    Reading a gap already stored is free and silent, which is why the freshness
    check comes before the price is ever mentioned.
    """
    plan = _gap_plan(context, args)
    target = View(views.GAP, f"{plan.competitor}|{plan.mine}")
    return await buy(
        context,
        Purchase(
            plan=plan,
            what=f"keyword gap · {plan.competitor}",
            detail=f"{plan.competitor} ranks for, {plan.mine} does not",
            owned=f"already stored, pulled {as_days(plan.age):.1f}d ago · $0.00",
            owned_view=target,
            declined_view=target,
            queue_work=lambda conn: (
                f"queued job {gap_service.enqueue(conn, plan)} "
                f"· ~${plan.estimate.usd:.5f} when it runs"
            ),
        ),
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


def _preview_help(context: Context, args: list[str]) -> Preview:
    return Preview(
        register="info",
        headline=f"{plural(len(REGISTRY), 'command')} · * costs money",
        lines=tuple(
            (f":{command.name}{'*' if command.spends else ''}", command.summary)
            for command in REGISTRY.values()
        ),
    )


def _preview_cancel(context: Context, args: list[str]) -> Preview:
    _require(args, what="cancel", example=":cancel 7")
    if not args[0].isdigit():
        raise InvalidRequestError(
            f"{args[0]!r} is not a job number.",
            fix="Take the number from the jobs pane, as in `:cancel 7`.",
        )
    job = queue.get(context.read, int(args[0]))
    if job is None:
        raise InvalidRequestError(
            f"There is no job {args[0]}.", fix="Take the number from the jobs pane."
        )
    if job["status"] != "queued":
        return Preview(
            register="error",
            headline=f"job {args[0]} is {job['status']} — too late to cancel",
        )
    return Preview(
        register="info",
        headline=f"drop job {args[0]} · nothing spent",
        lines=(("would have cost", f"${job['estimated_cost']:.5f}"),),
    )


def _preview_suggest(context: Context, args: list[str]) -> Preview:
    _require(args, what="suggest", example=":suggest crm software")
    return Preview(
        register="free",
        headline="autocomplete · tier 0 · $0.00",
        lines=(("seed", " ".join(args)),),
    )


def _preview_budget(context: Context, args: list[str]) -> Preview:
    return Preview(register="free", headline="refresh the vendor balance · free, one round trip")


def _preview_quit(context: Context, args: list[str]) -> Preview:
    return Preview(register="info", headline="close zipf")


@dataclass(frozen=True)
class Command:
    name: str
    summary: str
    run: Callable[[Context, list[str]], Awaitable[Outcome]]
    #: Whether running this can cost money. Drives the accent colour: the two
    #: visual registers only mean something if the split is declared, not guessed.
    spends: bool = False
    #: What this command would do, computed without doing it. Runs on every
    #: keystroke, so it must be a pure local read — every ``plan`` in
    #: ``services/`` already is, which is what makes this affordable.
    preview: Callable[[Context, list[str]], Preview] | None = None


REGISTRY: Final[dict[str, Command]] = {
    command.name: command
    for command in (
        Command("help", "list the commands", _help, preview=_preview_help),
        Command("quit", "close zipf", _quit, preview=_preview_quit),
        Command("budget", "refresh spend and vendor balance", _budget, preview=_preview_budget),
        Command("cancel", "drop a queued job before it runs", _cancel, preview=_preview_cancel),
        Command(
            "suggest", "autocomplete suggestions for a seed", _suggest, preview=_preview_suggest
        ),
        Command("vol", "search volume for keywords", _vol, spends=True, preview=_preview_vol),
        Command(
            "ranks",
            "what a domain already ranks for",
            _ranks,
            spends=True,
            preview=_preview_ranks,
        ),
        Command(
            "ideas",
            "discover keywords with volume attached",
            _ideas,
            spends=True,
            preview=_preview_ideas,
        ),
        Command(
            "gap",
            "keywords a competitor ranks for and you do not",
            _gap,
            spends=True,
            preview=_preview_gap,
        ),
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


#: Shown before anything has been typed. Not an error: an empty bar is a bar
#: waiting for input, and colouring it red would make opening it look like a
#: mistake.
_IDLE = Preview(register="idle", headline="type a command · :help lists them")


def preview(context: Context, text: str) -> Preview:
    """What running ``text`` would do, without running any of it.

    Safe to call on every keystroke. Everything it reaches is a local read: the
    ``plan`` functions price against the cache, and ``queue.get`` reads one row.
    Nothing here touches the write handle or the network, so a half-typed
    ``:gap a.com`` cannot buy anything — it can only be described.

    Errors come back as a preview rather than an exception because half-typed
    input is the normal state of a command bar. ``:ga`` is not a mistake to
    report, it is a command in progress, and the honest thing to show is what is
    wrong with it so far.
    """
    if not text.strip().removeprefix(":").strip():
        return _IDLE
    try:
        command, args = parse(text)
        builder = command.preview
        if builder is None:
            return Preview(register="info", headline=command.summary)
        return builder(context, args)
    except ZipfError as exc:
        lines = (("fix", exc.fix),) if exc.fix else ()
        return Preview(register="error", headline=exc.problem, lines=lines)
