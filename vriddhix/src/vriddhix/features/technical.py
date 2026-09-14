"""Technical features. Pure, vectorised, and strictly causal.

CAUSALITY IS THE POINT OF THIS MODULE. A value dated ``t`` may depend only on
bars dated ``<= t``. Every function here uses trailing windows (``rolling``,
``ewm``, positive ``shift``) and nothing else. The following are prohibited
and are checked for by tests/test_causality.py:

    .shift(-n)          pulls a future bar backwards
    center=True         a centred window straddles t
    .bfill()            fills a hole from later data
    df.max() / df.min() over the whole frame -- embeds the future in a bound
    z-scores over the full series -- the mean embeds the future

A violation here is the most expensive class of bug in the system: it does not
crash, it produces a backtest that looks excellent and means nothing.

See docs/04-data-model.md §4.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..domain.types import InvalidPriceFrame

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


# ---------------------------------------------------------------------------
# Frame contract
# ---------------------------------------------------------------------------


def validate_price_frame(df: pd.DataFrame, *, symbol: str = "?") -> None:
    """Enforce the price-frame contract, or raise.

    Raising rather than repairing is deliberate. A frame quietly patched into
    validity yields numbers that look right; an exception yields a fix.
    """
    if df is None or len(df) == 0:
        raise InvalidPriceFrame(f"{symbol}: empty price frame")

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise InvalidPriceFrame(f"{symbol}: missing columns {missing}")

    if not isinstance(df.index, pd.DatetimeIndex):
        raise InvalidPriceFrame(f"{symbol}: index must be a DatetimeIndex")

    if df.index.tz is not None:
        raise InvalidPriceFrame(
            f"{symbol}: index must be tz-naive "
            "(a tz-aware index silently compares unequal to plain dates)"
        )

    if not df.index.is_monotonic_increasing:
        raise InvalidPriceFrame(f"{symbol}: index must be sorted ascending")

    if df.index.has_duplicates:
        dupes = df.index[df.index.duplicated()].tolist()[:5]
        raise InvalidPriceFrame(f"{symbol}: duplicate timestamps, e.g. {dupes}")

    ohlc = df[["open", "high", "low", "close"]]
    if ohlc.isna().any().any():
        raise InvalidPriceFrame(
            f"{symbol}: NaN in OHLC "
            "(NaN propagates through every EMA without ever raising)"
        )

    if (ohlc <= 0).any().any():
        raise InvalidPriceFrame(f"{symbol}: non-positive price")

    bad_high = df["high"] < df[["open", "close"]].max(axis=1)
    bad_low = df["low"] > df[["open", "close"]].min(axis=1)
    if bad_high.any() or bad_low.any():
        first = df.index[bad_high | bad_low][0].date()
        raise InvalidPriceFrame(f"{symbol}: impossible OHLC relationship on {first}")


def normalise_price_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce a provider frame into the contract shape.

    Applied at the L1 boundary so that every layer above it can assume the
    contract holds rather than re-checking defensively.
    """
    out = df.copy()
    out.columns = [str(c).strip().lower().replace(" ", "_") for c in out.columns]

    if "adj_close" in out.columns and "close" not in out.columns:
        out["close"] = out["adj_close"]

    if not isinstance(out.index, pd.DatetimeIndex):
        date_col = next(
            (c for c in ("date", "datetime", "timestamp") if c in out.columns), None
        )
        if date_col is None:
            raise InvalidPriceFrame("no DatetimeIndex and no date column to build one")
        out = out.set_index(pd.to_datetime(out[date_col])).drop(columns=[date_col])

    if out.index.tz is not None:
        out.index = out.index.tz_localize(None)
    out.index = out.index.normalize()
    out.index.name = "date"

    keep = [c for c in (*REQUIRED_COLUMNS, "adj_close") if c in out.columns]
    out = out[keep]

    for col in keep:
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")

    # Duplicate timestamps: keep the last. Providers re-send a partial bar
    # before the final print, and the later row is the settled one.
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


# ---------------------------------------------------------------------------
# Primitives -- each one causal by construction
# ---------------------------------------------------------------------------


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average.

    ``adjust=False`` gives the recursive form, which depends only on the prior
    EMA and the current bar. ``adjust=True`` reweights the entire history as
    new bars arrive, so an EMA printed for a past date would *change* -- values
    already shown to a user, and already traded on in a backtest, would move.
    """
    if period <= 0:
        raise ValueError(f"ema period must be positive, got {period}")
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range, Wilder smoothing (alpha = 1/period).

    Wilder rather than a simple mean because it is what the original
    definition specifies and what every charting package draws; a simple
    rolling mean would put our ATR visibly out of step with the user's chart.
    """
    if period <= 0:
        raise ValueError(f"atr period must be positive, got {period}")
    return true_range(df).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def rolling_return(close: pd.Series, bars: int) -> pd.Series:
    """Return over ``bars`` trading bars -- bars, not calendar days.

    NSE holidays make calendar offsets uneven, so a "1 month" window defined in
    days would cover a different number of sessions depending on the month.
    """
    if bars <= 0:
        raise ValueError(f"return period must be positive, got {bars}")
    return close / close.shift(bars) - 1.0


def rolling_extreme(series: pd.Series, window: int, kind: str) -> pd.Series:
    """Trailing max/min. ``min_periods=1`` so early history yields the extreme
    of what exists rather than NaN -- a newly listed stock still has a high."""
    roll = series.rolling(window=window, min_periods=1)
    return roll.max() if kind == "max" else roll.min()


# ---------------------------------------------------------------------------
# Feature frame
# ---------------------------------------------------------------------------


def compute_features(
    df: pd.DataFrame,
    *,
    ema_periods: tuple[int, ...] = (20, 50, 100, 200),
    atr_period: int = 14,
    volume_avg_periods: tuple[int, ...] = (20, 50),
    return_periods: dict[str, int] | None = None,
    high_low_lookback: int = 252,
    symbol: str = "?",
    validate: bool = True,
) -> pd.DataFrame:
    """Full technical feature set, indexed identically to ``df``.

    Every column is causal at its own timestamp, which is what makes it legal
    to hand row ``t`` of this frame to an engine evaluating date ``t``.
    """
    if validate:
        validate_price_frame(df, symbol=symbol)

    return_periods = return_periods or {
        "ret_1m": 21,
        "ret_3m": 63,
        "ret_6m": 126,
        "ret_12m": 252,
    }

    close, volume = df["close"], df["volume"]
    out = pd.DataFrame(index=df.index.copy())

    for period in ema_periods:
        out[f"ema_{period}"] = ema(close, period)

    out[f"atr_{atr_period}"] = atr(df, atr_period)
    # Percent-of-price so that ATR is comparable across a Rs 80 stock and a
    # Rs 80,000 one. Absolute ATR is not.
    out["atr_pct"] = out[f"atr_{atr_period}"] / close * 100.0

    for period in volume_avg_periods:
        out[f"avg_volume_{period}"] = volume.rolling(period, min_periods=period).mean()

    ref = max(volume_avg_periods) if volume_avg_periods else 50
    ref_col = f"avg_volume_{ref}"
    with np.errstate(divide="ignore", invalid="ignore"):
        out["rel_volume"] = volume / out[ref_col].replace(0.0, np.nan)

    for name, bars in return_periods.items():
        out[name] = rolling_return(close, bars)

    out["high_52w"] = rolling_extreme(df["high"], high_low_lookback, "max")
    out["low_52w"] = rolling_extreme(df["low"], high_low_lookback, "min")
    # Negative below the high, zero at it. Sign carries meaning; abs() would
    # make "6% below the high" and "6% above" indistinguishable.
    out["pct_from_52w_high"] = (close - out["high_52w"]) / out["high_52w"] * 100.0
    out["pct_from_52w_low"] = (close - out["low_52w"]) / out["low_52w"] * 100.0

    for period in ema_periods:
        out[f"above_ema_{period}"] = close > out[f"ema_{period}"]

    return out


def compute_features_from_config(df: pd.DataFrame, cfg, *, symbol: str = "?") -> pd.DataFrame:
    """``compute_features`` with parameters taken from configuration."""
    features = cfg.section("features")
    return compute_features(
        df,
        ema_periods=tuple(features["ema_periods"]),
        atr_period=int(features["atr_period"]),
        volume_avg_periods=tuple(features["volume_avg_periods"]),
        return_periods=dict(features["return_periods"]),
        high_low_lookback=int(features["high_low_lookback"]),
        symbol=symbol,
    )
