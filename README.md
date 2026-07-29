# zipf

Single-operator keyword research over a local SQLite cache.

Zipf answers *"what should I write about, and did it work"* using search data it
buys once and owns forever. Commercial SEO platforms charge $130–150/month for
data available wholesale for under $20; the gap is product, not data.

**Zipf is not a keyword tool. It is a cache with opinions about staleness.**
Every feature is a cache hit; every dollar spent is a cache miss.

---

## Status

Working today: **M0–M2**. Free and paid tiers run end to end from the CLI, with
price declaration, a monthly ceiling, and asynchronous spend.

| Milestone | State |
|---|---|
| M0 The metered loop | done |
| M1 Free tier — autocomplete, Search Console | done |
| M2 Paid tier — volume, gap, jobs, budget | done |
| M3 Terminal UI | not started |
| M4 SERP and AI Overview | not started |
| M5 LLM visibility panel | not started |
| M6 MCP server | not started |

Both paid capabilities have been verified against the live DataForSEO API, with
recorded cost reconciled against the vendor balance to the cent and zero
estimator drift.

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

Free commands never prompt. Paid commands price the request first, confirm, then
enqueue — nothing spends money inline.

```
zipf init                                    create db, run migrations, scaffold config
zipf suggest <seed> [--questions] [--alphabet]   tier 0, free
zipf gsc auth | sites | import [--days N]    tier 0, free
zipf vol <keyword>... [--dry-run] [--flat]   tier 1, paid
zipf gap <competitor> [--mine D] [--limit N] tier 1, paid
zipf jobs run | list | cancel <id>           the work queue
zipf budget [--cached]                       spend, ceiling, and vendor balance
zipf db rebuild [--capability NAME]          replay projections from stored bytes
```

Paid commands take `--dry-run` (print the plan, spend $0), `--yes` (skip the
prompt), `--force` (ignore a fresh cache entry), and `--wait` (drain the queue
before returning).

### Example

```console
$ zipf vol "best crm software" --dry-run
1 keyword(s) · 0 still fresh · 1 to buy in 1 call(s)
dry run would buy 1 keyword(s) for $0.01212 · spent $0.00

$ zipf gap joshwcomeau.com --limit 100
╭─ pull keyword gap · joshwcomeau.com ──────────╮
│   joshwcomeau.com ranks for, mine.dev does not│
│   ~100 rows              not cached           │
│   $0.0240                tier 1, none queue   │
│   remaining this month: $19.99                │
│   vendor balance:       $0.95                 │
╰───────────────────────────────────────────────╯
pull [y/N]: y
queued job 3

$ zipf jobs run
job 3 done: labs.domain_intersection · $0.02400

$ zipf budget
spent    $0.04/$20.00  ░░░░░░░░░░  0% of ceiling
balance  $0.93 at DataForSEO live
confirms every spend
```

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

**A default `pytest` run makes zero network calls and costs $0.** An autouse
fixture fails any request that a test has not explicitly mocked. Tests marked
`live` are skipped unless `ZIPF_LIVE=1`.

Everything runs against a scratch database via `ZIPF_HOME`, so tests never touch
your real one.

---

## Not built yet

The terminal UI, SERP and AI Overview capture, the LLM visibility panel, and the
MCP server. Traffic estimates and a proprietary difficulty score are permanent
non-goals — see `dev/prd.md` for why.
