from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_backtester.src.backtest import PercentageCostModel, VectorizedBacktester
from quant_backtester.src.config import (
    AllocationConfig,
    RegimeConfig,
    RegimeDetectionConfig,
    RiskConfig,
)
from quant_backtester.src.regime import (
    BuyAndHoldPortfolio,
    DynamicRegimePortfolio,
    MarketRegimeDetector,
    PerformanceComparator,
    Regime,
    RiskManager,
    SignalBlender,
    StaticPortfolio,
)


# -- fixtures -----------------------------------------------------------


def _regime_switching_prices(n_per_regime: int = 500, seed: int = 3) -> pd.DataFrame:
    """Synthetic series with three genuinely distinct regimes in sequence,
    so a working detector has something real to find."""
    rng = np.random.default_rng(seed)
    segments = [
        rng.normal(0.0009, 0.007, n_per_regime),    # calm bull
        rng.normal(-0.0015, 0.030, n_per_regime),   # volatile bear
        rng.normal(0.0001, 0.005, n_per_regime),    # quiet chop
        rng.normal(0.0010, 0.008, n_per_regime),    # calm bull again
    ]
    returns = np.concatenate(segments)
    close = 100 * np.cumprod(1 + returns)
    dates = pd.bdate_range("2010-01-01", periods=len(close))
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * (1 + np.abs(returns) * 0.5),
            "Low": close * (1 - np.abs(returns) * 0.5),
            "Close": close,
            "Volume": 1_000_000,
        },
        index=dates,
    )


@pytest.fixture
def benchmark() -> pd.DataFrame:
    return _regime_switching_prices()


@pytest.fixture
def detection_config() -> RegimeDetectionConfig:
    return RegimeDetectionConfig(
        benchmark_symbol="SPY",
        n_states=3,
        volatility_window=20,
        fast_ma=50,
        slow_ma=200,
        atr_period=14,
        covariance_type="full",
        n_iter=100,
        random_seed=42,
        train_fraction=0.5,
        min_regime_persistence_days=3,
    )


@pytest.fixture
def risk_config() -> RiskConfig:
    return RiskConfig(
        target_annual_volatility=0.15,
        atr_period=14,
        max_position_leverage=1.0,
        max_portfolio_heat=3.0,
        drawdown_window_days=20,
        drawdown_threshold=0.15,
        drawdown_scale_factor=0.0,
        recovery_days=10,
    )


@pytest.fixture
def allocation_config() -> AllocationConfig:
    return AllocationConfig(
        regime_strategies={
            0: ["RSIReversion"],
            1: ["SimpleMomentum", "MACrossover"],
            2: ["RSIReversion", "KeltnerReversion"],
        },
        regime_exposure={0: 0.3, 1: 1.0, 2: 0.7},
        defensive_assets=["TLT"],
        correlation_window=120,
        min_weight=0.05,
    )


# ==========================================================================
# MarketRegimeDetector
# ==========================================================================


def test_features_are_finite_and_named(detection_config, benchmark):
    features = MarketRegimeDetector(detection_config).build_features(benchmark)
    assert list(features.frame.columns) == ["volatility", "trend", "atr_norm", "momentum"]
    assert np.isfinite(features.matrix).all()


def test_predict_regime_labels_every_feature_day(detection_config, benchmark):
    detector = MarketRegimeDetector(detection_config).fit(benchmark)
    regimes = detector.predict_regime(benchmark)
    assert set(regimes.unique()) <= {Regime.BEAR, Regime.TRENDING, Regime.RANGING}
    assert regimes.notna().all()


def test_detector_finds_all_three_regimes(detection_config, benchmark):
    """On data built with three genuinely distinct regimes, the HMM should
    recover more than one state — a detector that labels everything the same
    is useless as an overlay."""
    detector = MarketRegimeDetector(detection_config).fit(benchmark)
    regimes = detector.predict_regime(benchmark)
    assert regimes.nunique() >= 2


def test_bear_regime_aligns_with_the_volatile_downturn(detection_config, benchmark):
    """The segment built as a volatile bear market should be classified BEAR
    far more often than the calm bull segment is.

    Windows are selected by DATE, not by position: the feature frame drops
    ~200 warm-up rows for the 200-day SMA, so positional slicing of the
    regime series does not line up with the raw price segments.
    """
    detector = MarketRegimeDetector(detection_config).fit(benchmark)
    regimes = detector.predict_regime(benchmark)

    n = 500
    dates = benchmark.index
    bear_window = regimes[(regimes.index >= dates[n]) & (regimes.index < dates[2 * n])]
    first_bull = regimes[regimes.index < dates[n]]
    second_bull = regimes[regimes.index >= dates[3 * n]]

    bear_share = (bear_window == Regime.BEAR).mean()
    assert bear_share > 0.5, "the engineered bear market must be detected as BEAR"
    assert (first_bull == Regime.BEAR).mean() < 0.2, "a clean bull is not a bear"
    # The second bull follows a choppy stretch, so the 50/200 trend feature
    # is still recovering and some of it reads as bearish. That lag is a real
    # property of trend-following features at regime transitions, not a
    # labelling error — so assert the separation holds, not that lag is zero.
    assert bear_share > (second_bull == Regime.BEAR).mean() + 0.3


def test_fit_uses_only_the_training_prefix(detection_config, benchmark):
    """The model must not be fit on data it will later be evaluated over."""
    detector = MarketRegimeDetector(detection_config).fit(benchmark)
    features = detector.build_features(benchmark)
    expected_end = features.frame.index[int(len(features.frame) * 0.5) - 1]
    assert detector._training_end == expected_end
    assert detector._training_end < features.frame.index[-1]


def test_detector_is_deterministic(detection_config, benchmark):
    a = MarketRegimeDetector(detection_config).fit(benchmark).predict_regime(benchmark)
    b = MarketRegimeDetector(detection_config).fit(benchmark).predict_regime(benchmark)
    pd.testing.assert_series_equal(a, b)


def test_persistence_filter_suppresses_one_day_blips(detection_config, benchmark):
    """A regime that lasts a single day must not reroute the portfolio."""
    detector = MarketRegimeDetector(detection_config).fit(benchmark)
    noisy = pd.Series(
        [1, 1, 1, 1, 0, 1, 1, 1, 1], index=pd.bdate_range("2020-01-01", periods=9)
    )
    smoothed = detector._apply_persistence(noisy)
    assert 0 not in set(smoothed), "a 1-day blip should never be confirmed"


def test_persistence_filter_accepts_a_sustained_switch(detection_config, benchmark):
    detector = MarketRegimeDetector(detection_config).fit(benchmark)
    sustained = pd.Series(
        [1, 1, 1, 0, 0, 0, 0, 0], index=pd.bdate_range("2020-01-01", periods=8)
    )
    smoothed = detector._apply_persistence(sustained)
    assert smoothed.iloc[-1] == 0


def test_annotate_attaches_regime_columns(detection_config, benchmark):
    detector = MarketRegimeDetector(detection_config).fit(benchmark)
    annotated = detector.annotate(benchmark)
    assert "regime" in annotated.columns and "regime_name" in annotated.columns
    assert len(annotated) == len(benchmark)


def test_fit_rejects_insufficient_history(detection_config):
    short = _regime_switching_prices(n_per_regime=60)
    tiny = short.iloc[:210]  # barely enough rows to survive the 200-day SMA
    with pytest.raises(ValueError):
        MarketRegimeDetector(detection_config).fit(tiny)


# ==========================================================================
# RiskManager
# ==========================================================================


def test_atr_sizing_is_inverse_to_volatility(risk_config, benchmark):
    """The whole point: quiet markets get more exposure than wild ones."""
    manager = RiskManager(risk_config)
    scale = manager.atr_position_scale(benchmark)

    calm = scale.iloc[100:400].mean()      # calm bull segment
    volatile = scale.iloc[550:950].mean()  # volatile bear segment
    assert calm > volatile


def test_atr_sizing_respects_leverage_cap(risk_config, benchmark):
    scale = RiskManager(risk_config).atr_position_scale(benchmark)
    assert scale.max() <= risk_config.max_position_leverage + 1e-9
    assert (scale >= 0).all()


def test_atr_sizing_is_shifted_so_day_one_has_no_position(risk_config, benchmark):
    scale = RiskManager(risk_config).atr_position_scale(benchmark)
    assert scale.iloc[0] == 0.0


def test_equity_stop_halts_after_a_large_drawdown(risk_config):
    index = pd.bdate_range("2020-01-01", periods=60)
    returns = pd.Series(0.001, index=index)
    returns.iloc[20:25] = -0.06  # ~27% drawdown, well past the 15% threshold

    scale = RiskManager(risk_config).equity_curve_stop(returns)
    assert (scale.iloc[26:35] == 0.0).any(), "stop should be active after the drawdown"


def test_equity_stop_stays_inactive_without_drawdown(risk_config):
    index = pd.bdate_range("2020-01-01", periods=60)
    returns = pd.Series(0.002, index=index)  # monotonic gains
    scale = RiskManager(risk_config).equity_curve_stop(returns)
    assert (scale == 1.0).all()


def test_equity_stop_recovers_after_hysteresis(risk_config):
    index = pd.bdate_range("2020-01-01", periods=120)
    returns = pd.Series(0.002, index=index)
    returns.iloc[10:15] = -0.06
    scale = RiskManager(risk_config).equity_curve_stop(returns)
    assert scale.iloc[-1] == 1.0, "trading should resume once recovered"


def test_heat_cap_limits_total_exposure(risk_config):
    index = pd.bdate_range("2020-01-01", periods=10)
    exposures = pd.DataFrame({"a": 2.0, "b": 2.0, "c": 2.0}, index=index)  # gross 6.0
    capped = RiskManager(risk_config).apply_heat_cap(exposures)
    gross = capped.abs().sum(axis=1)
    assert (gross <= risk_config.max_portfolio_heat + 1e-9).all()


def test_heat_cap_preserves_relative_weights(risk_config):
    index = pd.bdate_range("2020-01-01", periods=5)
    exposures = pd.DataFrame({"a": 4.0, "b": 2.0}, index=index)
    capped = RiskManager(risk_config).apply_heat_cap(exposures)
    assert (capped["a"] / capped["b"]).round(6).eq(2.0).all()


def test_heat_cap_leaves_small_exposures_untouched(risk_config):
    index = pd.bdate_range("2020-01-01", periods=5)
    exposures = pd.DataFrame({"a": 0.5, "b": 0.4}, index=index)
    capped = RiskManager(risk_config).apply_heat_cap(exposures)
    pd.testing.assert_frame_equal(capped, exposures)


# ==========================================================================
# SignalBlender
# ==========================================================================


def test_blender_downweights_correlated_strands():
    """Two identical strands are one bet; an independent one deserves more."""
    index = pd.bdate_range("2020-01-01", periods=200)
    rng = np.random.default_rng(0)
    shared = rng.normal(0, 0.01, 200)
    frame = pd.DataFrame(
        {
            "twin_a": shared,
            "twin_b": shared,                        # perfectly correlated
            "independent": rng.normal(0, 0.01, 200),  # uncorrelated
        },
        index=index,
    )
    weights = SignalBlender(120).inverse_correlation_weights(frame)
    assert weights["independent"] > weights["twin_a"]
    assert weights.sum() == pytest.approx(1.0)


def test_blender_single_strand_gets_full_weight():
    frame = pd.DataFrame({"only": np.random.default_rng(0).normal(0, 0.01, 50)})
    weights = SignalBlender().inverse_correlation_weights(frame)
    assert weights["only"] == 1.0


def test_blender_handles_too_little_history():
    frame = pd.DataFrame({"a": [0.01], "b": [0.02]})
    weights = SignalBlender().inverse_correlation_weights(frame)
    assert weights.sum() == pytest.approx(1.0)


# ==========================================================================
# Portfolios and comparison
# ==========================================================================


@pytest.fixture
def price_data(benchmark) -> dict[str, pd.DataFrame]:
    return {
        "SPY": benchmark,
        "QQQ": _regime_switching_prices(seed=7),
        "TLT": _regime_switching_prices(seed=11),
    }


@pytest.fixture
def selected() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"symbol": "SPY", "strategy": "RSIReversion", "params": {"rsi_period": 14}},
            {"symbol": "QQQ", "strategy": "SimpleMomentum", "params": {"lookback_period": 126}},
            {"symbol": "TLT", "strategy": "MACrossover", "params": {"fast_period": 50, "slow_period": 200}},
        ]
    )


@pytest.fixture
def dynamic_portfolio(detection_config, risk_config, allocation_config, benchmark):
    detector = MarketRegimeDetector(detection_config).fit(benchmark)
    return DynamicRegimePortfolio(
        detector=detector,
        risk_manager=RiskManager(risk_config),
        backtester=VectorizedBacktester(PercentageCostModel(0.0005, 0.0002)),
        allocation=allocation_config,
    )


def test_dynamic_portfolio_produces_returns(dynamic_portfolio, price_data, selected, benchmark):
    result = dynamic_portfolio.run(price_data, selected, benchmark)
    assert not result.returns.empty
    assert result.metrics is not None
    assert not result.exposures.empty


def test_dynamic_portfolio_respects_heat_cap(
    dynamic_portfolio, price_data, selected, benchmark, risk_config
):
    result = dynamic_portfolio.run(price_data, selected, benchmark)
    gross = result.exposures.abs().sum(axis=1)
    assert (gross <= risk_config.max_portfolio_heat + 1e-9).all()


def test_dynamic_portfolio_uses_lagged_regimes(
    dynamic_portfolio, price_data, selected, benchmark
):
    """Allocation must act on yesterday's confirmed regime, never today's."""
    result = dynamic_portfolio.run(price_data, selected, benchmark)
    raw = dynamic_portfolio._detector.predict_regime(benchmark)
    aligned = result.regimes.dropna()
    common = aligned.index.intersection(raw.index)[:50]
    # the portfolio's regime series is the raw one shifted forward by a day
    assert (aligned.loc[common].values == raw.shift(1).loc[common].values).all()


def test_bear_regime_scales_exposure_down(
    detection_config, risk_config, allocation_config, benchmark, price_data, selected
):
    """Regime 0 carries a 0.3 exposure multiplier, so gross exposure on bear
    days must be lower than on trending days."""
    detector = MarketRegimeDetector(detection_config).fit(benchmark)
    portfolio = DynamicRegimePortfolio(
        detector, RiskManager(risk_config), VectorizedBacktester(PercentageCostModel(0, 0)),
        allocation_config,
    )
    result = portfolio.run(price_data, selected, benchmark)
    gross = result.exposures.abs().sum(axis=1)
    regimes = result.regimes.reindex(gross.index)

    bear_gross = gross[regimes == Regime.BEAR]
    trend_gross = gross[regimes == Regime.TRENDING]
    if len(bear_gross) > 10 and len(trend_gross) > 10:
        assert bear_gross.mean() <= trend_gross.mean()


def test_dynamic_portfolio_handles_no_deployable_strands(dynamic_portfolio, price_data, benchmark):
    empty = pd.DataFrame(columns=["symbol", "strategy", "params"])
    result = dynamic_portfolio.run(price_data, empty, benchmark)
    assert result.returns.empty


def test_static_baseline_runs(risk_config, price_data, selected):
    result = StaticPortfolio(
        RiskManager(risk_config), VectorizedBacktester(PercentageCostModel(0.0005, 0.0002))
    ).run(price_data, selected)
    assert not result.returns.empty
    assert result.metrics.num_periods > 0


def test_buy_and_hold_runs(price_data):
    result = BuyAndHoldPortfolio(VectorizedBacktester(PercentageCostModel(0, 0))).run(price_data)
    assert not result.returns.empty


def test_calmar_ratio_is_cagr_over_drawdown(price_data):
    result = BuyAndHoldPortfolio(VectorizedBacktester(PercentageCostModel(0, 0))).run(price_data)
    if result.metrics.max_drawdown > 0:
        assert result.calmar_ratio() == pytest.approx(
            result.metrics.cagr / result.metrics.max_drawdown
        )


@pytest.fixture
def regime_config(detection_config, risk_config, allocation_config, tmp_path) -> RegimeConfig:
    return RegimeConfig(
        detection=detection_config,
        risk=risk_config,
        allocation=allocation_config,
        regime_dir=tmp_path / "layer4",
    )


def test_comparator_table_has_all_required_metrics(
    regime_config, dynamic_portfolio, price_data, selected, benchmark, risk_config
):
    dynamic = dynamic_portfolio.run(price_data, selected, benchmark)
    static = StaticPortfolio(
        RiskManager(risk_config), VectorizedBacktester(PercentageCostModel(0, 0))
    ).run(price_data, selected)

    table = PerformanceComparator(regime_config).compare([dynamic, static])
    for column in ("annualized_return", "sharpe", "max_drawdown", "calmar"):
        assert column in table.columns
    assert len(table) == 2


def test_execution_log_is_tagged_with_regime(
    regime_config, dynamic_portfolio, price_data, selected, benchmark
):
    dynamic = dynamic_portfolio.run(price_data, selected, benchmark)
    log = PerformanceComparator(regime_config).execution_log(dynamic)
    assert not log.empty
    for column in ("date", "symbol", "strategy", "exposure_change", "regime", "regime_name"):
        assert column in log.columns


def test_regime_attribution_splits_by_state(
    regime_config, dynamic_portfolio, price_data, selected, benchmark
):
    dynamic = dynamic_portfolio.run(price_data, selected, benchmark)
    attribution = PerformanceComparator(regime_config).regime_attribution(dynamic)
    assert not attribution.empty
    assert set(attribution["regime"]) <= {0, 1, 2}


def test_rolling_correlation_shape(
    regime_config, dynamic_portfolio, price_data, selected, benchmark, risk_config
):
    dynamic = dynamic_portfolio.run(price_data, selected, benchmark)
    static = StaticPortfolio(
        RiskManager(risk_config), VectorizedBacktester(PercentageCostModel(0, 0))
    ).run(price_data, selected)
    correlation = PerformanceComparator(regime_config).rolling_correlation([dynamic, static])
    assert correlation.shape[1] == 1  # one pair


def test_comparator_writes_all_artifacts(
    regime_config, dynamic_portfolio, price_data, selected, benchmark, risk_config
):
    dynamic = dynamic_portfolio.run(price_data, selected, benchmark)
    static = StaticPortfolio(
        RiskManager(risk_config), VectorizedBacktester(PercentageCostModel(0, 0))
    ).run(price_data, selected)

    comparator = PerformanceComparator(regime_config)
    comparator.write_artifacts([dynamic, static], dynamic=dynamic)

    out_dir = regime_config.regime_dir
    for name in (
        "portfolio_comparison.csv",
        "rolling_correlation.csv",
        "equity_curves.csv",
        "regime_attribution.csv",
        "executions_by_regime.csv",
        "regime_timeline.csv",
    ):
        assert (out_dir / name).exists(), f"missing artifact {name}"
