# Pramana — Test Strategy

## 1. What testing is for here

In most applications a bug produces a visibly wrong screen. In this one, the
characteristic failure is a number that looks entirely reasonable and is
quietly wrong — a backtest that returns 34% CAGR because an EMA peeked one bar
ahead. Nobody notices, because the output is plausible.

So the test suite is organised around the failures that are *invisible*:

1. **Look-ahead** — a result changes when future data arrives
2. **Survivorship** — the universe is resolved as of the wrong date
3. **Silent degradation** — bad data flows through as a number instead of an error
4. **Drift** — the backtester and the live scanner stop agreeing
5. **Unit confusion** — percent vs fraction, adjusted vs raw

Everything else (CRUD, serialisation) is ordinary and tested ordinarily.

## 2. Layers

| Layer | Style | Data | Must be |
|---|---|---|---|
| L2 features | Unit, hand-computed | Tiny synthetic frames | Exact |
| L3 engines | Unit + golden | Fixed CSVs | Exact to 0.01 |
| L4 services | Integration, in-memory DB | Factories | Deterministic |
| L1 data | Unit + contract | Fakes + one recorded real response | Fail loudly |
| L5 jobs | Integration | Seeded DB | Idempotent |
| L6 API | Contract | Seeded DB | Schema-stable |

## 3. Golden datasets

`tests/golden/` holds small, committed CSVs with expected outputs alongside:

```
tests/golden/
  vcp_textbook.csv          + vcp_textbook.expected.json
  vcp_imperfect.csv         + ...      # 18→11→12→6, must still score well
  vcp_too_deep.csv          + ...      # must be rejected
  vcp_no_prior_trend.csv    + ...      # must be rejected
  smc_bos_choch.csv         + ...
  fvg_three_bar.csv         + ...
  regime_bull.csv / regime_bear.csv / regime_chop.csv
  rs_cross_section.csv      + ...
```

Rules:
- Generated once, reviewed by hand, then **frozen**. A golden file changes only
  in a commit that explains why the correct answer changed.
- Small enough to read (≤ 400 rows) so the expected value can be verified
  manually rather than trusted.
- Expected values carry the `engine_version` that produced them. A version bump
  requires regenerating and re-reviewing, which is the point — it forces
  someone to look at what moved.

## 4. The look-ahead test (the one that matters most)

Pattern, applied at every level from features to whole scans:

```python
def assert_no_lookahead(compute, df, cutoff):
    truncated = compute(df.loc[:cutoff])
    full      = compute(df).loc[:cutoff]
    assert_frame_equal(truncated, full, check_exact=True)
```

Applied to:

| Target | Test |
|---|---|
| Every feature column | `test_causality.py::test_all_features_causal` |
| VCP detection | `test_vcp_no_lookahead` |
| Pivot stability | `test_vcp_pivot_stability` — pivot cannot change retroactively |
| Swings/BOS/CHoCH | `test_smc_no_lookahead` |
| FVG status | `test_fvg_no_lookahead` |
| RS ranks | `test_rs_no_lookahead` |
| Regime | `test_regime_no_lookahead` |
| Whole daily scan | `test_scan_no_lookahead` |

`check_exact=True` matters. A tolerance of `1e-9` hides a one-bar shift in a
long EMA, which is precisely the bug being hunted.

The scan-level version is the acceptance gate:

> Run the full pipeline with data through `T`. Record every row. Append the
> next 60 trading days. Re-run as of `T`. Every row must be byte-identical.

## 5. Engine purity test

```python
def test_engines_import_nothing_impure():
    for module in walk("src/vriddhix/engines"):
        for imported in ast_imports(module):
            assert imported.split(".")[0] not in {
                "vriddhix.db", "vriddhix.api", "vriddhix.jobs",
                "vriddhix.services", "requests", "httpx", "sqlalchemy", "redis",
            }
```

Cheap, and it prevents the single architectural failure that would make every
backtest untrustworthy: an engine reaching for live state.

A companion test asserts no engine module contains `datetime.now()`,
`date.today()`, or `time.time()` — an engine that knows the wall clock behaves
differently in a backtest than it did live.

## 6. Survivorship tests

```python
def test_universe_point_in_time():
    # DELISTED_CO was in the index in 2021 and removed in 2023
    members = universe.members_as_of("NIFTY500", date(2021, 6, 1))
    assert "DELISTED_CO" in members           # must not vanish from history

def test_survivorship_mode_flagged():
    result = universe.members_as_of("NIFTY500", date(2005, 1, 1))
    assert result.mode is SurvivorshipMode.CURRENT_UNIVERSE   # no data that far back

def test_backtest_propagates_survivorship_warning():
    run = backtest(..., start=date(2005,1,1))
    assert run.metrics["survivorship_warning"] is not None
```

The third is the one that protects the user: an unmarked biased backtest is
worse than no backtest.

## 7. Data quality tests

Each rule gets a test proving it **raises or records**, never silently passes:

| Input | Expected |
|---|---|
| Duplicate date | `DUPLICATE` event, one row kept, deterministic |
| `high < low` | `INVALID_OHLC` ERROR, bar rejected |
| `close > high` | `INVALID_OHLC` ERROR |
| Negative price | `NEGATIVE_PRICE` ERROR |
| Zero volume | `ZERO_VOLUME` WARN, bar kept (halts are real) |
| 40% overnight gap, no corp action | `SUSPECTED_SPLIT` WARN |
| Missing bar on a trading day | `MISSING_BAR` WARN |
| Frame with NaN in OHLC | `InvalidPriceFrame` raised |

The zero-volume case is deliberately WARN-not-ERROR: NSE circuit halts produce
genuine zero-volume bars, and deleting them would fabricate a gap.

## 8. Determinism

```python
def test_scan_is_reproducible():
    a = run_scan(as_of=T, seed=42)
    b = run_scan(as_of=T, seed=42)
    assert a == b
```

Plus: no engine may consume unseeded randomness, and dict iteration order must
never affect output (sorted keys at every boundary that reaches a score).

## 9. Live/backtest equivalence

The direct proof of the architecture's central claim:

```python
def test_live_and_backtest_agree():
    live = scanner.scan(universe, as_of=T)                    # service path
    bt   = backtester.signals_on(universe, date=T)            # backtest path
    assert {r.symbol: r.score for r in live} == \
           {r.symbol: r.score for r in bt}
```

If this fails, two implementations exist somewhere and the whole system's
research output is suspect.

## 10. Property-based tests

Where invariants are clearer than examples (`hypothesis`):

- Any valid OHLC frame ⇒ `atr ≥ 0`
- Any frame ⇒ `ema_20` bounded by `[min(close), max(close)]` over its window
- Any contraction sequence ⇒ score in `[0, 100]`
- Any universe ⇒ RS ranks are a permutation of `1..n`
- Any FVG ⇒ `mitigation_pct` monotonically non-decreasing
- Any pattern ⇒ no lifecycle transition skips a state

## 11. Fixtures

- **DB**: SQLite in-memory per test, schema via Alembic (`upgrade head`) so
  migrations are exercised by every run rather than only in deployment.
- **Frames**: `make_ohlcv(pattern="uptrend"|"vcp"|"downtrend"|"chop", bars=N,
  seed=)` produces controlled synthetic series.
- **Clock**: `freeze_time` for anything date-relative; engines are forbidden
  from reading the clock at all (§5), so this applies only to services.
- **Providers**: `FakeProvider` returning fixed frames; real network calls are
  never made in tests.

## 12. Coverage expectations

Coverage percentage is a weak signal, so the gates are targeted:

| Area | Requirement |
|---|---|
| `engines/` | 95% lines **and** every config gate exercised at both sides |
| `features/` | 95% + causality test per column |
| `data/quality.py` | 100% — every rule has a test |
| `services/` | 85% |
| `api/` | Contract tests for every endpoint's success + each error code |

## 13. CI gates

A merge is blocked unless:

1. All tests pass
2. No-look-ahead suite passes
3. Engine purity test passes
4. Golden outputs unchanged, **or** the diff is explained in the commit body
5. Migrations apply cleanly forward, and `downgrade` runs without error
6. No new `# type: ignore` in `engines/` or `features/`

## 14. What is deliberately not tested

- Exact pixel rendering of charts
- Live broker connectivity (mocked; integration is manual and gated)
- LLM prose quality — asserted structurally (cites only supplied fields,
  contains the disclaimer, never emits "buy"/"target"/"guaranteed"), not
  semantically

## 15. Phase 1 test inventory (implemented now)

| File | Covers |
|---|---|
| `test_config.py` | Typed load, weight-sum validation, env override, unknown key rejection |
| `test_domain.py` | Enum/state-machine invariants, frozen dataclasses |
| `test_models.py` | Schema round-trip, constraints, cascade behaviour |
| `test_providers.py` | Provider contract, normalisation, failure surfacing |
| `test_quality.py` | Every validation rule, both directions |
| `test_universe.py` | Point-in-time resolution, survivorship flagging |
| `test_features.py` | Hand-computed EMA/ATR/returns/52w/rel-volume |
| `test_causality.py` | Every feature column causal at every cutoff |
| `test_ingest.py` | Idempotent re-ingest, partial failure, quality events written |
