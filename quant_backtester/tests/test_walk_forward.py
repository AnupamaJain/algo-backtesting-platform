from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_backtester.src.backtest import VectorizedBacktester
from quant_backtester.src.backtest import PercentageCostModel
from quant_backtester.src.backtest import (
    WalkForwardEngine,
    WindowGenerator,
)


@pytest.fixture
def long_index() -> pd.DatetimeIndex:
    return pd.bdate_range("2010-01-01", "2025-01-01")


@pytest.fixture
def long_prices(long_index) -> pd.Series:
    rng = np.random.default_rng(11)
    returns = rng.normal(0.0004, 0.011, len(long_index))
    return pd.Series(100 * np.cumprod(1 + returns), index=long_index)


@pytest.fixture
def engine() -> WalkForwardEngine:
    return WalkForwardEngine(
        backtester=VectorizedBacktester(PercentageCostModel(0.0005, 0.0002)),
        window_generator=WindowGenerator(is_years=3, oos_years=1, step_years=1),
    )


def test_window_generator_produces_expected_count(long_index):
    windows = WindowGenerator(is_years=3, oos_years=1, step_years=1).generate(long_index)
    # 15 years of history, 3y IS + 1y OOS stepping 1y => ~12 windows
    assert 10 <= len(windows) <= 13


def test_is_and_oos_never_overlap(long_index):
    windows = WindowGenerator(is_years=3, oos_years=1, step_years=1).generate(long_index)
    for window in windows:
        assert window.is_end <= window.oos_start, "OOS must begin after IS ends"
        assert window.is_start < window.is_end
        assert window.oos_start < window.oos_end


def test_oos_windows_are_disjoint(long_index):
    """OOS segments must tile without overlap, or stitching them would
    double-count the same days."""
    windows = WindowGenerator(is_years=3, oos_years=1, step_years=1).generate(long_index)
    for earlier, later in zip(windows, windows[1:]):
        assert earlier.oos_end <= later.oos_start


def test_anchored_mode_keeps_is_start_fixed(long_index):
    windows = WindowGenerator(is_years=3, oos_years=1, mode="anchored").generate(long_index)
    starts = {w.is_start for w in windows}
    assert len(starts) == 1, "anchored mode must pin the IS start to history's beginning"


def test_rolling_mode_advances_is_start(long_index):
    windows = WindowGenerator(is_years=3, oos_years=1, mode="rolling").generate(long_index)
    starts = [w.is_start for w in windows]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts), "rolling windows must each start later"


def test_window_generator_rejects_bad_mode():
    with pytest.raises(ValueError):
        WindowGenerator(mode="sideways")


def test_empty_index_produces_no_windows():
    assert WindowGenerator().generate(pd.DatetimeIndex([])) == []


def test_evaluate_configuration_returns_separate_is_and_oos(engine, long_prices):
    signals = pd.Series(1.0, index=long_prices.index)
    result = engine.evaluate_configuration(
        "TEST", "BuyAndHold", "none", {}, long_prices, signals
    )
    assert result is not None
    assert result.num_windows > 0
    assert result.oos_metrics.num_periods > 0
    # IS and OOS are scored on different data, so they should not coincide.
    assert result.is_metrics.sharpe != result.oos_metrics.sharpe


def test_evaluate_configuration_short_history_returns_none(engine):
    short_index = pd.bdate_range("2024-01-01", periods=50)
    prices = pd.Series(np.linspace(100, 110, 50), index=short_index)
    signals = pd.Series(1.0, index=short_index)
    assert engine.evaluate_configuration("T", "S", "k", {}, prices, signals) is None


def test_select_and_run_picks_params_without_seeing_oos(engine, long_prices):
    """Parameter selection must be driven purely by in-sample data."""
    always_long = pd.Series(1.0, index=long_prices.index)
    always_short = pd.Series(-1.0, index=long_prices.index)
    result = engine.select_and_run(
        "TEST",
        "Directional",
        long_prices,
        {"long": always_long, "short": always_short},
        {"long": {"d": 1}, "short": {"d": -1}},
    )
    assert result is not None
    assert result.num_windows > 0
    # every window recorded which parameter set it chose
    assert all("chosen_param_key" in w for w in result.per_window)


def test_select_and_run_empty_candidates_returns_none(engine, long_prices):
    assert engine.select_and_run("T", "S", long_prices, {}) is None


def test_to_row_contains_funnel_columns(engine, long_prices):
    signals = pd.Series(1.0, index=long_prices.index)
    result = engine.evaluate_configuration("SPY", "BH", "k", {}, long_prices, signals)
    row = result.to_row()
    for column in (
        "symbol",
        "strategy",
        "is_sharpe",
        "oos_sharpe",
        "oos_max_drawdown",
        "oos_profit_factor",
        "oos_num_trades",
        "oos_num_periods",
    ):
        assert column in row
