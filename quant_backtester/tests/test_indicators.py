from __future__ import annotations

import numpy as np
import pandas as pd

from quant_backtester.src import strategies as indicators


def test_rsi_bounds(synthetic_ohlcv):
    values = indicators.rsi(synthetic_ohlcv["Close"], period=14).dropna()
    assert (values >= 0).all() and (values <= 100).all()


def test_bollinger_bands_ordering(synthetic_ohlcv):
    upper, mid, lower = indicators.bollinger_bands(synthetic_ohlcv["Close"], 20, 2.0)
    valid = mid.notna()
    assert (upper[valid] >= mid[valid]).all()
    assert (mid[valid] >= lower[valid]).all()


def test_donchian_channel_excludes_current_bar():
    # Construct a series where day 10 has an extreme high that must NOT
    # appear in its own day's channel (that would be look-ahead).
    high = pd.Series([10.0] * 9 + [100.0] + [10.0] * 5)
    low = high - 1
    upper, _ = indicators.donchian_channel(high, low, period=5)
    assert upper.iloc[9] < 100.0  # day 10's own spike isn't in day 10's channel
    assert upper.iloc[10] == 100.0  # but it is visible from day 11 onward


def test_atr_non_negative(synthetic_ohlcv):
    values = indicators.atr(
        synthetic_ohlcv["High"], synthetic_ohlcv["Low"], synthetic_ohlcv["Close"], 14
    ).dropna()
    assert (values >= 0).all()
