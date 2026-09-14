"""Feature arithmetic, checked against values computed by hand.

Hand-computed rather than snapshot-compared: a snapshot only proves the code
still does what it did, which is worthless if what it did was wrong.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vriddhix.domain.types import InvalidPriceFrame
from vriddhix.features.technical import (
    atr,
    compute_features,
    ema,
    normalise_price_frame,
    rolling_extreme,
    rolling_return,
    true_range,
    validate_price_frame,
)
from conftest import make_ohlcv


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------


def test_ema_matches_hand_computation():
    # span=3 -> alpha = 2/(3+1) = 0.5, recursive form seeded at x0.
    #   y0 = 1
    #   y1 = 0.5*1    + 0.5*2 = 1.5
    #   y2 = 0.5*1.5  + 0.5*3 = 2.25
    #   y3 = 0.5*2.25 + 0.5*4 = 3.125
    #   y4 = 0.5*3.125+ 0.5*5 = 4.0625
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = ema(series, 3)

    assert result.isna().tolist()[:2] == [True, True]  # min_periods=3
    assert result.iloc[2] == pytest.approx(2.25)
    assert result.iloc[3] == pytest.approx(3.125)
    assert result.iloc[4] == pytest.approx(4.0625)


def test_ema_is_recursive_not_reweighted():
    """adjust=False: a past EMA value must not move when new bars arrive.

    With adjust=True pandas reweights the whole history, so the EMA printed
    for a past date would change -- a value a user already saw, and a backtest
    already traded on, would silently differ on the next run.
    """
    series = pd.Series(np.linspace(100, 200, 60))
    short = ema(series.iloc[:40], 20)
    long = ema(series, 20)
    pd.testing.assert_series_equal(short, long.iloc[:40], check_exact=True)


def test_ema_rejects_bad_period():
    with pytest.raises(ValueError):
        ema(pd.Series([1.0, 2.0]), 0)


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------


def test_true_range_takes_the_widest_of_three():
    df = pd.DataFrame(
        {
            "open": [10.0, 11.0, 9.0],
            "high": [11.0, 13.0, 10.0],
            "low": [9.0, 10.5, 8.0],
            "close": [10.5, 12.0, 8.5],
        },
        index=pd.bdate_range("2024-01-01", periods=3),
    )
    tr = true_range(df)

    # bar 0: no previous close -> high-low only
    assert tr.iloc[0] == pytest.approx(2.0)
    # bar 1: max(13-10.5=2.5, |13-10.5|=2.5, |10.5-10.5|=0) = 2.5
    assert tr.iloc[1] == pytest.approx(2.5)
    # bar 2: max(10-8=2, |10-12|=2, |8-12|=4) = 4  -- the gap down dominates
    assert tr.iloc[2] == pytest.approx(4.0)


def test_atr_uses_wilder_smoothing():
    df = make_ohlcv(60, seed=7)
    result = atr(df, 14)
    expected = true_range(df).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    pd.testing.assert_series_equal(result, expected, check_exact=True)


def test_atr_is_never_negative():
    result = atr(make_ohlcv(200, seed=3), 14).dropna()
    assert (result >= 0).all()


# ---------------------------------------------------------------------------
# Returns and extremes
# ---------------------------------------------------------------------------


def test_rolling_return_counts_bars_not_days():
    close = pd.Series([100.0, 110.0, 121.0], index=pd.bdate_range("2024-01-01", periods=3))
    result = rolling_return(close, 1)
    assert np.isnan(result.iloc[0])
    assert result.iloc[1] == pytest.approx(0.10)
    assert result.iloc[2] == pytest.approx(0.10)


def test_rolling_extreme_is_trailing():
    series = pd.Series([5.0, 3.0, 9.0, 1.0])
    highs = rolling_extreme(series, 3, "max")
    # Each value looks back only: the 9 at index 2 must not appear at index 1.
    assert highs.tolist() == [5.0, 5.0, 9.0, 9.0]

    lows = rolling_extreme(series, 3, "min")
    assert lows.tolist() == [5.0, 3.0, 3.0, 1.0]


# ---------------------------------------------------------------------------
# Full feature frame
# ---------------------------------------------------------------------------


def test_compute_features_produces_expected_columns():
    features = compute_features(make_ohlcv(300))
    for column in (
        "ema_20", "ema_50", "ema_100", "ema_200", "atr_14", "atr_pct",
        "avg_volume_20", "avg_volume_50", "rel_volume",
        "ret_1m", "ret_3m", "ret_6m", "ret_12m",
        "high_52w", "low_52w", "pct_from_52w_high", "pct_from_52w_low",
        "above_ema_20", "above_ema_50", "above_ema_100", "above_ema_200",
    ):
        assert column in features.columns, f"missing feature column {column}"


def test_feature_index_matches_input():
    df = make_ohlcv(120)
    features = compute_features(df)
    pd.testing.assert_index_equal(features.index, df.index)


def test_pct_from_52w_high_is_signed():
    """Negative below the high, zero at it. abs() would make '6% below the
    high' and '6% above' indistinguishable, which are opposite situations."""
    df = make_ohlcv(300, pattern="uptrend", seed=11)
    features = compute_features(df)
    assert (features["pct_from_52w_high"] <= 1e-9).all()
    assert (features["pct_from_52w_low"] >= -1e-9).all()


def test_atr_pct_is_scale_free():
    """ATR as a percentage of price is comparable across a Rs 80 stock and a
    Rs 80,000 one; absolute ATR is not."""
    cheap = make_ohlcv(200, start_price=80.0, seed=5)
    dear = cheap.copy() * 1000  # identical shape, 1000x the price
    dear["volume"] = cheap["volume"]

    a = compute_features(cheap)["atr_pct"].dropna()
    b = compute_features(dear)["atr_pct"].dropna()
    pd.testing.assert_series_equal(a, b, check_exact=False, rtol=1e-9)


def test_rel_volume_is_one_when_volume_is_flat():
    df = make_ohlcv(120, seed=2)
    df["volume"] = 500_000.0
    features = compute_features(df)
    assert features["rel_volume"].dropna().round(6).eq(1.0).all()


# ---------------------------------------------------------------------------
# Frame contract
# ---------------------------------------------------------------------------


def test_validate_rejects_empty_frame():
    with pytest.raises(InvalidPriceFrame, match="empty"):
        validate_price_frame(pd.DataFrame())


def test_validate_rejects_missing_columns():
    df = make_ohlcv(30).drop(columns=["volume"])
    with pytest.raises(InvalidPriceFrame, match="missing columns"):
        validate_price_frame(df)


def test_validate_rejects_unsorted_index():
    df = make_ohlcv(30).iloc[::-1]
    with pytest.raises(InvalidPriceFrame, match="sorted"):
        validate_price_frame(df)


def test_validate_rejects_duplicate_timestamps():
    df = make_ohlcv(30)
    df = pd.concat([df, df.iloc[[5]]]).sort_index()
    with pytest.raises(InvalidPriceFrame, match="duplicate"):
        validate_price_frame(df)


def test_validate_rejects_tz_aware_index():
    """A tz-aware index compares unequal to a plain date, so a slice silently
    returns nothing rather than failing."""
    df = make_ohlcv(30)
    df.index = df.index.tz_localize("Asia/Kolkata")
    with pytest.raises(InvalidPriceFrame, match="tz-naive"):
        validate_price_frame(df)


def test_validate_rejects_impossible_ohlc():
    df = make_ohlcv(30)
    df.iloc[10, df.columns.get_loc("high")] = df.iloc[10]["low"] - 1
    with pytest.raises(InvalidPriceFrame, match="impossible"):
        validate_price_frame(df)


def test_validate_rejects_nan_in_ohlc():
    df = make_ohlcv(30)
    df.iloc[3, df.columns.get_loc("close")] = np.nan
    with pytest.raises(InvalidPriceFrame, match="NaN"):
        validate_price_frame(df)


def test_normalise_handles_provider_spellings():
    raw = pd.DataFrame(
        {
            "Date": ["2024-01-01", "2024-01-02"],
            "Open": [1.0, 2.0], "High": [2.0, 3.0],
            "Low": [0.5, 1.5], "Close": [1.5, 2.5],
            "Volume": [100, 200], "Adj Close": [1.5, 2.5],
        }
    )
    out = normalise_price_frame(raw)
    assert list(out.columns) == ["open", "high", "low", "close", "volume", "adj_close"]
    assert isinstance(out.index, pd.DatetimeIndex)
    assert out.index.name == "date"
    assert out["close"].dtype == np.float64


def test_normalise_keeps_the_later_of_duplicate_bars():
    """Providers re-send a partial bar before the final print; the later row
    is the settled one."""
    raw = pd.DataFrame(
        {
            "Date": ["2024-01-01", "2024-01-01"],
            "Open": [1.0, 1.0], "High": [2.0, 2.0], "Low": [0.5, 0.5],
            "Close": [1.5, 1.9], "Volume": [100, 350],
        }
    )
    out = normalise_price_frame(raw)
    assert len(out) == 1
    assert out.iloc[0]["close"] == 1.9
    assert out.iloc[0]["volume"] == 350
