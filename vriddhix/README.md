# VriddhiX

Pattern intelligence and quantitative research for Indian equities.

A research system, not a prediction machine. It discovers and measures chart
structure, places it in market and sector context, scores it, records what
happened next, and lets the same rules be replayed over history. Every number
it reports is the output of a deterministic calculation over data that existed
at the time.

**Status: Phases 1-10 implemented.** Specifications for every phase are in
`docs/`.

## What works today

```
config       typed, validated, no trading constant in code
database     32 tables, 5 Alembic migrations, Postgres/Timescale-ready
domain       shared vocabulary + pattern lifecycle state machine
providers    CSV cache + yfinance, chained with failover
quality      7 validation rules, nothing silently dropped or repaired
universe     point-in-time membership with survivorship flagging
features     EMA/ATR/returns/52w/relative-volume, strictly causal
ingestion    idempotent, partial-failure tolerant

engines      VCP · swings · SMC (BOS/CHoCH/order blocks/liquidity) · FVG
             relative strength · sector rotation · market regime
services     scanner · lifecycle · failure engine · X-ray · conditions
             backtest (costs, sizing, walk-forward, Monte Carlo) · alerts
             context + structure persistence
jobs         the daily pipeline, catch-up backfill, and the backtest worker
api          43 read endpoints, every payload carrying provenance
auth         scrypt password hashing, signed tokens, throttled login
web          landing, signup and learn pages served from the same process
ai           explanation layer: server-built payloads, screened output
```

Verified against real NSE history: 23 symbols, 37,881 bars and feature rows,
2019-12 to 2026-09, with the full pipeline run end to end — a breakout
detected, tracked and confirmed five sessions later.

## Quick start

```bash
pip install -r requirements.txt
export PYTHONPATH=src

python -m vriddhix.cli init         # apply migrations
python -m vriddhix.cli bootstrap    # seed sectors, stocks, index membership
python -m vriddhix.cli ingest       # fetch, validate, persist bars + features
python -m vriddhix.cli scan         # run every engine, persist the results
python -m vriddhix.cli backfill     # fetch new bars, scan every missing session
python -m vriddhix.cli backtests    # run whatever backtests are queued
python -m vriddhix.cli status       # what is in the database
python -m vriddhix.cli serve        # the read API on :8000

pytest                              # 433 tests
```

`scan --since 2026-08-01` replays the pipeline day by day, oldest first. The
order matters: RS trend and regime hysteresis each read the previous stored
value, so running dates out of order would measure every day against a future
baseline.

Once `serve` is running: `/` is the landing page, `/learn` explains what each
engine measures, `/signup` creates an account, and `/api/docs` is the API
reference.

`deploy/` holds a launchd agent that runs `backfill` on weekday evenings. It
catches up rather than doing "today": miss a week and the next run scans all
five missing sessions, oldest first. See `deploy/README.md`.

## Three commitments the design is built around

**One implementation of every rule.** The live scanner, the backtester, the
historical X-Ray and the research queries call the same function objects —
not the same algorithm written twice. A backtest cannot drift from live
behaviour because there is no second implementation to drift from. Enforced by
an import-purity test, not by convention.

**Nothing may see the future.** A value dated `t` depends only on bars dated
`≤ t`. This is checked by truncating a frame at `T`, recomputing, and requiring
byte-identical output — on synthetic data, on real NSE history, and across an
unadjusted 1:1 bonus issue where an accidental look-ahead would be most
visible. A causality bug does not crash; it produces a backtest that looks
excellent and means nothing.

**The database says what it does not know.** Universe membership without real
history is marked `CURRENT_UNIVERSE` and carries a survivorship warning into
every run that consumes it. A regime computed from too little of the universe
returns nothing rather than a confident number from a biased sample. Failed
breakouts are never deleted.

The same rule runs through every stored column. An unranked sector's RS score
is `null`, not `0.0` — "no standing" and "ranked last" are different claims.
A breadth reading nobody computed is `null`, not `0%`. A win rate over zero
resolved trades is `null`, not `0.0`, which would read as "this has never
worked". The API preserves each of those nulls rather than coercing them, so a
chart cannot quietly turn an absence of data into a bearish reading.

## Layout

```
config/         all tunables
docs/           10 specifications (architecture -> test strategy)
migrations/     Alembic, 4 revisions
src/vriddhix/
  domain/       vocabulary, lifecycle state machine
  db/           models and session handling
  data/         providers, quality, ingestion
  universe/     point-in-time membership
  features/     L2 — pure, causal, vectorised
  engines/      L3 — THE source of truth for detection
  services/     L4 — stateful orchestration and persistence
  jobs/         L5 — daily pipeline, catch-up backfill, backtest worker
  api/          L6 — FastAPI (reads), auth, and the public pages
  ai/           L6 — explanation, never computation
tests/          433 tests
```

Dependencies point downward only: `engines` may never import `db`, `api` or
`services`. Enforced by `tests/test_engine_purity.py`, which walks the AST —
not by convention. See `docs/02-repository-structure.md`.

## Documentation

| | |
|---|---|
| `01-system-architecture.md` | Layers, runtime topology, determinism |
| `02-repository-structure.md` | Layout and import rules |
| `03-database-erd.md` | All tables, present and planned |
| `04-data-model.md` | Vocabulary, frame contract, causality rules |
| `05-vcp-specification.md` | VCP detection, precisely enough to implement |
| `06-smc-fvg-specification.md` | Swings, BOS, CHoCH, order blocks, FVG |
| `07-market-regime-specification.md` | Breadth and regime |
| `08-rs-sector-specification.md` | Relative strength and sector rotation |
| `09-api-contracts.md` | HTTP surface |
| `10-test-strategy.md` | What is tested and why |

## Deployment note

The target is PostgreSQL with a TimescaleDB hypertable on `ohlcv_daily`.
PostgreSQL is not installed on the current development machine, so local runs
and tests use SQLite via `VRIDDHIX_DATABASE_URL`. The schema stays in the
portable subset and the hypertable conversion is a no-op off PostgreSQL, so
this is a URL change rather than a migration rewrite.

## Backtests

A backtest replays **stored patterns**, not a fresh run of the detector. The
daily scan already recorded what each engine saw on each date under the engine
version that produced it; re-detecting inside the backtester would be the
second implementation the architecture forbids, and it would quietly apply
today's engine to yesterday's question. Context is read as it was on that date
too — joining today's relative strength onto a 2019 signal is a look-ahead no
test of the engines would catch, because the engines never see it.

Entry rules run through the same condition evaluator as the live screener, so
a strategy cannot mean one thing on the dashboard and another in its own
backtest. Fills are at the next open, never the signal close. Costs are the
Indian statutory set — STT, stamp duty, exchange, SEBI, GST — and every rate
is configurable because they change.

## The API in one paragraph

Every endpoint is a projection of rows the daily scan already wrote. None of
them runs an engine: a read that computes can time out, can disagree with
yesterday's answer for the same date, and can show two users different numbers
for the same query. When the data is not there the answer is a typed
`SCAN_NOT_RUN` (503), never an empty list — a UI cannot tell `[]` apart from
"nothing set up today". Stale data is returned WITH its age rather than
refused, because stale plus a visible age is more useful than empty.

Scanner rows, condition trees and the backtester's row shape share one
vocabulary (`vcp_score`, not `score` in one place and `vcp_score` in another).
Two names for one value is how a screen and the backtest that is supposed to
replay it start to disagree.

## Accounts

Passwords are hashed with scrypt and a per-account random salt; the plaintext
is never stored and cannot be recovered, only reset. A failed login gives one
message whether or not the account exists, because the difference tells an
attacker which addresses are worth attacking. Repeated failures are throttled
per address — scrypt raises the cost of each guess, but only a limit makes
millions of them impractical. A NULL password hash never authenticates.

Tokens are HS256, signed with `VRIDDHIX_API_JWT_SECRET`. Without that set the
server refuses to issue one rather than falling back to something unsigned
that `deps.py` would accept in development.

## Not built

The dashboard. `/`, `/learn` and `/signup` are served from this process, and
`docs/09` is the contract a full application would consume, but the scanner
tables, chart panels and pattern X-Ray views described in the master
specification are not built — the API serves their data today.

No Celery, no Redis. The daily pipeline runs under launchd and the backtest
worker is a CLI command; both are single-process and synchronous, which is
honest for one machine and one universe. Distributing them is a deployment
change, not a rewrite.
