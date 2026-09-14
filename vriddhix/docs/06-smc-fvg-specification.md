# VriddhiX — SMC & FVG Engine Specification

`SMC_ENGINE_V1.0` (Phase 4) · `FVG_ENGINE_V1.0` (Phase 5)

Smart Money Concepts is normally taught as a set of visual judgements. That is
useless for a system that must backtest. Every structure here has an
arithmetic definition producing the same answer every time it is run.

## 1. Swing detection — the primitive everything else rests on

```python
def find_swings(df, left=3, right=3, min_move_pct=0.0) -> list[SwingPoint]
```

Bar `i` is a **swing high** when:

```
high[i] > high[j]  for all j in [i-left, i-1]      (strictly greater)
high[i] >= high[j] for all j in [i+1, i+right]     (greater or equal)
```

Swing low is the mirror with `low`, reversed comparisons.

Asymmetric strictness (`>` left, `>=` right) is deliberate: on a plateau of
equal highs it marks the **first** one, so the swing does not drift forward as
bars arrive.

**Confirmation lag.** A swing at bar `i` is only *known* at bar `i + right`.
Every consumer must use the confirmed set:

```python
def confirmed_swings(df, as_of, left, right):
    return [s for s in find_swings(df.loc[:as_of], left, right)
            if bars_between(s.date, as_of) >= right]
```

Skipping this is the most common way SMC backtests become fiction — the swing
is drawn using bars that had not printed yet.

Optional `min_move_pct` filters swings whose move from the prior opposite
swing is below a threshold, suppressing noise in low-volatility names.

`strength` = number of bars on the *weaker* side that confirm it, capped at
`max(left, right)`.

## 2. Market structure state

Track the sequence of confirmed swings as:

```
last_confirmed_high  (price, date)
last_confirmed_low   (price, date)
structure            BULLISH | BEARISH | RANGING
```

Initial structure is `RANGING` until the first BOS.

## 3. BOS — Break of Structure

A continuation break: price takes out the last swing in the direction it is
already trending.

**Bullish BOS** at bar `t`:
```
close[t] > last_confirmed_high.price
AND structure in (BULLISH, RANGING)
AND last_confirmed_high.date < t
```

**Bearish BOS** at bar `t`:
```
close[t] < last_confirmed_low.price
AND structure in (BEARISH, RANGING)
AND last_confirmed_low.date < t
```

Close-based, not wick-based. Configurable via `smc.break_on: close | high_low`,
default `close` — wick breaks generate roughly 3× the events and most are
noise.

Recorded: `date, direction, broken_swing_id, broken_level, close_price,
volume, rel_volume`. After a BOS, structure is set to the BOS direction and
the broken swing is retired from the reference set.

## 4. CHoCH — Change of Character

The *first* break against the prevailing structure. The distinction from BOS
is entirely about the prior structure, which is why structure must be tracked
as state rather than recomputed per bar.

```
Bearish structure + close > last_confirmed_high   ⇒ BULLISH CHoCH
Bullish structure + close < last_confirmed_low    ⇒ BEARISH CHoCH
```

After a CHoCH, structure flips; the next break in the new direction is a BOS,
not another CHoCH.

Every CHoCH records `reference_swing_ids` — the specific swings that
constituted the prior structure — so the UI can draw exactly what changed
rather than asserting that something did.

```
   Bearish structure          CHoCH               New bullish structure
        ╲                       │                        ╱
   HH ───╲        LH            │                   HL ─╱─── HH
          ╲      ╱  ╲           │                 ╱
           ╲    ╱    ╲   close above LH      ────╱
            LL ╱      ╲ ────────┘
```

## 5. Order blocks

The last opposing candle before the impulse that caused a BOS.

**Bullish OB**: given a bullish BOS at `t`, scan back up to
`smc.ob_lookback_bars` (default 10) for the most recent bar `k < t` with
`close[k] < open[k]` (down candle) such that price advanced from `k` to `t`
without closing below `low[k]`.

```
upper = high[k]
lower = low[k]        # or body-only if smc.ob_use_body = true
```

Bearish OB is the mirror.

Status: `OPEN` until price trades back into `[lower, upper]`, then
`MITIGATED`. `INVALIDATED` if price closes beyond the far side.

## 6. Liquidity

**Equal highs / lows.** Two or more swings of the same type within
`smc.equal_level_tolerance_pct` (default 0.1%) of each other. Recorded as
`EQH`/`EQL` with the participating swing ids and the level (their mean).

**Liquidity sweep.** A wick through a swing (or EQH/EQL cluster) that the
close does not hold:

```
Bullish sweep (below a low):
    low[t]   < level
AND close[t] > level
AND close[t] > close[t-1]
```

Sweeps are recorded with `reclaimed = True/False` and, when the sweep is
followed by a BOS within `smc.sweep_bos_window` bars (default 5), linked to
that BOS — that pairing is the structure the confluence engine actually cares
about.

## 7. FVG — Fair Value Gap

A three-candle imbalance where the middle candle's move left an unfilled range.

**Bullish FVG** at bars `(i-2, i-1, i)`:
```
low[i] > high[i-2]
upper_bound = low[i]
lower_bound = high[i-2]
```

**Bearish FVG**:
```
high[i] < low[i-2]
upper_bound = low[i-2]
lower_bound = high[i]
```

```
size_pct = (upper_bound - lower_bound) / lower_bound × 100
```

Discard if `size_pct < fvg.min_gap_pct` (default 0.1). Gaps below that are
tick noise, and on an illiquid NSE small-cap they occur constantly.

### Mitigation

Tracked forward every bar after creation:

```
overlap        = max(0, min(bar_high, upper) - max(bar_low, lower))
mitigation_pct = cumulative_max(overlap / (upper - lower)) × 100
```

| Status | Condition |
|---|---|
| `OPEN` | `mitigation_pct < 10` |
| `PARTIALLY_FILLED` | `10 ≤ mitigation_pct < fvg.mitigation_threshold_pct` (default 50) |
| `MITIGATED` | `mitigation_pct ≥ mitigation_threshold_pct` |
| `INVALIDATED` | close beyond the far side by `fvg.invalidation_buffer_pct` (0.5) |

`mitigation_pct` is a running maximum: a gap tagged 60% and left does not
revert to 0%. The deepest touch is the fact worth keeping.

Timeframes: `D1` always; `H4, H1, M30, M15` when intraday data is present.
Each timeframe's zones are stored separately and never merged.

## 8. Confluence scoring

`CONFLUENCE_V1.0`. Components are binary or graded, weighted from config:

| Component | Condition | Default weight |
|---|---|---|
| `bos_aligned` | BOS in the setup direction within `lookback_bars` (20) | 0.20 |
| `choch_recent` | CHoCH in direction within 40 bars | 0.10 |
| `fvg_present` | Unmitigated FVG in direction, price at/near it | 0.15 |
| `order_block` | Price inside or within 1% of an OPEN OB | 0.15 |
| `liquidity_sweep` | Sweep in direction within 10 bars | 0.10 |
| `vwap_alignment` | Price on the correct side of anchored VWAP | 0.05 |
| `ema_alignment` | `close > ema20 > ema50` (bullish) | 0.05 |
| `volume` | `rel_volume ≥ 1.2` | 0.05 |
| `rs_supportive` | `rs_score ≥ 70` | 0.10 |
| `regime_supportive` | Regime aligned with direction | 0.05 |

```
confluence_score = 100 × Σ (component_met × weight)
```

Graded components (`fvg_present`, `order_block`) return a proximity fraction
rather than 0/1, so "price sitting in the gap" outranks "a gap exists 6% away".

**This score is a count of conditions met, weighted. It is not a probability.**
The UI must render it as `Confluence: 87 (8 of 10 conditions)` and never as a
likelihood of the trade working.

## 9. Multi-timeframe

Higher timeframes supply context; lower timeframes supply structure. The
default chain is configurable:

```yaml
mtf:
  context: D1        # trend / regime
  structure: H4      # BOS / CHoCH
  refinement: H1     # order blocks
  entry: M15         # FVG / trigger
  require_context_alignment: true
```

With `require_context_alignment: true`, a lower-timeframe setup opposing the
`context` timeframe's structure is recorded but scored with the
`regime_supportive` and `bos_aligned` terms zeroed, and tagged
`counter_trend: true`. It is not hidden — counter-trend setups are exactly
the population you want to study separately.

## 10. Test obligations

| Test | Assertion |
|---|---|
| `test_swing_confirmation_lag` | A swing is not reported before `right` bars have printed |
| `test_swing_plateau` | Equal highs mark the first bar, stable as bars append |
| `test_bos_vs_choch` | Same break classified differently under different prior structure |
| `test_fvg_arithmetic` | Hand-computed 3-bar gaps match to the tick |
| `test_fvg_mitigation_monotonic` | `mitigation_pct` never decreases |
| `test_smc_no_lookahead` | Events as of `T` unchanged by later bars |
| `test_confluence_weights_sum` | Weights ≠ 1.0 raises at config load |
