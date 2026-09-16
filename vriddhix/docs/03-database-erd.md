# Tathya — Database ERD

Tables marked **[P1]** are created by migration `0001_phase1_foundation`.
The rest are specified here so later phases extend a known design.

## 1. Reference & universe [P1]

```
   sectors ──1───∞── industries ──1───∞── stocks
                                            │
                                            │ 1
                                            │
                                            ∞
                                     universe_members
                                            ∞
                                            │
                                            │ 1
                                        indices
```

```
sectors            [P1]
  id               PK
  code             UNIQUE   e.g. "ENERGY"
  name
  created_at

industries         [P1]
  id               PK
  sector_id        FK → sectors.id
  code             UNIQUE
  name
  UNIQUE (sector_id, code)

stocks             [P1]
  id               PK
  symbol           UNIQUE   NSE trading symbol, e.g. "RELIANCE"
  name
  isin             UNIQUE NULL
  exchange                  "NSE" | "BSE"
  industry_id      FK → industries.id  NULL
  lot_size
  is_active                 delisted names stay, flagged false
  listed_on        NULL
  delisted_on      NULL
  created_at, updated_at
  INDEX (exchange, is_active), INDEX (industry_id)

indices            [P1]
  id               PK
  code             UNIQUE   "NIFTY500" | "NIFTY200" | ...
  name
  is_benchmark              exactly one true; RS denominator default

universe_members   [P1]   ← point-in-time membership. The survivorship fix.
  id               PK
  index_id         FK → indices.id
  stock_id         FK → stocks.id
  effective_from   DATE  NOT NULL
  effective_to     DATE  NULL       NULL = still a member
  source                            "declared" | "inferred" | "current_only"
  UNIQUE (index_id, stock_id, effective_from)
  INDEX (index_id, effective_from, effective_to)

universe_snapshots [P1]   ← named, frozen resolutions for reproducibility
  id               PK
  index_id         FK → indices.id
  version          UNIQUE   "NIFTY500_2026Q1"
  as_of            DATE
  member_count
  is_complete               false ⇒ survivorship warning in UI
  created_at
```

`universe_members` answers "who was in Nifty 500 on 2021-03-04?" without a
join to today's list. When real history is unavailable the rows are written
with `source='current_only'` and the snapshot carries `is_complete=false`,
so a backtest run over that period is labelled rather than quietly biased.

## 2. Price data [P1]

```
ohlcv_daily        [P1]   ← TimescaleDB hypertable on (date)
  stock_id         FK → stocks.id   ┐ composite PK
  date             DATE             ┘
  open, high, low, close    NUMERIC(18,4)
  volume                    NUMERIC(20,2)
  adj_close                 NUMERIC(18,4)
  adj_factor                NUMERIC(18,8)  1.0 when unadjusted
  provider                  "yfinance" | "flattrade" | "dhan" | "csv_cache"
  ingested_at               TIMESTAMPTZ
  PRIMARY KEY (stock_id, date)
  INDEX (date)

ohlcv_intraday     [P4]   same shape + timeframe column, separate hypertable
  stock_id, ts, timeframe  ("5m"|"15m"|"30m"|"1h"|"4h")
  PRIMARY KEY (stock_id, timeframe, ts)
```

Raw and adjusted are both retained. `close` is what printed; `adj_close` is
what a continuous series needs. Discarding either makes one class of question
unanswerable later.

## 3. Derived features [P1]

```
technical_features [P1]
  stock_id         FK → stocks.id   ┐ composite PK
  date             DATE             ┘
  ema_20, ema_50, ema_100, ema_200      NUMERIC(18,4)
  atr_14, atr_pct                       NUMERIC(18,6)
  avg_volume_20, avg_volume_50          NUMERIC(20,2)
  rel_volume                            NUMERIC(12,4)   vol / avg_volume_50
  ret_1m, ret_3m, ret_6m, ret_12m       NUMERIC(12,6)
  high_52w, low_52w                     NUMERIC(18,4)
  pct_from_52w_high, pct_from_52w_low   NUMERIC(12,6)
  above_ema_20/50/100/200               BOOLEAN
  feature_version                       "FEATURES_V1.0"
  computed_at                           TIMESTAMPTZ
  PRIMARY KEY (stock_id, date)
  INDEX (date), INDEX (date, rel_volume)
```

Every column is causal at `date` — see `04-data-model.md §4`.

## 4. Market structure (SMC) [P4]

```
swing_points            stock_id, timeframe, date, price, swing_type(HIGH|LOW),
                        strength, left_bars, right_bars, confirmed_on,
                        engine_version
structure_breaks        stock_id, timeframe, date, kind(BOS|CHOCH), direction,
                        broken_level, broken_swing_date, close_price, volume,
                        prior_structure, engine_version
order_blocks            stock_id, timeframe, date, direction, upper, lower,
                        origin_break_date, status, mitigated_at
liquidity_events        stock_id, timeframe, date, kind(SWEEP|EQH|EQL), level,
                        direction, reclaimed, linked_break_date
```

Two deviations from the original sketch, both deliberate:

* **BOS and CHoCH share one table.** They are the same event shape and differ
  only in whether the break continued or reversed the prior structure. Two
  tables would mean two query paths for the one question anybody asks
  ("what happened to this symbol's structure?").
* **`swing_points.confirmed_on` is new.** A swing is only knowable
  `right_bars` bars after it printed. Without a column for that date, every
  query filtering on `date` hands the caller information from the future —
  the single most common way an SMC backtest quietly becomes fiction.

## 5. Fair value gaps [P5]

```
fvg_zones
  id, stock_id, timeframe, direction(BULLISH|BEARISH),
  created_at_bar DATE, upper_bound, lower_bound, size_pct,
  status (OPEN|PARTIALLY_FILLED|MITIGATED|INVALIDATED),
  mitigation_pct, mitigated_at, engine_version
  INDEX (stock_id, timeframe, status)
```

## 6. Patterns & lifecycle [P2]

```
   stocks ──1───∞── patterns ──1───∞── pattern_events
                       │
                       │ 1
                       ├───────∞── vcp_contractions
                       │
                       └───────1── breakouts ──1───1── breakout_outcomes
```

```
patterns
  id, stock_id FK, pattern_type (VCP|BASE|...),
  detected_on DATE, base_start DATE, base_end DATE,
  base_depth_pct, base_duration_days,
  pivot_price, pivot_date,
  status (FORMING|NEAR_PIVOT|BREAKOUT|CONFIRMED|EXTENDED|FAILED|COMPLETED),
  score, score_breakdown JSONB,
  engine_version, rule_version, scan_id FK
  INDEX (stock_id, detected_on), INDEX (status, score DESC)

pattern_events          append-only lifecycle log — never updated in place
  id, pattern_id FK, event_date, from_status, to_status, reason, payload JSONB

vcp_contractions
  id, pattern_id FK, sequence (1..n), start_date, end_date,
  high, low, depth_pct, duration_days, avg_volume, atr_avg

breakouts
  id, pattern_id FK, stock_id FK, breakout_date, breakout_price,
  pivot_price, volume, rel_volume, rs_at_breakout, sector_rank_at_breakout,
  regime_at_breakout, setup_score, status, engine_version

breakout_outcomes       one row per breakout, updated as it plays out
  breakout_id PK FK, mfe_pct, mae_pct, days_held, exit_date, exit_price,
  return_pct, failed BOOLEAN, failure_date, failure_reason, failure_price,
  days_to_failure, drawdown_pct
```

`pattern_events` is append-only on purpose. "How did this become CONFIRMED?"
must be answerable from rows, not inferred from a mutated status column.

## 7. Context: RS, sector, market [P3]

```
relative_strength
  stock_id, date, ret_1m/3m/6m/12m, rs_raw, rs_score(0-100), rs_rank,
  rs_vs_sector, rs_trend (IMPROVING|STABLE|DETERIORATING),
  benchmark_index_id FK, engine_version
  PRIMARY KEY (stock_id, date)

sector_metrics
  sector_id, date, ret_1m/3m/6m, rs_score, rs_rank, momentum_score,
  pct_above_ema_20/50/200, new_highs, new_lows, breakouts, failed_breakouts,
  avg_pattern_score, quadrant (LEADING|IMPROVING|WEAKENING|LAGGING)
  PRIMARY KEY (sector_id, date)

market_metrics
  date PK, advancers, decliners, ad_ratio,
  pct_above_ema_20/50/100/200, new_highs_52w, new_lows_52w,
  breakouts, failed_breakouts, breakout_success_ratio,
  index_close, index_ret_1m/3m, volatility_20d, universe_size

market_regimes
  date PK, regime (STRONG_BULL|BULL|NEUTRAL|BEAR|STRONG_BEAR),
  regime_score(0-100), trend_score, breadth_score, momentum_score,
  participation_score, volatility_score, confidence, engine_version
```

## 8. User-facing [P10]

```
users ──1──∞── watchlists ──1──∞── watchlist_items ──∞──1── stocks
  │
  ├──1──∞── saved_screens ──1──∞── screen_conditions
  ├──1──∞── strategies ──1──∞── strategy_versions
  ├──1──∞── alerts
  └──1──∞── backtest_runs
```

All user-owned tables carry `user_id` and are filtered by it at the repository
layer — multi-tenancy is not bolted on later.

## 9. Backtesting [P8]

```
backtest_runs
  id, user_id FK, strategy_version_id FK, universe_snapshot_id FK,
  start_date, end_date, initial_capital, config JSONB,
  engine_version, rule_version,
  survivorship_mode (POINT_IN_TIME|CURRENT_UNIVERSE),
  status, started_at, finished_at

backtest_trades
  id, run_id FK, stock_id FK, entry_date, entry_price, exit_date, exit_price,
  quantity, gross_pnl, net_pnl, return_pct, mfe_pct, mae_pct,
  pattern_id FK NULL, pattern_score, rs_at_entry, sector_at_entry,
  regime_at_entry, entry_reason, exit_reason

backtest_metrics
  run_id PK FK, total_trades, win_rate, avg_win, avg_loss, profit_factor,
  expectancy, cagr, max_drawdown, sharpe, sortino, calmar,
  avg_holding_days, largest_win, largest_loss, equity_curve JSONB
```

`universe_snapshot_id` is not optional. A backtest that cannot name the
universe it ran against is not reproducible.

## 10. Operations [P1 partial]

```
scan_runs               [P1]
  id, scan_date, started_at, finished_at, status,
  symbols_processed, symbols_failed, patterns_detected, breakouts_detected,
  engine_version, universe_snapshot_id FK, notes

data_quality_events     [P1]
  id, stock_id FK NULL, date NULL, severity (INFO|WARN|ERROR),
  category (MISSING_BAR|DUPLICATE|INVALID_OHLC|ZERO_VOLUME|
            SUSPECTED_SPLIT|STALE|PROVIDER_ERROR),
  detail JSONB, provider, detected_at, resolved_at NULL
  INDEX (severity, detected_at), INDEX (stock_id, date)

ai_explanations         [P9]
  id, subject_type (PATTERN|BREAKOUT|FAILURE|REGIME), subject_id,
  prompt_hash, model, input_payload JSONB, output_text,
  generated_at, engine_version_of_inputs
```

`ai_explanations.input_payload` stores exactly what the model was shown. An
explanation that cannot be audited against its inputs is not evidence of
anything.

## 11. Cardinality summary

```
sector    1 ──── ∞ industry 1 ──── ∞ stock
stock     1 ──── ∞ ohlcv_daily
stock     1 ──── ∞ technical_features
stock     1 ──── ∞ patterns      1 ──── ∞ pattern_events
patterns  1 ──── ∞ vcp_contractions
patterns  1 ──── 0..1 breakouts  1 ──── 1 breakout_outcomes
index     1 ──── ∞ universe_members ∞ ──── 1 stock
index     1 ──── ∞ universe_snapshots
run       1 ──── ∞ backtest_trades
```

## 12. Indexing intent

Read patterns drive the indexes:

| Query the UI actually makes | Index |
|---|---|
| top N setups today by score | `patterns (status, score DESC)` |
| one stock's history | `patterns (stock_id, detected_on)` |
| breadth for a date | `technical_features (date)` |
| relative-volume scan | `technical_features (date, rel_volume)` |
| open FVGs for a symbol | `fvg_zones (stock_id, timeframe, status)` |
| membership as of a date | `universe_members (index_id, effective_from, effective_to)` |
| recent data problems | `data_quality_events (severity, detected_at)` |
