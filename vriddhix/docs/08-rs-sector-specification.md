# VriddhiX — Relative Strength & Sector Rotation Specification

`RS_ENGINE_V1.0`, `SECTOR_ENGINE_V1.0` · Phase 3

## Part A — Relative Strength

### A.1 What is being measured

How a stock's return compares to a benchmark and to its peers, over multiple
horizons, weighted toward the recent. This is a **descriptive ranking of what
has already happened**. It is not a forecast, and the word "strength" in the
UI must never be styled to imply one.

### A.2 Raw RS

Per stock, on each date, using adjusted closes:

```
ret_1m  = close / close[-21]  - 1
ret_3m  = close / close[-63]  - 1
ret_6m  = close / close[-126] - 1
ret_12m = close / close[-252] - 1
```

Bars, not calendar days — NSE holidays make calendar offsets uneven.

If a stock lacks history for a horizon (recent listing), that horizon is
`None` and the weights of the **available** horizons are renormalised to sum
to 1. `rs_coverage` records which were available, so a 2-month-old listing
ranking highly on `ret_1m` alone is identifiable as such rather than silently
comparable to a stock with four horizons.

```
rs_raw = Σ (w_h × ret_h) / Σ w_h        over available horizons h
```

Default weights (config `rs.horizon_weights`):

| Horizon | Weight |
|---|---|
| 1m | 0.40 |
| 3m | 0.30 |
| 6m | 0.20 |
| 12m | 0.10 |

Front-weighted deliberately: a 12-month return is dominated by what happened
last year, which is not what "currently strong" should mean.

### A.3 RS Score and Rank — percentile, not z-score

```
rs_rank  = percentile rank of rs_raw within the point-in-time universe, 1..100
rs_score = rs_rank                      (identical by construction in V1.0)
```

Percentile rank is used rather than a z-score because the cross-sectional
return distribution on NSE is fat-tailed and skewed; a z-score lets three
runaway small-caps compress everything else into the middle. Percentile is
robust to that and means exactly what users expect: **"RS 95 = stronger than
95% of the universe on this date."**

Ties share the lower rank. The universe is resolved point-in-time — ranking
against today's index for a 2020 date is survivorship bias in its purest form.

### A.4 RS versus sector

```
rs_vs_sector = percentile rank of rs_raw within the stock's own sector
```

Surfaces the strongest name in a weak sector, and the laggard in a hot one —
two populations worth separating in research.

### A.5 RS trend

```
rs_slope = rs_score - rs_score[-21]        # 1-month change in percentile
```

| `rs_slope` | `rs_trend` |
|---|---|
| ≥ +5 | `IMPROVING` |
| −5 … +5 | `STABLE` |
| ≤ −5 | `DETERIORATING` |

A stock at RS 96 and deteriorating is a different object from RS 96 and
improving. The single number hides that; the trend restores it.

### A.6 RS Line

For charting: `rs_line = close / benchmark_close`, normalised to 100 at the
start of the visible window. "RS line at new high while price is not" is a
readable fact on a chart and needs no further scoring.

### A.7 Output

```python
@frozen
class RsResult:
    stock_id: int; date: date
    ret_1m: float|None; ret_3m: float|None
    ret_6m: float|None; ret_12m: float|None
    rs_raw: float; rs_score: float; rs_rank: int
    rs_vs_sector: float|None
    rs_trend: RsTrend
    rs_coverage: tuple[str, ...]
    benchmark_index: str
    engine_version: str
```

## Part B — Sector Rotation

### B.1 Sector aggregation

Sector metrics are **equal-weighted means over constituents**, not
cap-weighted. Cap weighting would make every sector a proxy for its two
largest names — the opposite of the participation question sectors are being
asked here. (Cap-weighted variants are a Phase-10 config toggle, stored under
a distinct `engine_version`.)

```
sector_ret_1m   = mean(constituent ret_1m)
sector_ret_3m   = mean(constituent ret_3m)
sector_ret_6m   = mean(constituent ret_6m)

pct_above_ema_20/50/200 = percentage of constituents above each
new_highs / new_lows    = counts of 52-week extremes
breakouts / failed      = counts from the pattern lifecycle for the date
avg_pattern_score       = mean score of patterns detected in the sector
```

Sectors with fewer than `sector.min_constituents` (default 5) are computed but
flagged `low_sample: true` and excluded from ranking — a "sector" of two
stocks produces a rank that is really a single-stock opinion.

### B.2 Sector RS and momentum

```
sector_rs_raw   = Σ (w_h × sector_ret_h)      # same horizon weights as §A.2
sector_rs_score = percentile rank across sectors, 0..100
sector_momentum = sector_rs_score - sector_rs_score[-21]   # ~1m change
```

### B.3 The rotation quadrant

Two axes: **where the sector is** (RS) and **where it is going** (momentum).

```
                    momentum ↑
                         │
        IMPROVING        │        LEADING
     (weak, gaining)     │     (strong, gaining)
                         │
    ─────────────────────┼───────────────────── RS →
                         │
        LAGGING          │       WEAKENING
     (weak, losing)      │    (strong, losing)
                         │
```

```
rs_high       = sector_rs_score >= 50
mom_positive  = sector_momentum >= 0

LEADING    = rs_high and mom_positive
WEAKENING  = rs_high and not mom_positive
IMPROVING  = not rs_high and mom_positive
LAGGING    = not rs_high and not mom_positive
```

The midpoint is 50 (the median sector) rather than an absolute return, so the
four quadrants are always populated and describe *relative* standing.

**What this does and does not claim.** The quadrant states where a sector sits
today on two measured axes. It does **not** predict rotation into the next
quadrant. The UI renders the matrix with a caption to that effect; the
tempting arrow showing the "expected" clockwise path is not drawn, because
the data does not support it.

### B.4 Sector breadth panel

```
SECTOR            RS   MOM   QUADRANT     >20EMA  >50EMA  >200EMA  NH  NL  BO  FAIL
Capital Goods     92   +11   LEADING         86%     79%      71%   12   0   4     0
Energy            78    -6   WEAKENING       61%     64%      70%    3   1   1     1
IT                34   +14   IMPROVING       48%     31%      22%    1   4   2     1
FMCG              21    -8   LAGGING         19%     14%      18%    0   9   0     2
```

Every column is defined in §B.1. None is editorial.

### B.5 Output

```python
@frozen
class SectorResult:
    sector_id: int; date: date
    ret_1m: float; ret_3m: float; ret_6m: float
    rs_score: float; rs_rank: int; momentum: float
    quadrant: SectorQuadrant
    pct_above_ema_20: float; pct_above_ema_50: float; pct_above_ema_200: float
    new_highs: int; new_lows: int
    breakouts: int; failed_breakouts: int
    avg_pattern_score: float|None
    constituent_count: int; low_sample: bool
    engine_version: str
```

## Part C — Shared configuration

```yaml
rs:
  benchmark_index: NIFTY500
  horizons: {ret_1m: 21, ret_3m: 63, ret_6m: 126, ret_12m: 252}
  horizon_weights: {ret_1m: 0.40, ret_3m: 0.30, ret_6m: 0.20, ret_12m: 0.10}
  trend_lookback_days: 21
  trend_improving_threshold: 5
  min_history_days: 21          # below this, no RS is produced at all

sector:
  min_constituents: 5
  aggregation: equal_weight     # equal_weight | cap_weight (P10)
  quadrant_rs_midpoint: 50
```

## Part D — Test obligations

| Test | Assertion |
|---|---|
| `test_rs_percentile` | Known return vector produces exact expected ranks |
| `test_rs_ties` | Tied `rs_raw` share the lower rank |
| `test_rs_partial_history` | Missing horizons renormalise weights; coverage recorded |
| `test_rs_point_in_time_universe` | Ranks computed against membership as of the date |
| `test_rs_no_lookahead` | RS at `T` unchanged when later bars are appended |
| `test_sector_low_sample` | < 5 constituents flagged and excluded from ranks |
| `test_sector_quadrants` | Four synthetic sectors land in the four quadrants |
| `test_rs_weights_sum` | Horizon weights ≠ 1.0 raises at load |
