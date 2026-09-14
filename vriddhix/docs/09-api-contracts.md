# VriddhiX — API Contracts

FastAPI, JSON, prefix `/api/v1`. Implemented from Phase 6; specified now so
the frontend and the services layer agree before either is written.

## 1. Rules that apply to every endpoint

**Reads are precomputed.** No endpoint runs a scan, an engine, or an
ingestion. If the data is not there, the endpoint says so — it does not
compute it inline while the user waits.

**Every payload carries provenance.** Any response containing derived numbers
includes:

```json
{
  "as_of": "2026-09-14",
  "engine_version": "VCP_ENGINE_V1.0",
  "rule_version": "SCORING_V1.0",
  "computed_at": "2026-09-14T18:42:11+05:30",
  "is_stale": false
}
```

A client that cannot tell how old a number is will eventually present a
three-day-old score as live.

**Staleness is explicit, not an error.** If the last scan is older than
`api.stale_after_hours` (default 20), `is_stale: true` and the UI shows the
age banner. The data is still returned — stale is more useful than empty.

**Errors are typed.**

```json
{ "error": { "code": "SCAN_NOT_RUN", "message": "...", "detail": {...} } }
```

| Code | HTTP | Meaning |
|---|---|---|
| `NOT_FOUND` | 404 | Unknown symbol / id |
| `SCAN_NOT_RUN` | 503 | No scan for the requested date |
| `INSUFFICIENT_COVERAGE` | 503 | Universe coverage below threshold |
| `INVALID_PARAMS` | 422 | Validation failure |
| `RATE_LIMITED` | 429 | Quota exceeded |
| `BACKTEST_RUNNING` | 202 | Async job still in progress |

**Pagination**: `?limit=` (default 50, max 500) and `?cursor=`, with
`next_cursor` in the response. Offset pagination is not used — scanner results
shift between pages as scores update.

**Auth**: `Authorization: Bearer <jwt>`. User-scoped resources are filtered by
the token's `user_id` in the repository layer, never by a query parameter.

## 2. Market

```http
GET /api/v1/market
```
```json
{
  "as_of": "2026-09-14",
  "regime": {
    "regime": "BULL", "score": 81, "confidence": 78,
    "components": [
      {"name": "trend", "raw": 88, "weight": 0.30, "contribution": 26.4},
      {"name": "breadth", "raw": 79, "weight": 0.25, "contribution": 19.75}
    ],
    "pending_regime": null
  },
  "breadth": {
    "advancers": 312, "decliners": 178, "ad_ratio": 1.75,
    "pct_above_ema_20": 74.2, "pct_above_ema_50": 66.1,
    "pct_above_ema_200": 59.4,
    "new_highs_52w": 41, "new_lows_52w": 6,
    "breakouts": 18, "failed_breakouts": 4,
    "universe_size": 492
  },
  "engine_version": "REGIME_ENGINE_V1.0", "is_stale": false
}
```

```http
GET /api/v1/market/breadth?from=2026-06-01&to=2026-09-14&metrics=pct_above_ema_50,ad_ratio
GET /api/v1/market/regime?from=&to=          # historical regime series
GET /api/v1/indices
```

## 3. Sectors

```http
GET /api/v1/sectors?as_of=&sort=rs_score&order=desc
```
```json
{
  "as_of": "2026-09-14",
  "sectors": [{
    "sector": {"id": 4, "code": "CAPGOODS", "name": "Capital Goods"},
    "rs_score": 92, "rs_rank": 1, "momentum": 11.4, "quadrant": "LEADING",
    "ret_1m": 0.082, "ret_3m": 0.214, "ret_6m": 0.378,
    "pct_above_ema_20": 86.4, "pct_above_ema_50": 79.1, "pct_above_ema_200": 71.0,
    "new_highs": 12, "new_lows": 0,
    "breakouts": 4, "failed_breakouts": 0,
    "avg_pattern_score": 71.2,
    "constituent_count": 28, "low_sample": false
  }],
  "engine_version": "SECTOR_ENGINE_V1.0"
}
```

```http
GET /api/v1/sectors/{code}                    # detail + constituents
GET /api/v1/sectors/rotation?as_of=           # quadrant matrix payload
```

## 4. Stocks

```http
GET /api/v1/stocks?index=NIFTY500&sector=CAPGOODS&limit=50&cursor=
GET /api/v1/stocks/{symbol}
```
```json
{
  "stock": {"symbol": "RELIANCE", "name": "Reliance Industries",
            "exchange": "NSE", "sector": "Energy", "industry": "Refineries"},
  "quote": {"date": "2026-09-14", "close": 2840.5, "volume": 8412334,
            "change_pct": 1.24},
  "features": {"ema_20": 2790.1, "ema_50": 2705.4, "ema_200": 2480.9,
               "atr_14": 48.2, "atr_pct": 1.70, "rel_volume": 1.42,
               "high_52w": 3024.0, "low_52w": 2201.5,
               "pct_from_52w_high": -6.07},
  "rs": {"rs_score": 95, "rs_rank": 24, "rs_trend": "IMPROVING",
         "rs_vs_sector": 88, "ret_1m": 0.041, "ret_3m": 0.118,
         "coverage": ["ret_1m","ret_3m","ret_6m","ret_12m"]},
  "active_pattern": {
    "id": 88213, "type": "VCP", "status": "NEAR_PIVOT", "score": 91,
    "pivot_price": 2884.0, "distance_from_pivot_pct": 1.53,
    "base_start": "2026-06-02", "base_end": "2026-09-14",
    "base_depth_pct": 17.2, "contractions": 4
  },
  "context": {"market_regime": "BULL", "sector_rank": 2},
  "as_of": "2026-09-14", "is_stale": false
}
```

```http
GET /api/v1/stocks/{symbol}/chart?timeframe=D1&from=&to=&overlays=ema,vwap,rs_line
GET /api/v1/stocks/{symbol}/patterns?status=&from=&to=
GET /api/v1/stocks/{symbol}/xray
GET /api/v1/stocks/{symbol}/structure?timeframe=D1     # swings, BOS, CHoCH, OB, FVG
```

`/chart` returns OHLCV plus overlay series **and** the annotation geometry
(base rectangle, contraction spans, pivot line, BOS/CHoCH markers, FVG boxes)
so the frontend draws from the same numbers the engine produced rather than
re-deriving them in TypeScript — a second implementation is exactly what §2 of
the architecture prohibits.

## 5. Scanners

```http
GET /api/v1/scanners/vcp?as_of=&min_score=70&stage=NEAR_PIVOT
                        &sector=&min_rs=80&max_pivot_distance_pct=5
                        &sort=score&limit=50&cursor=
```
```json
{
  "as_of": "2026-09-14", "total": 37,
  "results": [{
    "symbol": "RELIANCE", "close": 2840.5,
    "pattern_type": "VCP", "vcp_stage": "NEAR_PIVOT", "vcp_score": 91,
    "score_components": [
      {"name":"prior_trend","raw":94,"weight":0.15,"contribution":14.1},
      {"name":"contraction_quality","raw":96,"weight":0.20,"contribution":19.2}
    ],
    "grade": "A_PLUS",
    "rs_score": 95, "sector": "Energy", "sector_rank": 2,
    "pivot_price": 2884.0, "distance_from_pivot_pct": 1.53,
    "rel_volume": 1.42, "market_regime": "BULL",
    "pattern_id": 88213
  }],
  "next_cursor": "eyJzY29yZSI6ODguMSwiaWQiOjg4MjE0fQ==",
  "engine_version": "VCP_ENGINE_V1.0", "rule_version": "SCORING_V1.0"
}
```

```http
GET /api/v1/scanners/smc?direction=BULLISH&min_confluence=70&timeframe=H4
GET /api/v1/scanners/fvg?status=OPEN&timeframe=D1&direction=BULLISH
GET /api/v1/scanners/breakouts?as_of=&confirmed=true
GET /api/v1/scanners/failed-breakouts?from=&to=
```

`score_components` on every row is non-negotiable — it is what makes the
"WHY THIS SETUP?" panel a projection of stored data rather than a second
computation.

Row keys use the **same names as `SetupRow` fields** — `vcp_score`, not
`score`. The scanner payload, the condition-tree vocabulary and the
backtester's row shape are one vocabulary, so a screen saved against
`vcp_score` means the same thing wherever it is applied. Two names for one
value is how the screen builder and the backtester start to disagree.

## 6. Breakout ledger

```http
GET /api/v1/breakouts?from=&to=&status=&sector=&min_score=
GET /api/v1/breakouts/{id}
```
```json
{
  "id": 5512, "symbol": "TITAN", "pattern_id": 88100,
  "breakout_date": "2026-08-21", "breakout_price": 3412.0,
  "pivot_price": 3398.0, "volume": 4120334, "rel_volume": 2.1,
  "rs_at_breakout": 93, "sector_rank_at_breakout": 3,
  "regime_at_breakout": "BULL", "setup_score": 88,
  "status": "FAILED",
  "outcome": {
    "mfe_pct": 4.2, "mae_pct": -7.8, "days_held": 9,
    "return_pct": -6.1, "failed": true,
    "failure_date": "2026-09-01", "failure_reason": "CLOSE_BELOW_PIVOT",
    "days_to_failure": 9, "drawdown_pct": -7.8
  }
}
```

Failed breakouts are first-class rows with the same shape as successes. No
endpoint filters them out by default.

## 7. Screens, strategies, backtests

```http
POST /api/v1/screens            # {name, conditions: <condition tree>}
GET  /api/v1/screens
POST /api/v1/screens/{id}/run   # → results, same shape as /scanners/*

POST /api/v1/strategies         # {name, entry, exit, sizing, risk}
GET  /api/v1/strategies/{id}/versions

POST /api/v1/backtests          # → 202 {run_id, status: "QUEUED"}
GET  /api/v1/backtests/{run_id}
GET  /api/v1/backtests/{run_id}/trades?limit=&cursor=
GET  /api/v1/backtests/{run_id}/metrics
GET  /api/v1/backtests/compare?run_ids=1,2,3
```

The condition tree is recursive and serialisable:

```json
{"op": "AND", "children": [
  {"field": "vcp_score", "cmp": "gt", "value": 80},
  {"field": "rs_score",  "cmp": "gt", "value": 85},
  {"op": "OR", "children": [
    {"field": "sector_quadrant", "cmp": "eq", "value": "LEADING"},
    {"field": "sector_rs",       "cmp": "gt", "value": 70}
  ]}
]}
```

The same tree is evaluated by the scanner and the backtester — one evaluator,
per the architecture rule.

`GET /backtests/{run_id}/metrics` always includes:

```json
{
  "survivorship_mode": "CURRENT_UNIVERSE",
  "survivorship_warning": "Universe membership history unavailable before 2024-01-01; results may overstate performance."
}
```

A backtest that cannot be point-in-time says so in its own payload, so the
warning travels with the numbers rather than living in documentation nobody
reads.

## 8. Watchlists and alerts

```http
GET/POST      /api/v1/watchlists
GET/POST/DEL  /api/v1/watchlists/{id}/items
GET/POST/DEL  /api/v1/alerts
GET           /api/v1/alerts/triggered?from=&to=
```

## 9. AI

```http
POST /api/v1/ai/explain
```
```json
{ "subject_type": "PATTERN", "subject_id": 88213 }
```

The server builds the payload from stored rows; the client cannot inject
market facts. Response:

```json
{
  "explanation": "The setup scores highly because ...",
  "inputs": { "...exactly what the model was shown..." },
  "model": "claude-opus-5", "generated_at": "...",
  "engine_version_of_inputs": "VCP_ENGINE_V1.0",
  "disclaimer": "Explanation of computed measurements. Not investment advice."
}
```

`inputs` is echoed so any claim in the prose can be checked against the data
that produced it.

```http
POST /api/v1/ai/research     # {question} → structured query + narrated result
```

The research assistant translates a question into a **query against stored
tables** and narrates the rows returned. It has no free-text path to market
data and cannot answer a question the database cannot answer — it says so
instead.

## 10. Operations

```http
GET  /api/v1/ops/health          # db, redis, worker, last scan age
GET  /api/v1/ops/scans?limit=20
GET  /api/v1/ops/data-quality?severity=ERROR&from=
POST /api/v1/ops/scans/run       # admin only; enqueues, returns 202
```
