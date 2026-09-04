"""The most important tests in Layer 2: proving the engine cannot see the future."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_backtester.src.backtest import VectorizedBacktester
from quant_backtester.src.backtest import PercentageCostModel, PerShareCostModel
from quant_backtester.src.backtest import compute_asset_returns


@pytest.fixture
def zero_cost_backtester() -> VectorizedBacktester:
    return VectorizedBacktester(PercentageCostModel(0.0, 0.0))


@pytest.fixture
def prices() -> pd.Series:
    rng = np.random.default_rng(7)
    n = 300
    returns = rng.normal(0.0005, 0.012, n)
    return pd.Series(
        100 * np.cumprod(1 + returns), index=pd.bdate_range("2020-01-01", periods=n)
    )


def test_same_bar_signal_cannot_capture_same_bar_return(zero_cost_backtester, prices):
    """A signal computed from bar t's own return must NOT earn bar t's return.

    This is the canonical look-ahead trap: `signal = sign(today's return)` is
    only knowable at today's close. If the engine failed to delay execution,
    every bar would earn |return| and the strategy would be riskless. The
    one-bar shift must break that.
    """
    asset_returns = compute_asset_returns(prices)
    cheating_signal = np.sign(asset_returns)

    result = zero_cost_backtester.run(prices, cheating_signal)

    # With a correct one-bar delay, losing days must exist.
    assert (result.gross_returns < 0).any(), "engine appears to execute without delay"
    # And the impossible perfect equity curve must not appear.
    perfect = asset_returns.abs().sum()
    assert result.gross_returns.sum() < perfect


def test_position_equals_signal_shifted_by_one(zero_cost_backtester, prices):
    signals = pd.Series(1.0, index=prices.index)
    signals.iloc[:5] = 0.0
    result = zero_cost_backtester.run(prices, signals)
    pd.testing.assert_series_equal(
        result.position, signals.shift(1).fillna(0.0), check_names=False
    )


def test_first_bar_never_holds_a_position(zero_cost_backtester, prices):
    """There is no prior bar to have decided on, so bar 0 must be flat."""
    signals = pd.Series(1.0, index=prices.index)
    result = zero_cost_backtester.run(prices, signals)
    assert result.position.iloc[0] == 0.0
    assert result.gross_returns.iloc[0] == 0.0


def test_future_signal_changes_do_not_affect_past_returns(zero_cost_backtester, prices):
    """Mutating signals only in the last 50 bars must leave earlier returns
    byte-identical — proof that no backward information flow exists."""
    base = pd.Series(1.0, index=prices.index)
    mutated = base.copy()
    mutated.iloc[-50:] = -1.0

    result_base = zero_cost_backtester.run(prices, base)
    result_mutated = zero_cost_backtester.run(prices, mutated)

    pd.testing.assert_series_equal(
        result_base.net_returns.iloc[:-50], result_mutated.net_returns.iloc[:-50]
    )


def test_costs_reduce_returns(prices):
    signals = pd.Series(np.tile([1.0, -1.0], len(prices) // 2), index=prices.index)
    free = VectorizedBacktester(PercentageCostModel(0.0, 0.0)).run(prices, signals)
    costly = VectorizedBacktester(PercentageCostModel(0.001, 0.001)).run(prices, signals)

    assert costly.net_returns.sum() < free.net_returns.sum()
    assert (costly.costs >= 0).all()


def test_percentage_cost_charged_on_turnover():
    model = PercentageCostModel(commission_pct=0.001, slippage_pct=0.0)
    turnover = pd.Series([0.0, 1.0, 2.0])
    prices = pd.Series([10.0, 10.0, 10.0])
    costs = model.cost_fraction(turnover, prices)
    assert list(costs) == pytest.approx([0.0, 0.001, 0.002])


def test_per_share_cost_is_price_sensitive():
    """The same notional turnover costs more on a cheap stock than a pricey
    one, because commission is charged per share."""
    model = PerShareCostModel(commission_per_share=0.005, slippage_pct=0.0)
    turnover = pd.Series([1.0, 1.0])
    cheap_vs_expensive = pd.Series([5.0, 500.0])
    costs = model.cost_fraction(turnover, cheap_vs_expensive)
    assert costs.iloc[0] > costs.iloc[1]


def test_backtester_rejects_non_overlapping_index(zero_cost_backtester, prices):
    other = pd.Series(1.0, index=pd.bdate_range("1990-01-01", periods=10))
    with pytest.raises(ValueError):
        zero_cost_backtester.run(prices, other)
