# zipf

Single-operator keyword research over a local SQLite cache.

Zipf answers *"what should I write about, and did it work"* using search data it
buys once and owns forever. Commercial SEO platforms charge $130–150/month for
data available wholesale for under $20; the gap is product, not data.

**Zipf is not a keyword tool. It is a cache with opinions about staleness.**
Every feature is a cache hit; every dollar spent is a cache miss.

---

## Status

Working today: **M0–M2.9**. Free and paid tiers run end to end from the CLI, with
price declaration, a monthly ceiling, asynchronous spend, and near-duplicate
keyword clustering.

| Milestone | State |
|---|---|
| M0 The metered loop | done |
| M1 Free tier — autocomplete, Search Console | done |
| M2 Paid tier — volume, gap, jobs, budget | done |
| M2.9 Usability pass | done |
| M3 Terminal UI | next |
| M3.5 Litestream backup | not started |
| M4 SERP and AI Overview | not started |
| M5 LLM visibility panel | not started |
| M6 MCP server | not started |

Both paid capabilities have been verified against the live DataForSEO API, with
recorded cost reconciled against the vendor balance to the cent and zero
estimator drift across every purchase.

**`observation` is still empty.** It is the table where classic rank and LLM
visibility meet in one shape, which is the thing a subscription cannot do — and
it stays empty until M4 and M5.

---

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run zipf init
```

`init` creates the database and a config file:

- database — `~/.local/share/zipf/zipf.db`
- config — `~/.config/zipf/config.toml`
- OAuth tokens — `~/.local/share/zipf/state/` (mode 0600)

Set `ZIPF_HOME` to point all three somewhere else. That is what test isolation
and scratch databases use.

### Credentials

Secrets come from the environment or a gitignored `.env`. None are required to
start: a capability whose credential is missing fails when you call it, naming
what it needed, rather than blocking startup.

| Variable | Needed for |
|---|---|
| `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD` | `vol`, `gap`, `budget` balance |
| `GSC_CLIENT_ID` / `GSC_CLIENT_SECRET` | Search Console (Google Cloud OAuth **desktop** client) |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | LLM panel (M5, not yet built) |

Search Console needs one scope: `https://www.googleapis.com/auth/webmasters.readonly`.
Keep the OAuth consent screen in **production** if you want refresh tokens to
outlive 7 days; in *testing* status Google expires them weekly.

---

## Commands

```
zipf init                                        create db, run migrations, scaffold config
zipf suggest <seed> [--questions] [--alphabet]   tier 0, free
zipf gsc auth | sites | import [--days N]        tier 0, free
zipf vol <keyword>...                            tier 1 — free when already owned
zipf gap <competitor> [--mine D] [--limit N]     tier 1 — free when already owned
zipf jobs run | list | show <id> | cancel <id>   the work queue
zipf budget [--cached]                           what you can still spend
zipf db stats                                    what the database holds
zipf db rebuild [--capability NAME]              replay projections from stored bytes
zipf db prune [--dry-run]                        drop free, superseded responses
```

**Reading data you already own is always free and never prompts.** `vol` and
`gap` check what is stored first; you are asked to pay only for what is missing
or past its TTL. Every command ends with a line naming what changed and what it
cost, and cost is measured from the ledger rather than predicted, so a command
and `zipf budget` cannot disagree.

Flags on `vol` and `gap`:

| Flag | Effect |
|---|---|
| `--dry-run` | Print the plan and the bill. Spends $0. |
| `--cached` | Read stored data only. Never prompts, never spends. |
| `--force` | Re-buy even when a fresh copy is stored. |
| `--yes` | Skip the confirmation prompt. |
| `--wait` | Drain the queue before returning. |
| `--flat` | Show every phrasing instead of collapsing restatements. |

### Reading what you own — free, no prompt

```console
$ zipf gap joshwcomeau.com
cached pulled 1.0d ago · nothing to buy · $0.00
               50 distinct queries · 50 restatement(s) collapsed
┃ keyword                 ┃     vol ┃ pos ┃ also ┃ their url          ┃
│ game button css         │ 135,000 │   2 │   +1 │ joshwcomeau.com/…  │
│ bottom shadow css       │   9,900 │   6 │   +3 │ joshwcomeau.com/…  │
→ 50 distinct queries from 100 rows · joshwcomeau.com not matched by you · $0.00
```

`also +3` means three further phrasings of the same query were collapsed into
that row. A real 100-row pull was half restatements.

### Buying something new

```console
$ zipf gap css-tricks.com --limit 100
╭─ pull keyword gap · css-tricks.com ────────────╮
│   css-tricks.com ranks for, xhillman.dev does not
│   ~100 rows              not cached            │
│   $0.0240                tier 1, none queue    │
│   remaining this month: $19.95                 │
│   vendor balance:       $0.91                  │
╰────────────────────────────────────────────────╯
pull [y/N]: y
queued job 5

$ zipf jobs run          # money moves here, never at the prompt
job 5 done: labs.domain_intersection · $0.02400
→ 1 done · $0.02400
```

### Where the money is

```console
$ zipf budget
$0.91 available  ▒░░░░░░░░░  5% used
  limited by your DataForSEO balance, not the $20.00 monthly ceiling

  spent this month  $0.04836 of $20.00
  vendor balance    $0.91 at DataForSEO live
  confirms          every spend

$ zipf jobs list
 id │ what                         │ kind │ status    │ when │      cost
  4 │ hugo vs eleventy +1          │ vol  │ done      │  49m │  $0.01224
  3 │ joshwcomeau.com vs xhillman… │ gap  │ done      │  21h │  $0.02400
  1 │ ahrefs.com vs xhillman.dev   │ gap  │ cancelled │  22h │ ~$0.13200
```

The headline is the smaller of your monthly ceiling and your vendor balance,
because that is the number that is actually true. The meter measures spend
against that headroom, not against the ceiling — a $20 ceiling would read 0%
while a $0.91 balance was nearly exhausted.

`zipf jobs show <id>` gives the full record for one job: subject, depth,
estimate against actual with drift, every timestamp, and the stored response it
produced.

---

## How it works

```
        [ tier 0–3 sources ]
                 |
        ===  fetch()  ===        <- the only priced door
                 |
        raw_response              <- append-only, immutable, source of truth
                 |
              rebuild             <- free, idempotent, repeatable
                 |
     projections + observation    <- disposable, never hand-written
                 |
          CLI · TUI · MCP         <- read-only consumers
```

Exactly one function touches the network. It runs six steps in a fixed order,
and the order is load-bearing:

1. **Normalise params → hash.** Casing, whitespace, and key order cannot buy the
   same data twice.
2. **TTL short-circuit.** Target: >95% of calls end here at zero cost.
3. **Price declaration.** Computed before the call, never read off an invoice.
4. **Ceiling check.** Fails the call. It does not warn or downgrade.
5. **Dry run.** Return the plan and the bill without fetching.
6. **Execute, validate, persist.** Vendor errors are rejected *before* caching,
   so a failure never gets served for the rest of the TTL.

Because the cache sits at the HTTP boundary rather than around domain objects,
the fix for a wrong number is always `zipf db rebuild`, never an `UPDATE`.

### Staleness

| Capability | Tier | TTL |
|---|---|---|
| `autocomplete.suggest` | 0 | 90d |
| `gsc.search_analytics` | 0 | 1d |
| `gsc.sites` | 0 | 1d |
| `dataforseo.user_data` | 0 | 15m |
| `labs.search_volume` | 1 | 30d |
| `labs.ranked_keywords` | 1 | 7d |
| `labs.domain_intersection` | 1 | 7d |

### Cost

DataForSEO Labs, measured against the live API: **$0.012 per call plus $0.00012
per row.** The base dominates by 100×, so one hundred keywords cost $0.024
batched and $1.21 one at a time.

Two consequences shape the design: paid tiers batch aggressively, and `vol` reads
the `keyword` projection first so it buys only the terms that actually went
stale. Free tiers do the opposite — one item per call, for independent TTLs.

Freshness for volume joins to `raw_response`, because `keyword.updated_at` is
also set by free discovery. Without that join a keyword autocomplete merely
*suggested* would look freshly *measured*, and its volume would never be bought.

### Clustering

A real 100-row gap pull returned 50 distinct queries and 50 restatements of them
— fifteen rows reading `button in html with css`, `html css button`, `css html
buttons`, all at 22,200 volume, all ranking the same URL.

`vol` and `gap` collapse those into one row, keyed on the **token set**: two
keywords built from the same words, ignoring order, filler words, and plurals,
are the same query. Genuinely narrower queries stay separate — `svg file` and
`svg file type` do not merge. `--flat` shows every phrasing.

Volume is the **maximum** across variants, never the sum. Adding fifteen
restatements of a 22,200 keyword would report demand that does not exist.

Clustering is display-only; the purchase is deliberately not deduplicated. A
redundant row costs $0.00012 against a $0.012 base, so collapsing the batch would
save fractions of a cent and discard data you asked for.

### Invariants

Enforced by tests in `tests/invariants/`, and by the database where possible.

- **R1** Nothing reaches the network outside `fetch()`. The one documented
  exception is OAuth token exchange, which returns a credential rather than
  vendor data and is named in an allowlist.
- **R2** `raw_response` is append-only — enforced by SQLite triggers, not by
  convention.
- **R3** Projections are never hand-written. `rebuild()` twice must produce
  byte-identical tables.
- **R4** History is never pruned. Nobody will sell you last Tuesday's rankings
  retroactively.
- **R5** No request spends money synchronously. Commands enqueue; the runner
  spends.

---

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

168 tests, ruff and mypy strict clean.

**A default `pytest` run makes zero network calls and costs $0.** An autouse
fixture fails any request that a test has not explicitly mocked. Tests marked
`live` are skipped unless `ZIPF_LIVE=1`.

Everything runs against a scratch database via `ZIPF_HOME`, so tests never touch
your real one.

### Error messages

Errors carry two parts: the problem, and what to do about it. The CLI prints them
on separate lines.

```console
$ zipf gap example.com
error own_domain is not configured.
      Set own_domain in ~/.config/zipf/config.toml, or pass --mine on this command.
```

Messages never name an invariant or a design decision — a reader holding a shell
prompt cannot act on "R2". A test fails the suite if any error leaks an
`R`- or `D`-numbered tag, and the append-only triggers say what to run instead:

```console
$ sqlite3 ~/.local/share/zipf/zipf.db "DELETE FROM raw_response"
Stored responses cannot be deleted. Nobody will sell you this data again, so it
is kept permanently. Projection tables are safe to delete; rebuild restores them.
```

---

## Not built yet

The terminal UI, SERP and AI Overview capture, the LLM visibility panel, and the
MCP server.

There is also no home for data you author — no publishing status, no notes. Every
table today holds either paid bytes or something derived from them, and `rebuild`
would erase anything hand-written. That is the missing half of PRD problem #4,
*"did the thing I published actually work"*.

Traffic estimates and a proprietary difficulty score are permanent non-goals —
see `dev/prd.md` for why.
