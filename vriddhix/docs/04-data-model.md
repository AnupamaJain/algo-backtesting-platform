# Pramana — Data Model

The ERD (`03`) describes storage. This describes **meaning**: the vocabulary
every layer shares, and the invariants that hold everywhere.

## 1. Why a separate domain layer

`vriddhix.domain` contains enums and frozen dataclasses with no dependencies
beyond the standard library. Engines speak in these types, not in ORM objects
or raw dicts.

Three reasons this is worth a layer:

1. An engine that accepted an ORM row could lazily load a relationship
   mid-calculation and reach data outside its evaluation window.
2. Backtests construct domain objects from Parquet, live scans construct them
   from Postgres. If the engine took ORM rows, the backtest would need a fake
   ORM.
3. Frozen dataclasses make a result immutable once produced, so a scoring step
   cannot quietly mutate what a detection step returned.

## 2. Enumerations

```python
Exchange        NSE | BSE
Timeframe       M5 | M15 | M30 | H1 | H4 | D1
PatternType     VCP | BASE | FLAT_BASE | CUP_HANDLE | DOUBLE_BOTTOM

PatternStatus   FORMING | NEAR_PIVOT | BREAKOUT | CONFIRMED
                | EXTENDED | FAILED | COMPLETED

Direction       BULLISH | BEARISH
SwingType       HIGH | LOW
StructureEvent  BOS | CHOCH
FvgStatus       OPEN | PARTIALLY_FILLED | MITIGATED | INVALIDATED

MarketRegime    STRONG_BULL | BULL | NEUTRAL | BEAR | STRONG_BEAR
SectorQuadrant  LEADING | IMPROVING | WEAKENING | LAGGING
RsTrend         IMPROVING | STABLE | DETERIORATING
SetupGrade      A_PLUS | A | B_PLUS | B | C

DataIssue       MISSING_BAR | DUPLICATE | INVALID_OHLC | ZERO_VOLUME
                | NEGATIVE_PRICE | SUSPECTED_SPLIT | STALE | PROVIDER_ERROR
Severity        INFO | WARN | ERROR
SurvivorshipMode POINT_IN_TIME | CURRENT_UNIVERSE
```

`PatternStatus` is a state machine, not a label set — see §6.

## 3. The price frame contract

Every engine and feature function receives a `pandas.DataFrame` obeying a
single contract, validated once at the L1/L2 boundary by
`features.technical.validate_price_frame()`:

| Requirement | Rationale |
|---|---|
| `DatetimeIndex`, named `date`, tz-naive, normalised to midnight | Comparing a tz-aware index to a tz-naive date is a silent empty-result bug |
| Strictly increasing, no duplicates | A duplicated bar double-counts volume and corrupts every rolling window |
| Columns `open, high, low, close, volume` (lowercase) | One spelling everywhere; provider quirks are normalised at ingest |
| `float64` for OHLC and volume | Mixed int/float volume changes rolling-mean results at the last decimal |
| `low ≤ min(open, close)` and `high ≥ max(open, close)` | An impossible bar produces impossible ATR |
| No NaN in OHLC | NaN propagates silently through EMAs |

Violations raise `InvalidPriceFrame` rather than returning a degraded result.
A wrong number that looks plausible is worse than an exception.

## 4. Causality — the rule that makes backtests meaningful

> **A value dated `t` may depend only on bars dated `≤ t`.**

Concretely, in `features/` and `engines/`:

| Forbidden | Why |
|---|---|
| `.shift(-n)` | pulls a future bar backwards |
| `center=True` in `.rolling()` | a centred window straddles `t` |
| `.bfill()` | fills a hole with data from later |
| z-scoring against the full series | the mean embeds the future |
| `df.max()` / `df.min()` over the whole frame | same |
| `.iloc[-1]` as "current" inside a loop over history | uses the end of the frame, not `t` |

Permitted causal equivalents: `.rolling(n)` (trailing, default), `.ewm()`,
`.expanding()`, `.shift(+n)`, `.ffill(limit=k)`.

Enforcement is two-layer:

- `tests/test_causality.py` truncates a frame at `T`, computes features on
  both the truncated and full frames, and asserts every row `≤ T` matches
  exactly.
- `tests/test_no_lookahead.py` (Phase 2+) does the same for whole scans.

A causality violation is a correctness bug of the highest severity in this
codebase: it invalidates every backtest number the system has ever produced.

## 5. Core domain objects

```python
@frozen
class Bar:
    date: date; open: float; high: float; low: float
    close: float; volume: float

@frozen
class SwingPoint:
    date: date; price: float; type: SwingType
    strength: int                      # bars confirming it on the weaker side

@frozen
class Contraction:
    sequence: int                      # 1-based, chronological
    start: date; end: date
    high: float; low: float
    depth_pct: float                   # (high-low)/high * 100
    duration_days: int
    avg_volume: float
    atr_avg: float

@frozen
class VcpBase:
    start: date; end: date
    base_high: float; base_low: float
    depth_pct: float; duration_days: int
    contractions: tuple[Contraction, ...]
    pivot_price: float; pivot_date: date

@frozen
class ScoreComponent:
    name: str; raw: float              # 0..100 before weighting
    weight: float; contribution: float # raw * weight

@frozen
class CompositeScore:
    total: float                       # 0..100
    components: tuple[ScoreComponent, ...]
    rule_version: str
```

`CompositeScore` always carries its components. A score the UI cannot break
down is a number the user is asked to trust blindly, which this system does
not do.

## 6. Pattern lifecycle state machine

```
                    ┌──────────┐
                    │ FORMING  │◀──── detected: valid base, pivot > n% away
                    └────┬─────┘
                         │ price within pivot_proximity_pct of pivot
                         ▼
                   ┌────────────┐
                   │ NEAR_PIVOT │
                   └─────┬──────┘
            close > pivot │              base invalidated
                          ▼              (depth/duration breach)
                    ┌──────────┐                │
                    │ BREAKOUT │                │
                    └────┬─────┘                │
        volume confirm   │   no confirm /       │
        + holds n days   │   closes back below  │
              ┌──────────┴──────────┐           │
              ▼                     ▼           ▼
        ┌───────────┐         ┌──────────┐  ┌──────────┐
        │ CONFIRMED │         │  FAILED  │  │  FAILED  │
        └─────┬─────┘         └──────────┘  └──────────┘
              │ +X% from pivot
              ▼
        ┌───────────┐   exit rule met    ┌───────────┐
        │ EXTENDED  │───────────────────▶│ COMPLETED │
        └───────────┘                    └───────────┘
```

Rules:

- Transitions are **append-only rows** in `pattern_events`. The `status`
  column on `patterns` is a denormalised convenience, always derivable by
  replaying events.
- `FAILED` and `COMPLETED` are terminal. A symbol that sets up again gets a
  **new** pattern row.
- No transition may be skipped. `FORMING → CONFIRMED` is a bug.
- Nothing is ever deleted. A failed breakout is research data.

## 7. Scoring model

All scores are `0–100`, higher = stronger measurement. Composition:

```
total = Σ (component_raw_i × weight_i),  Σ weight_i = 1.0
```

Weights come from config (`scoring:` block), never from code. Component raws
are produced by their own engine and are independently meaningful — an RS
score of 95 means the same thing whether or not a VCP is present.

**Vocabulary discipline.** The system reports *measurements*:

- `Pattern Score`, `Setup Quality`, `Breakout Readiness`, `RS Rank`

It does not report *predictions*: no "Buy", no "Target", no "probability of
profit" unless a calibration study exists and is cited on screen.
`SetupGrade` (A+/A/B+/B/C) is a bucketing of `total`, defined in config, and
is labelled in the UI as a research classification.

## 8. Point-in-time universe resolution

```python
UniverseService.members_as_of(index="NIFTY500", as_of=date(2021, 3, 4))
```

Resolves via `universe_members` where
`effective_from ≤ as_of AND (effective_to IS NULL OR effective_to > as_of)`.

If no row predates `as_of` for that index, the service returns the current
membership **and** sets `SurvivorshipMode.CURRENT_UNIVERSE` on the result.
Callers must propagate that flag to any stored run. The bias is then visible
in the UI instead of being an unmarked property of the numbers.

## 9. Versioning

```python
FEATURES_VERSION = "FEATURES_V1.0"
VCP_ENGINE_VERSION = "VCP_ENGINE_V1.0"
SCORING_RULE_VERSION = "SCORING_V1.0"
```

Every persisted derived row carries the version(s) that produced it. Rules:

- A change to a formula bumps **minor**.
- A change to the *meaning* of an output bumps **major**.
- Recomputation writes new rows. Historical rows keep their old version.
- Any query mixing versions must group by version or say which it filtered to.

## 10. Numeric conventions

| Quantity | Representation |
|---|---|
| Prices | `NUMERIC(18,4)` stored; `float64` in frames |
| Percentages | whole numbers (`9.27` = 9.27%), never fractions |
| Returns | fractions in engines (`0.0927`), rendered as % at the edge |
| Volume | `NUMERIC(20,2)` — Indian large caps exceed int32 |
| Scores | float `0..100`, rounded to 2dp at the API boundary only |
| Dates | `date` for daily; `datetime` (tz-aware, IST) for intraday |

Percent-vs-fraction confusion is the single most common source of silently
wrong thresholds. The rule: **fractions inside engines, percentages at every
boundary a human reads.** Field names carry `_pct` when they are percentages.
