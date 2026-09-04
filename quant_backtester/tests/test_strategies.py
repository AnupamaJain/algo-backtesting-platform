from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_backtester.src.strategies import (
    BaseStrategy,
    BBReversion,
    KeltnerReversion,
    MACrossover,
    RSIReversion,
    SimpleMomentum,
    StrategyValidationError,
    TurtleBreakout,
)

ALL_STRATEGIES = [
    RSIReversion,
    BBReversion,
    KeltnerReversion,
    MACrossover,
    TurtleBreakout,
    SimpleMomentum,
]


@pytest.mark.parametrize("strategy_class", ALL_STRATEGIES)
def test_generate_signals_contract(strategy_class, synthetic_ohlcv):
    strategy = strategy_class()
    signals = strategy.generate_signals(synthetic_ohlcv)

    assert list(signals.index) == list(synthetic_ohlcv.index)
    assert signals.isin([-1, 0, 1]).all()
    assert not signals.isna().any()


def test_missing_columns_raise():
    bad_data = pd.DataFrame(
        {"Close": [1.0, 2.0]}, index=pd.date_range("2020-01-01", periods=2)
    )
    with pytest.raises(StrategyValidationError):
        RSIReversion().generate_signals(bad_data)


def test_non_datetime_index_raises(synthetic_ohlcv):
    bad = synthetic_ohlcv.reset_index(drop=True)
    with pytest.raises(StrategyValidationError):
        RSIReversion().generate_signals(bad)


def test_rsi_reversion_param_validation():
    with pytest.raises(StrategyValidationError):
        RSIReversion(oversold_threshold=80, overbought_threshold=20)


def test_ma_crossover_param_validation():
    with pytest.raises(StrategyValidationError):
        MACrossover(fast_period=200, slow_period=50)


def test_ma_crossover_matches_manual_sign(synthetic_ohlcv):
    strategy = MACrossover(fast_period=5, slow_period=20)
    signals = strategy.generate_signals(synthetic_ohlcv)

    fast = synthetic_ohlcv["Close"].rolling(5).mean()
    slow = synthetic_ohlcv["Close"].rolling(20).mean()
    expected = np.sign(fast - slow).fillna(0).astype(int)

    pd.testing.assert_series_equal(signals, expected, check_names=False)


def test_simple_momentum_no_lookahead(synthetic_ohlcv):
    strategy = SimpleMomentum(lookback_period=10)
    signals = strategy.generate_signals(synthetic_ohlcv)

    # First `lookback_period` rows have no valid comparison point yet and
    # must be flat, not an arbitrary guess.
    assert (signals.iloc[:10] == 0).all()


def test_turtle_breakout_stays_in_bounds(synthetic_ohlcv):
    strategy = TurtleBreakout(entry_period=20, exit_period=10)
    signals = strategy.generate_signals(synthetic_ohlcv)
    assert signals.isin([-1, 0, 1]).all()


def test_param_key_is_stable_and_sorted():
    a = RSIReversion(rsi_period=14, oversold_threshold=30, overbought_threshold=70)
    b = RSIReversion(overbought_threshold=70, oversold_threshold=30, rsi_period=14)
    assert a.param_key() == b.param_key()


def test_states_from_entries_forward_fills():
    idx = pd.date_range("2020-01-01", periods=6)
    long_entry = pd.Series([True, False, False, False, False, False], index=idx)
    short_entry = pd.Series([False, False, False, True, False, False], index=idx)
    result = BaseStrategy._states_from_entries(long_entry, short_entry)
    assert list(result) == [1, 1, 1, -1, -1, -1]
