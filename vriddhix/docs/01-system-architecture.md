# Tathya — System Architecture

## 1. What this system is

A quantitative research system for Indian equities. It discovers, measures and
records chart structure (VCP bases, market structure breaks, fair value gaps),
places each finding in market and sector context, scores it, tracks what
happened next, and lets the same rules be replayed over history.

It is **not** a prediction machine. Every number it shows is the output of a
deterministic calculation over data that existed at the time, and every number
can be traced back to that calculation.

## 2. The one rule that constrains every other decision

> **Detection logic has exactly one implementation.**

The live scanner, the backtester, the historical X-Ray and the research
database all call the *same* function objects. Not "the same algorithm
reimplemented consistently" — the same code.

```
                    ┌──────────────────────────┐
                    │   vriddhix.engines.*     │
                    │  (pure, stateless,       │
                    │   DataFrame in/out)      │
                    └────────────┬─────────────┘
                                 │
        ┌──────────────┬─────────┴────────┬──────────────┐
        ▼              ▼                  ▼              ▼
  Live Scanner    Backtester       Pattern X-Ray     Research
  (daily job)    (historical)      (per symbol)      (queries)
```

This is enforced structurally, not by convention:

- Engines live in `vriddhix.engines` and may import **only** from
  `vriddhix.domain`, `vriddhix.features`, `numpy`, `pandas`.
- Engines may not import `vriddhix.db`, `vriddhix.api`, `vriddhix.jobs`, or
  anything that reads configuration from a live process.
- Engines take `(df: pd.DataFrame, config: EngineConfig)` and return typed
  results. They never perform I/O.
- A test (`test_engine_purity.py`) walks the AST of every module under
  `vriddhix/engines/` and fails the build if a forbidden import appears.

The consequence: a backtest cannot silently drift from live behaviour, because
there is no second implementation to drift from.

## 3. Layered structure

```
┌───────────────────────────────────────────────────────────────┐
│ L6  PRESENTATION      Next.js frontend · FastAPI HTTP layer   │
├───────────────────────────────────────────────────────────────┤
│ L5  ORCHESTRATION     daily scan pipeline · backtest runner   │
│                       alert evaluation · Celery workers       │
├───────────────────────────────────────────────────────────────┤
│ L4  SERVICES          scanning · scoring · backtest · alerts  │
│                       (stateful, DB-aware, config-aware)      │
├───────────────────────────────────────────────────────────────┤
│ L3  ENGINES           VCP · SMC · FVG · RS · Sector · Regime  │
│                       PURE. No I/O. One source of truth.      │
├───────────────────────────────────────────────────────────────┤
│ L2  FEATURES          EMA · ATR · returns · 52w · volume      │
│                       PURE. Vectorised. Deterministic.        │
├───────────────────────────────────────────────────────────────┤
│ L1  DATA              providers · ingestion · validation      │
│                       universe · snapshots · normalisation    │
├───────────────────────────────────────────────────────────────┤
│ L0  PERSISTENCE       PostgreSQL · Redis · Parquet/object     │
└───────────────────────────────────────────────────────────────┘
```

Dependencies point **downward only**. L3 never imports L4.

## 4. Runtime topology

```
                          ┌─────────────┐
     Browser ────────────▶│  Next.js    │
                          │  (SSR/RSC)  │
                          └──────┬──────┘
                                 │ HTTP
                          ┌──────▼──────┐        ┌──────────┐
                          │  FastAPI    │───────▶│  Redis   │
                          │  (read API) │◀───────│  (cache) │
                          └──────┬──────┘        └────▲─────┘
                                 │                    │
                          ┌──────▼────────────────────┴─────┐
                          │        PostgreSQL               │
                          │  (+ TimescaleDB for OHLCV)      │
                          └──────▲──────────────────────────┘
                                 │
              ┌──────────────────┴──────────────────┐
              │          Celery workers             │
              │  ingest · features · scan · score   │
              │  backtest · alerts · AI explain     │
              └──────────────────▲──────────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │   Data providers        │
                    │  yfinance · broker      │
                    │  adapters · CSV cache   │
                    └─────────────────────────┘
```

**The API never computes.** A request to `/scanners/vcp` reads precomputed rows.
If the daily scan has not run, the endpoint says so — it does not scan 500
symbols inline. This is what makes the sub-2-second page target achievable
rather than aspirational.

## 5. Write path (nightly) vs read path (interactive)

The two paths are deliberately separated:

| | Write path | Read path |
|---|---|---|
| Trigger | Scheduled, post-close | HTTP request |
| Runs | Celery workers | FastAPI |
| Duration budget | Minutes | < 2s |
| Touches | Providers, engines, DB writes | DB reads, Redis |
| Failure mode | Retry, alert, partial-complete | 503 with last-good timestamp |

An interactive request must never trigger ingestion or pattern detection.

## 6. Determinism and versioning

Every stored result records the versions of the code that produced it:

```
engine_version    VCP_ENGINE_V1.0
rule_version      SCORING_V1.0
universe_version  NIFTY500_2026Q1
```

Changing a rule does **not** rewrite history. It produces new rows under a new
version, and the UI can say "scored under V1.0" or "rescored under V1.1". A
result whose inputs cannot be reconstructed is a result that cannot be trusted.

## 7. Point-in-time correctness

Three distinct hazards, each handled explicitly:

**Look-ahead.** Engines receive a DataFrame truncated at the evaluation
timestamp. The truncation happens in L4 (services), not L3, so an engine
physically cannot see a future bar — the rows are not in the frame. Verified by
`test_no_lookahead.py`, which runs a scan as of date `T`, appends data after
`T`, re-runs, and asserts byte-identical output.

**Survivorship.** `universe_snapshots` stores index membership with
`effective_from` / `effective_to`. A backtest resolves membership per
rebalance date. Where snapshots do not exist for a period, the run is labelled
`CURRENT_UNIVERSE` and the UI displays a survivorship warning — the bias is
surfaced, not silently absorbed.

**Leakage through features.** Feature calculation is strictly causal: an EMA at
bar `t` uses bars `≤ t`. Centred windows, `shift(-n)`, and full-series
normalisation (z-scores against the whole history) are prohibited in L2 and
checked by test.

## 8. Data flow — nightly

```
 market close
      │
      ▼
 [1] ingest        providers → raw OHLCV → staging
      │
      ▼
 [2] validate      gaps · duplicates · OHLC sanity · volume · splits
      │            failures → data_quality_events (never silently dropped)
      ▼
 [3] normalise     corporate actions → adjusted series
      │
      ▼
 [4] features      EMA/ATR/returns/52w/rel-volume → technical_features
      │
      ▼
 [5] structure     swings → BOS/CHoCH → order blocks → FVG
      │
      ▼
 [6] patterns      VCP bases → contractions → pivot
      │
      ▼
 [7] context       RS ranks → sector metrics → market breadth → regime
      │
      ▼
 [8] score         composite setup score (needs [6] and [7])
      │
      ▼
 [9] lifecycle     advance pattern states · detect breakouts · detect failures
      │
      ▼
 [10] persist      write results under (scan_id, engine_version)
      │
      ▼
 [11] cache        warm Redis for dashboard/scanner reads
```

Steps 4–6 are per-symbol and embarrassingly parallel. Step 7 needs the whole
universe and therefore acts as a barrier. Step 8 depends on both.

## 9. Technology choices and why

| Layer | Choice | Reason |
|---|---|---|
| Frontend | Next.js + TypeScript + Tailwind + shadcn/ui | Server components render data-dense tables without shipping the whole dataset |
| API | FastAPI + Pydantic | Response models are the API contract, typed and validated at the boundary |
| Compute | Python + pandas/numpy | Vectorised per-symbol work; the same array code runs live and in backtest |
| OLTP | PostgreSQL | Relational integrity for patterns/lifecycle/trades |
| Time series | TimescaleDB hypertable on `ohlcv_daily` | Compression and time-bucketed queries at scale |
| Cache | Redis | Precomputed dashboard/scanner payloads |
| Bulk history | Parquet | Backtests read columnar files, not row-by-row SQL |
| Queue | Celery | Mature, supports the fan-out/barrier shape of step 7 |

### Local development deviation (recorded honestly)

PostgreSQL is **not installed** on the current development machine. The system
therefore runs on SQLite locally via a configurable `VRIDDHIX_DATABASE_URL`.
Schema and migrations are written to the portable subset (no SQLite-only
types, explicit `Numeric`, timezone-aware `DateTime`, named constraints) so
moving to PostgreSQL is a URL change plus the Timescale hypertable statement
in `migrations/versions/0001_*.py::upgrade_timescale()`, which is a no-op on
SQLite. Tests run on SQLite by design — they must be fast and hermetic.

This is a deployment-target difference, not an architectural one.

## 10. Where AI sits

AI is confined to L6. It receives structured rows that L3 already computed and
turns them into prose. It has no path to the number-producing code:

```
  engines ──▶ DB rows ──▶ AI prompt ──▶ explanation text ──▶ ai_explanations
                              ▲
                              │  (read-only; cannot write scores)
```

The AI layer cannot invent a market fact, because the only market facts in its
prompt are the ones passed to it, and its output is stored as commentary
alongside — never in place of — the computed values.

## 11. Failure posture

- A provider outage fails ingestion for the affected symbols and records
  `data_quality_events`; the scan proceeds for the rest and reports partial
  completion. Yesterday's data stays visible, timestamped.
- An engine exception fails that symbol, not the scan.
- A failed breakout is never deleted. Failure records are the most valuable
  rows in the database for research purposes.
- The dashboard always states the age of what it is showing.
