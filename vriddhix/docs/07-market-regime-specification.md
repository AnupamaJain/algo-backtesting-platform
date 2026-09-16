# Tathya — Market Regime & Breadth Specification

`REGIME_ENGINE_V1.0` · Phase 3 · `src/vriddhix/engines/regime.py`

Regime describes what the market **is doing now**, measured from breadth and
trend. It is not a forecast, and the UI must never render it as one.

## 1. Breadth primitives

Computed daily across the resolved universe (point-in-time membership — a
breadth number computed over today's Nifty 500 for a 2019 date is wrong and
`UniverseService` flags it).

```
advancers          count(close > prev_close)
decliners          count(close < prev_close)
unchanged          count(close == prev_close)
ad_ratio           advancers / max(decliners, 1)
ad_line            cumulative (advancers - decliners)

pct_above_ema_20   100 × count(close > ema_20)  / universe_size
pct_above_ema_50   100 × count(close > ema_50)  / universe_size
pct_above_ema_100  100 × count(close > ema_100) / universe_size
pct_above_ema_200  100 × count(close > ema_200) / universe_size

new_highs_52w      count(high == rolling_max(high, 252))
new_lows_52w       count(low  == rolling_min(low, 252))
net_new_highs      new_highs_52w - new_lows_52w

breakouts          count(patterns transitioning to BREAKOUT today)
failed_breakouts   count(breakouts failing today)
breakout_success   breakouts_confirmed_20d / max(breakouts_attempted_20d, 1)
```

A symbol missing a bar for the date is excluded from **both** numerator and
denominator, and `universe_size` reflects the exclusion. Treating a missing
bar as "not above its EMA" manufactures bearishness on data-outage days.

## 2. The five regime components

Each scores `0–100`.

### 2.1 Trend (weight 0.30)

Benchmark index (`NIFTY500` by default, from config):

```
above_200 = 30 if close > ema_200 else 0
above_50  = 25 if close > ema_50  else 0
stack     = 25 if ema_20 > ema_50 > ema_200 else
            12 if ema_50 > ema_200 else 0
slope     = 20 × clamp(pct_change(ema_50, 20) / cfg.ideal_ema50_slope(=0.03), 0, 1)
```

### 2.2 Breadth (weight 0.25)

```
breadth = 0.30 × pct_above_ema_20
        + 0.35 × pct_above_ema_50
        + 0.35 × pct_above_ema_200
```

Already `0–100` since each term is a percentage. Weighted toward the slower
averages: `pct_above_ema_20` whipsaws on a three-day bounce.

### 2.3 Momentum (weight 0.20)

```
nh_nl     = 40 × clamp((net_new_highs / universe_size + 0.02) / 0.06, 0, 1)
ad_trend  = 30 × clamp(pct_change(ad_line_ema_10, 10) normalised, 0, 1)
idx_mom   = 30 × clamp((benchmark_ret_3m + 0.05) / 0.20, 0, 1)
```

The offsets centre "flat" near the middle of the range rather than at zero —
a market with equal new highs and lows is neutral, not maximally weak.

### 2.4 Participation (weight 0.15)

Is strength broad or concentrated in a few names?

```
sectors_positive  = count(sectors with rs_score > 50) / total_sectors
top_decile_share  = share of universe 1m return contributed by top 10% of stocks
concentration     = 1 - clamp((top_decile_share - 0.3) / 0.5, 0, 1)

participation = 60 × sectors_positive + 40 × concentration
```

A tape carried by five names scores poorly here even when the index is at
highs. That divergence is the entire point of measuring it separately.

### 2.5 Volatility (weight 0.10)

Higher score = calmer, more constructive.

```
realised_vol_20d = stdev(benchmark daily returns, 20) × sqrt(252)
vol_score = 100 × clamp(1 - (realised_vol_20d - cfg.low_vol(=0.10))
                            / (cfg.high_vol(=0.30) - cfg.low_vol), 0, 1)
```

## 3. Composite

```
regime_score = 0.30×trend + 0.25×breadth + 0.20×momentum
             + 0.15×participation + 0.10×volatility
```

Classification (thresholds configurable):

| Score | Regime |
|---|---|
| ≥ 75 | `STRONG_BULL` |
| 60–74 | `BULL` |
| 40–59 | `NEUTRAL` |
| 25–39 | `BEAR` |
| < 25 | `STRONG_BEAR` |

### Hysteresis

Raw thresholds flip state on noise. A regime change requires:

```
new_regime != current_regime
AND score is beyond the boundary by ≥ cfg.hysteresis_band (default 3)
AND that has held for ≥ cfg.hysteresis_days (default 2) consecutive sessions
```

Otherwise the previous regime persists and `pending_regime` is recorded. This
matters for backtests: a regime filter that flickers daily produces entry
churn that has nothing to do with the market.

### Confidence

```
confidence = 100 - (spread of the five component scores, as stdev, clamped)
```

Five components agreeing at ~80 is a confident bull. Components at 95/30/80/20/60
averaging to the same number is not, and the UI should say so.

## 4. Output

```python
@frozen
class RegimeResult:
    date: date
    regime: MarketRegime
    regime_score: float
    components: tuple[ScoreComponent, ...]   # the five, with weights
    confidence: float
    pending_regime: MarketRegime | None
    universe_size: int
    engine_version: str
```

Rendered as:

```
MARKET REGIME       BULL        Confidence 78

Trend          88  ×0.30
Breadth        79  ×0.25
Momentum       84  ×0.20
Participation  76  ×0.15
Volatility     73  ×0.10
                              Score 81
Breadth detail
  > 20 EMA  74%   > 50 EMA  66%   > 200 EMA  59%
```

Every number on that panel traces to §1–§3. None is decorative.

## 5. Historical series

`market_regimes` and `market_metrics` are written one row per trading day and
never back-filled from later knowledge. The regime for 2024-03-14 is whatever
the engine computed from data up to and including 2024-03-14 — even if, with
hindsight, that day was an obvious top. Backtests read this table directly, so
a hindsight-corrected regime would leak the future into every strategy that
filters on it.

## 6. Configuration

```yaml
regime:
  benchmark_index: NIFTY500
  weights: {trend: 0.30, breadth: 0.25, momentum: 0.20,
            participation: 0.15, volatility: 0.10}
  thresholds: {strong_bull: 75, bull: 60, neutral: 40, bear: 25}
  hysteresis_band: 3
  hysteresis_days: 2
  ideal_ema50_slope: 0.03
  low_vol: 0.10
  high_vol: 0.30
  min_universe_coverage_pct: 80    # below this, regime is NULL, not guessed
```

`min_universe_coverage_pct` is a refusal condition: if a data outage means
only 60% of the universe has bars, the engine returns `None` and the
dashboard shows "regime unavailable — insufficient coverage" rather than a
number computed from a biased sample.

## 7. Test obligations

| Test | Assertion |
|---|---|
| `test_regime_golden` | Synthetic bull/bear/chop universes land in expected buckets |
| `test_regime_hysteresis` | A one-day score dip across a boundary does not flip regime |
| `test_regime_no_lookahead` | Regime for `T` unchanged by appending data after `T` |
| `test_breadth_excludes_missing` | Missing bars leave the denominator, not counted as weak |
| `test_regime_coverage_guard` | Coverage below threshold returns `None` |
| `test_regime_weights_sum` | Weights ≠ 1.0 raises at load |
