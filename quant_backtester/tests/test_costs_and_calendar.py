"""Regression tests for two bugs found by auditing a full-universe run.

1. Layer 4 charged no execution cost on portfolio rebalancing. Strand returns
   carried the cost of signal flips, but the exposure overlay on top (ATR
   sizing recomputed daily, regime switches, correlation weights) generated
   real trading that was never charged.

2. Annualization used a single global 252 bars/year. Crypto trades 365, so its
   Sharpe was understated by ~20% and its CAGR by a factor of 1.45.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_backtester.src.backtest import (
    PercentageCostModel,
    VectorizedBacktester,
    WalkForwardEngine,
    WindowGenerator,
    infer_periods_per_year,
)
from quant_backtester.src.config import AllocationConfig, RiskConfig
from quant_backtester.src.regime import (
    RiskManager,
    StaticPortfolio,
    StrandSet,
    apply_portfolio_costs,
)


# ==========================================================================
# 1. Calendar inference
# ==========================================================================


def test_infers_equity_calendar_from_business_days():
    index = pd.bdate_range("2015-01-01", periods=2520)  # ~10 years of weekdays
    assert infer_periods_per_year(index) == 252


def test_infers_crypto_calendar_from_daily_bars():
    index = pd.date_range("2015-01-01", periods=3650, freq="D")  # every day
    assert infer_periods_per_year(index) == 365


def test_short_series_falls_back_to_equity_default():
    assert infer_periods_per_year(pd.DatetimeIndex([])) == 252
    assert infer_periods_per_year(pd.DatetimeIndex(["2020-01-01"])) == 252


def _series(index: pd.DatetimeIndex, seed: int = 0) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(100 * np.cumprod(1 + rng.normal(0.0004, 0.01, len(index))), index=index)


def test_seven_day_asset_annualizes_higher_than_weekday_asset():
    """The same daily return distribution compounds to a higher annualized
    Sharpe when the asset trades 365 days instead of 252."""
    weekday_index = pd.bdate_range("2015-01-01", periods=1500)
    daily_index = pd.date_range("2015-01-01", periods=1500, freq="D")

    backtester = VectorizedBacktester(PercentageCostModel(0.0, 0.0))
    long_only = lambda idx: pd.Series(1.0, index=idx)  # noqa: E731

    weekday = backtester.run(_series(weekday_index, 1), long_only(weekday_index))
    crypto = backtester.run(_series(daily_index, 1), long_only(daily_index))

    assert weekday.metrics.sharpe != crypto.metrics.sharpe
    # Same underlying returns, only the calendar differs.
    ratio = crypto.metrics.sharpe / weekday.metrics.sharpe
    assert ratio == pytest.approx(np.sqrt(365 / 252), rel=0.02)


def test_explicit_periods_per_year_overrides_inference():
    daily_index = pd.date_range("2015-01-01", periods=1200, freq="D")
    pinned = VectorizedBacktester(PercentageCostModel(0.0, 0.0), periods_per_year=252)
    assert pinned.periods_for(daily_index) == 252

    auto = VectorizedBacktester(PercentageCostModel(0.0, 0.0))
    assert auto.periods_for(daily_index) == 365


def test_walk_forward_reports_the_calendar_it_scored_on():
    index = pd.date_range("2010-01-01", periods=5000, freq="D")
    engine = WalkForwardEngine(
        backtester=VectorizedBacktester(PercentageCostModel(0.0, 0.0)),
        window_generator=WindowGenerator(3, 1, 1),
    )
    result = engine.evaluate_configuration(
        "BTC-USD", "BuyHold", "k", {}, _series(index), pd.Series(1.0, index=index)
    )
    assert result is not None
    assert result.periods_per_year == 365
    assert result.to_row()["periods_per_year"] == 365


# ==========================================================================
# 2. Portfolio rebalancing costs
# ==========================================================================


def _strands(index: pd.DatetimeIndex) -> StrandSet:
    rng = np.random.default_rng(3)
    gross = pd.DataFrame({"A|S": rng.normal(0.0005, 0.01, len(index))}, index=index)
    return StrandSet(
        gross_returns=gross,
        positions=pd.DataFrame({"A|S": 1.0}, index=index),
        sizes=pd.DataFrame({"A|S": 1.0}, index=index),
        prices=pd.DataFrame({"A|S": 100.0}, index=index),
    )


def test_rebalancing_turnover_is_charged():
    """A portfolio whose allocation moves every day must pay for it, even
    though the underlying strand never changes direction."""
    index = pd.bdate_range("2020-01-01", periods=250)
    strands = _strands(index)

    steady = pd.DataFrame({"A|S": 0.5}, index=index)
    churning = pd.DataFrame(
        {"A|S": [0.5 + 0.4 * (i % 2) for i in range(len(index))]}, index=index
    )

    cost_model = PercentageCostModel(0.001, 0.001)
    steady_returns, steady_turnover = apply_portfolio_costs(steady, strands, cost_model)
    churn_returns, churn_turnover = apply_portfolio_costs(churning, strands, cost_model)

    assert churn_turnover.sum() > steady_turnover.sum()
    assert churn_returns.sum() < steady_returns.sum(), "churn must cost money"


def test_zero_cost_model_leaves_returns_untouched():
    index = pd.bdate_range("2020-01-01", periods=100)
    strands = _strands(index)
    allocation = pd.DataFrame({"A|S": 0.5}, index=index)

    free, _ = apply_portfolio_costs(allocation, strands, PercentageCostModel(0.0, 0.0))
    expected = (allocation["A|S"] * strands.gross_returns["A|S"])
    pd.testing.assert_series_equal(free, expected, check_names=False)


def test_opening_position_is_charged():
    """Establishing the book on day one is a real trade, not a free lunch."""
    index = pd.bdate_range("2020-01-01", periods=50)
    strands = _strands(index)
    allocation = pd.DataFrame({"A|S": 1.0}, index=index)  # constant after day 1

    _, turnover = apply_portfolio_costs(allocation, strands, PercentageCostModel(0.001, 0.0))
    assert turnover.iloc[0] == pytest.approx(1.0)
    assert turnover.iloc[1:].sum() == pytest.approx(0.0)


def test_direction_flip_doubles_turnover():
    index = pd.bdate_range("2020-01-01", periods=10)
    strands = _strands(index)
    positions = strands.positions.copy()
    positions.iloc[5:] = -1.0
    strands = StrandSet(strands.gross_returns, positions, strands.sizes, strands.prices)

    allocation = pd.DataFrame({"A|S": 1.0}, index=index)
    _, turnover = apply_portfolio_costs(allocation, strands, PercentageCostModel(0.0, 0.0))
    # +1 -> -1 is a turnover of 2.0 on the flip bar.
    assert turnover.iloc[5] == pytest.approx(2.0)


def _ohlcv(seed: int, n: int = 3800) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2010-01-01", periods=n)
    close = 100 * np.cumprod(1 + rng.normal(0.0004, 0.011, n))
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.005,
            "Low": close * 0.995,
            "Close": close,
            "Volume": 1_000_000,
        },
        index=dates,
    )


def test_static_baseline_is_also_costed():
    """The baseline rebalances daily through ATR sizing too. Costing the
    dynamic portfolio but not its benchmark would rig the comparison."""
    risk = RiskConfig(0.15, 14, 1.0, 3.0, 20, 0.15, 0.0, 10)
    price_data = {"AAA": _ohlcv(1)}
    selected = pd.DataFrame(
        [{"symbol": "AAA", "strategy": "RSIReversion", "params": {"rsi_period": 14}}]
    )

    free = StaticPortfolio(
        RiskManager(risk), VectorizedBacktester(PercentageCostModel(0.0, 0.0))
    ).run(price_data, selected)
    costly = StaticPortfolio(
        RiskManager(risk), VectorizedBacktester(PercentageCostModel(0.002, 0.002))
    ).run(price_data, selected)

    assert costly.metrics.total_return < free.metrics.total_return
    assert costly.turnover.sum() > 0
