# Pramana — VCP Engine Specification

`VCP_ENGINE_V1.0` · implemented in Phase 2 · `src/vriddhix/engines/vcp/`

A Volatility Contraction Pattern is a sequence of progressively tighter
pullbacks following an established uptrend, ending at a definable pivot. This
document defines it precisely enough that two engineers implementing from it
independently would produce the same detections.

## 0. What this engine is not allowed to do

- **Not** "ATR is falling, therefore VCP." Falling ATR is one input among
  several and is neither necessary nor sufficient.
- **Not** perfectionist. Real bases are untidy. A contraction sequence of
  `18% → 11% → 12% → 6%` has one out-of-order step and is still a VCP. The
  engine measures quality on a continuum; it does not reject on one flaw.
- **Not** forward-looking. Every quantity at bar `t` uses bars `≤ t`. The
  pivot is identifiable in real time, not after the breakout reveals it.

## 1. Interface

```python
def detect(df: pd.DataFrame, cfg: VcpConfig, as_of: date | None = None)
        -> VcpResult | None
```

Pure. No I/O. `as_of` defaults to the last bar; if given, the frame is
truncated to `≤ as_of` before anything else happens.

```python
@frozen
class VcpResult:
    base: VcpBase
    score: CompositeScore
    stage: PatternStatus          # FORMING | NEAR_PIVOT | BREAKOUT
    prior_trend: PriorTrend
    diagnostics: dict             # every intermediate, for the X-Ray panel
    engine_version: str
```

Returning `None` means "no base meeting minimum structural requirements",
which is different from "a poor base". Poor bases return a result with a low
score. `diagnostics` always explains which gate rejected a candidate.

## 2. Stage 1 — prior trend

A base is only meaningful as a *pause* in something. Require a prior advance.

Measured over `cfg.prior_trend_lookback_days` (default 250) ending at
`base_start`:

```
prior_trend_pct = (base_start_close / min_close_in_lookback - 1) × 100
trend_duration  = bars from that minimum to base_start
```

Gates (all configurable, all must pass):

| Gate | Default |
|---|---|
| `prior_trend_pct ≥ min_prior_trend_pct` | 30 |
| `trend_duration ≥ min_prior_trend_days` | 40 |
| `close > ema_200` at `base_start` | true |
| `ema_50 > ema_200` at `base_start` | true |

Scored `0–100` (contributes to `Prior Trend`, weight 15%):

```
trend_magnitude  = clamp(prior_trend_pct / cfg.ideal_prior_trend_pct, 0, 1) × 40
ema_alignment    = 25 if close>ema20>ema50>ema200 else
                   15 if close>ema50>ema200 else
                    8 if close>ema200 else 0
trend_persistence= (fraction of lookback bars with close > ema_50) × 20
rs_bonus         = clamp((rs_score - 50) / 50, 0, 1) × 15
```

`rs_score` is supplied by the caller (RS engine output); when absent, that
term scores 0 and `diagnostics["rs_available"] = False`.

## 3. Stage 2 — base boundaries

The base is the consolidation containing the contractions.

**Base end** = `as_of` (bases are evaluated as they stand today).

**Base start** — scan backwards from `as_of` for the most recent bar that is
both:
- a swing high of strength `≥ cfg.base_pivot_strength` (default 5 bars each
  side, right side may be truncated at `as_of`), and
- the highest close in the `cfg.base_high_lookback` (default 30) bars before it.

That bar is the **base high**. `base_start` = that bar's date.

Then:

```
base_high      = max(high) over [base_start, base_end]
base_low       = min(low)  over [base_start, base_end]
depth_pct      = (base_high - base_low) / base_high × 100
duration_days  = trading bars in [base_start, base_end]
```

Gates:

| Gate | Default |
|---|---|
| `duration_days ≥ min_base_days` | 30 |
| `duration_days ≤ max_base_days` | 250 |
| `depth_pct ≤ max_base_depth_pct` | 35 |
| `depth_pct ≥ min_base_depth_pct` | 5 |

The lower depth bound matters: a 2%-deep "base" is drift, not a pattern with
supply being absorbed.

Base quality score (weight 15%):

```
depth_quality    = 40 × triangular(depth_pct, ideal=cfg.ideal_base_depth_pct(=18),
                                   lo=min_base_depth_pct, hi=max_base_depth_pct)
duration_quality = 25 × triangular(duration_days, ideal=55, lo=30, hi=250)
tightness        = 20 × (1 - clamp(stdev(close)/mean(close) / cfg.max_base_cv, 0, 1))
position         = 15 × clamp(1 - (base_high - last_close)/(base_high - base_low), 0, 1)
```

`triangular(x, ideal, lo, hi)` returns 1.0 at `ideal`, tapering linearly to 0
at `lo` and `hi`. It encodes "there is a sweet spot", which a monotonic
function cannot.

## 4. Stage 3 — contraction detection

Within `[base_start, base_end]`, decompose price into alternating swing legs.

1. Find swing highs and lows using strength `cfg.contraction_swing_strength`
   (default 3 bars each side).
2. Walk the alternating sequence. A **contraction** is a
   `swing_high → subsequent swing_low` pair.
3. For each contraction `i`:

```
high_i        = swing high price
low_i         = subsequent swing low price
depth_pct_i   = (high_i - low_i) / high_i × 100
duration_i    = bars between them
avg_volume_i  = mean(volume) over the leg
atr_avg_i     = mean(atr_14) over the leg
```

4. Discard any contraction with `depth_pct_i < cfg.min_contraction_depth_pct`
   (default 2) — noise, not a pullback.
5. Keep the last `cfg.max_contractions` (default 6), chronological.

Gate: `len(contractions) ≥ cfg.min_contractions` (default 2). Fewer than two
means there is no *sequence* to contract.

### Contraction quality (weight 20%)

The ideal is monotonic tightening `T1 > T2 > T3 > T4`, with tolerance.

```
n = len(contractions)
pairs = [(d_i, d_{i+1}) for consecutive depths]

# 1. monotonicity, tolerance-aware  (50 pts)
tightening_ratio = count(d_{i+1} <= d_i × (1 + cfg.contraction_tolerance(=0.15)))
                   / len(pairs)
monotonic_score  = 50 × tightening_ratio

# 2. total compression: how much tighter is the last vs the first  (30 pts)
compression      = clamp(1 - d_last / d_first, 0, 1)
compression_score= 30 × clamp(compression / cfg.ideal_compression(=0.6), 0, 1)

# 3. final tightness in absolute terms  (20 pts)
final_score      = 20 × clamp(1 - d_last / cfg.max_final_contraction_pct(=8), 0, 1)
```

A sequence `18 → 11 → 12 → 6` scores: 2 of 3 pairs tighten within tolerance
(`12 ≤ 11×1.15 = 12.65` → also passes) ⇒ 3/3 ⇒ 50; compression
`1 - 6/18 = 0.67` ⇒ 30; final `1 - 6/8 = 0.25` ⇒ 5. Total 85 — correctly
treated as a good VCP despite the middle step being nominally wider.

## 5. Stage 4 — volume dry-up (weight 15%)

Supply exhausting shows as declining volume into the tightest contraction.

```
early_volume = mean(avg_volume_i for first half of contractions)
late_volume  = mean(avg_volume_i for last  half of contractions)
dryup_ratio  = late_volume / early_volume            # < 1 is contraction

base_avg_vol = mean(volume over base)
prior_avg_vol= mean(volume over the 50 bars before base_start)
base_ratio   = base_avg_vol / prior_avg_vol
```

```
dryup_score  = 60 × clamp((1 - dryup_ratio) / (1 - cfg.ideal_dryup_ratio(=0.6)), 0, 1)
base_score   = 40 × clamp((1 - base_ratio)  / (1 - cfg.ideal_base_vol_ratio(=0.75)), 0, 1)
```

Volume *expansion* into the pivot is a negative, not a neutral: both terms go
to 0 and `diagnostics["volume_expanding"] = True` so the UI can flag it.

## 6. Stage 5 — pivot identification

The pivot is the price above which the base is resolved upward. It must be
identifiable **before** any breakout.

Candidates, in priority order:

1. **Last contraction's high** — the most recent supply point, if it is within
   `cfg.pivot_max_distance_from_base_high_pct` (default 8) of `base_high`.
2. **Base high** — otherwise.

Then apply a tick-aware buffer:

```
pivot_price = round_to_tick(candidate × (1 + cfg.pivot_buffer_pct(=0.1)/100))
pivot_date  = date of the candidate bar
pivot_type  = "LAST_CONTRACTION_HIGH" | "BASE_HIGH"
```

The buffer prevents a one-tick poke through the exact high registering as a
breakout.

```
distance_from_pivot_pct = (pivot_price - last_close) / last_close × 100
```

Negative distance means price is already above the pivot.

### Pivot readiness (weight 10%)

```
readiness = 100 × clamp(1 - max(distance_from_pivot_pct, 0)
                            / cfg.max_pivot_distance_pct(=10), 0, 1)
```

At or above the pivot ⇒ 100. Ten percent away ⇒ 0. This is the term that
makes "ready now" outrank "structurally lovely but 15% below the pivot".

## 7. Stage 6 — stage classification

```
if last_close > pivot_price and volume_confirms:   BREAKOUT
elif distance_from_pivot_pct <= cfg.near_pivot_pct(=3):  NEAR_PIVOT
else:                                              FORMING
```

where

```
volume_confirms = volume_today >= avg_volume_50 × cfg.breakout_volume_multiplier
```

`breakout_volume_multiplier` default 1.5, configurable to 1.0 / 1.2 / 2.0. A
close above the pivot **without** volume is recorded as `BREAKOUT` with
`diagnostics["volume_confirmed"] = False`; the lifecycle service then decides
`CONFIRMED` vs `FAILED` per `06`/`services/lifecycle.py`. Detection and
confirmation are deliberately separate steps.

## 8. Composite VCP score

```
VCP Score = 0.15 × prior_trend
          + 0.15 × base_quality
          + 0.20 × contraction_quality
          + 0.15 × volume_dryup
          + 0.15 × relative_strength      ← from RS engine
          + 0.10 × sector_strength        ← from sector engine
          + 0.10 × pivot_readiness
```

Weights live in `config/vriddhix.yaml → scoring.vcp`. They must sum to 1.0;
`VcpConfig.__post_init__` raises if they do not. RS and sector terms score 0
and are marked unavailable when their engines have not run, rather than being
silently redistributed — a score computed without market context should not
be able to impersonate one that had it.

Every component is returned in `CompositeScore.components` and rendered:

```
VCP SCORE: 91
  Prior Trend        94   × 0.15 = 14.1
  Base Quality       88   × 0.15 = 13.2
  Contractions       96   × 0.20 = 19.2
  Volume Dry-Up      89   × 0.15 = 13.4
  Relative Strength  97   × 0.15 = 14.6
  Sector Strength    82   × 0.10 =  8.2
  Pivot Readiness    90   × 0.10 =  9.0
```

## 9. Breakout record

On transition to `BREAKOUT`, `services/lifecycle.py` writes a `breakouts` row
capturing the *state at that moment* — pivot, price, volume, relative volume,
RS, sector rank, regime, setup score. These are snapshotted, not joined later:
the question "what did this look like when it broke out?" must survive later
recomputation of RS and regime.

## 10. Failure conditions

Configurable; defaults:

| Condition | Default |
|---|---|
| Close below pivot after breakout | `close < pivot × (1 - failure_buffer_pct/100)`, 2% |
| Close below base low at any time | immediate `FAILED` |
| Breakout not confirmed within N days | 3 days |
| Depth breach while `FORMING` | `depth_pct > max_base_depth_pct` |

On failure: write `pattern_events` transition, populate `breakout_outcomes`
with `failure_reason`, `failure_price`, `days_to_failure`, `drawdown_pct`.
Never delete the pattern.

## 11. Configuration surface

```yaml
vcp:
  prior_trend_lookback_days: 250
  min_prior_trend_pct: 30
  min_prior_trend_days: 40
  ideal_prior_trend_pct: 100

  min_base_days: 30
  max_base_days: 250
  min_base_depth_pct: 5
  max_base_depth_pct: 35
  ideal_base_depth_pct: 18
  base_pivot_strength: 5
  base_high_lookback: 30
  max_base_cv: 0.12

  contraction_swing_strength: 3
  min_contractions: 2
  max_contractions: 6
  min_contraction_depth_pct: 2
  contraction_tolerance: 0.15
  ideal_compression: 0.6
  max_final_contraction_pct: 8

  ideal_dryup_ratio: 0.6
  ideal_base_vol_ratio: 0.75

  pivot_buffer_pct: 0.1
  pivot_max_distance_from_base_high_pct: 8
  max_pivot_distance_pct: 10
  near_pivot_pct: 3

  breakout_volume_multiplier: 1.5
  failure_buffer_pct: 2
  confirm_within_days: 3
```

## 12. Test obligations (Phase 2 gate)

| Test | Assertion |
|---|---|
| `test_vcp_golden` | Fixed CSVs of known bases produce recorded scores, ±0.01 |
| `test_vcp_no_lookahead` | `detect(df, as_of=T)` identical when bars after `T` are appended |
| `test_vcp_pivot_stability` | Pivot does not change retroactively as new bars arrive while `FORMING` |
| `test_vcp_tolerance` | `18→11→12→6` scores > 80; `6→11→18` scores < 30 |
| `test_vcp_rejects_no_trend` | Base after a downtrend returns `None` |
| `test_vcp_rejects_too_deep` | 45%-deep base returns `None` |
| `test_vcp_weights_sum` | Config whose weights ≠ 1.0 raises at load |
| `test_vcp_missing_rs` | Absent RS ⇒ component 0 and flagged, never redistributed |
