"""Layer 1 - indicator maths, the BaseStrategy contract, and the strategy library."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np
import pandas as pd


# ==========================================================================
# TECHNICAL INDICATORS
# ==========================================================================
# Vectorized technical indicators shared by every strategy.
#
# Kept separate from `strategies.py` (Single Responsibility): strategies decide
# *when* to trade, this module only computes *numbers* from price series. No
# indicator here looks at any row beyond the one it's computed for — each is a
# strict function of the current row and prior rows (rolling windows), so
# composing them inside a strategy can't introduce look-ahead on its own.


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI, fully vectorized."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi_values = 100.0 - (100.0 / (1.0 + rs))
    # RSI is defined as 100 when avg_loss is 0 and avg_gain > 0.
    rsi_values = rsi_values.where(avg_loss != 0.0, 100.0)
    return rsi_values


def bollinger_bands(
    series: pd.Series, period: int, num_std: float
) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(series, period)
    std = series.rolling(window=period, min_periods=period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def keltner_channel(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    ema_period: int,
    atr_period: int,
    multiplier: float,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = ema(close, ema_period)
    band_width = multiplier * atr(high, low, close, atr_period)
    upper = mid + band_width
    lower = mid - band_width
    return upper, mid, lower


def donchian_channel(
    high: pd.Series, low: pd.Series, period: int
) -> tuple[pd.Series, pd.Series]:
    """Rolling N-day high/low, evaluated on data strictly *prior* to today
    (shifted by 1) so a breakout is measured against the channel as it stood
    at yesterday's close — not one that includes today's own high/low."""
    upper = high.rolling(window=period, min_periods=period).max().shift(1)
    lower = low.rolling(window=period, min_periods=period).min().shift(1)
    return upper, lower



# -- momentum & oscillators -------------------------------------------------


def roc(series: pd.Series, period: int) -> pd.Series:
    """Rate of change: fractional return over `period` bars."""
    return series.pct_change(period)


def zscore(series: pd.Series, period: int) -> pd.Series:
    """How many rolling standard deviations price sits from its own mean.

    Scale-free, so one threshold behaves the same on a 50 stock and a 5,000
    index — unlike a raw price distance.
    """
    mean = series.rolling(period, min_periods=period).mean()
    std = series.rolling(period, min_periods=period).std(ddof=0)
    return (series - mean) / std.replace(0.0, np.nan)


def williams_r(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Williams %R, bounded [-100, 0]. -100 is the period low."""
    highest = high.rolling(period, min_periods=period).max()
    lowest = low.rolling(period, min_periods=period).min()
    span = (highest - lowest).replace(0.0, np.nan)
    return -100.0 * (highest - close) / span


def cci(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Commodity Channel Index over the typical price."""
    typical = (high + low + close) / 3.0
    mean = typical.rolling(period, min_periods=period).mean()
    # CCI uses mean absolute deviation, not standard deviation.
    mad = typical.rolling(period, min_periods=period).apply(
        lambda w: np.abs(w - w.mean()).mean(), raw=True
    )
    return (typical - mean) / (0.015 * mad.replace(0.0, np.nan))


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line, histogram."""
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return macd_line, signal_line, macd_line - signal_line


# -- trend strength & volatility --------------------------------------------


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Average Directional Index — trend STRENGTH, not direction.

    Used as a gate: a crossover signal in a directionless market is noise, so
    strategies pair this with a directional indicator rather than trading it.
    """
    up = high.diff()
    down = -low.diff()
    # A directional move counts only when it exceeds the opposite side's move.
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)

    atr_values = atr(high, low, close, period).replace(0.0, np.nan)
    alpha = 1.0 / period
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr_values
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr_values

    denominator = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denominator
    return dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()


def directional_index(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int
) -> tuple[pd.Series, pd.Series]:
    """+DI and -DI, the directional halves that ADX summarises."""
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    atr_values = atr(high, low, close, period).replace(0.0, np.nan)
    alpha = 1.0 / period
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr_values
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr_values
    return plus_di, minus_di


def realized_volatility(series: pd.Series, period: int) -> pd.Series:
    """Annualized rolling volatility of returns."""
    return series.pct_change().rolling(period, min_periods=period).std(ddof=0) * np.sqrt(252)


def bollinger_bandwidth(series: pd.Series, period: int, num_std: float) -> pd.Series:
    """Band width as a fraction of the middle band.

    A contracting value is the classic "squeeze": volatility compressing
    before an expansion.
    """
    upper, mid, lower = bollinger_bands(series, period, num_std)
    return (upper - lower) / mid.replace(0.0, np.nan)


def supertrend(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int, multiplier: float
) -> pd.Series:
    """Supertrend direction: +1 uptrend, -1 downtrend.

    The band ratchets — it only ever tightens toward price while the trend
    holds — so the state is inherently sequential. The per-bar work is O(1)
    and the ATR feeding it is computed vectorized up front.
    """
    hl2 = (high + low) / 2.0
    band = multiplier * atr(high, low, close, period)
    upper_basic = (hl2 + band).to_numpy()
    lower_basic = (hl2 - band).to_numpy()
    closes = close.to_numpy()

    n = len(close)
    direction = np.ones(n, dtype=int)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)

    for i in range(1, n):
        if np.isnan(upper_basic[i]):
            direction[i] = direction[i - 1]
            continue
        upper[i] = (
            min(upper_basic[i], upper[i - 1])
            if not np.isnan(upper[i - 1]) and closes[i - 1] <= upper[i - 1]
            else upper_basic[i]
        )
        lower[i] = (
            max(lower_basic[i], lower[i - 1])
            if not np.isnan(lower[i - 1]) and closes[i - 1] >= lower[i - 1]
            else lower_basic[i]
        )
        if closes[i] > upper[i]:
            direction[i] = 1
        elif closes[i] < lower[i]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]

    return pd.Series(direction, index=close.index)


# -- price structure & candle patterns --------------------------------------


def swing_high(high: pd.Series, period: int) -> pd.Series:
    """Rolling highest high over the PRIOR `period` bars.

    Shifted by one so the current bar is never part of the level it is being
    compared against — otherwise every new high trivially "breaks" a level
    that includes itself.
    """
    return high.rolling(period, min_periods=period).max().shift(1)


def swing_low(low: pd.Series, period: int) -> pd.Series:
    return low.rolling(period, min_periods=period).min().shift(1)


def is_inside_bar(high: pd.Series, low: pd.Series) -> pd.Series:
    """True when a bar's range sits entirely inside its predecessor's."""
    return (high < high.shift(1)) & (low > low.shift(1))


def is_bullish_engulfing(
    open_: pd.Series, close: pd.Series
) -> pd.Series:
    """A down candle followed by an up candle that engulfs its body."""
    prior_down = close.shift(1) < open_.shift(1)
    now_up = close > open_
    engulfs = (close >= open_.shift(1)) & (open_ <= close.shift(1))
    return prior_down & now_up & engulfs


def is_bearish_engulfing(open_: pd.Series, close: pd.Series) -> pd.Series:
    prior_up = close.shift(1) > open_.shift(1)
    now_down = close < open_
    engulfs = (close <= open_.shift(1)) & (open_ >= close.shift(1))
    return prior_up & now_down & engulfs


# ==========================================================================
# STRATEGY BASE CLASS AND IMPLEMENTATIONS
# ==========================================================================
# Layer 1 strategy library: BaseStrategy + the six "naked" reference strategies.
#
# Every strategy returns a `pd.Series` of target positions indexed like the
# input data, valued in {-1, 0, 1}. Signals are computed strictly from data at
# or before the current row's timestamp — no strategy here is allowed to see
# its own future. Whether/when that signal is actually executed (e.g. at the
# next bar's open) is Layer 2's responsibility, not this layer's.
#
# Required OHLCV columns on the input DataFrame: Open, High, Low, Close, Volume.


REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume")


class StrategyValidationError(ValueError):
    """Raised when a strategy is constructed with invalid parameters."""


class BaseStrategy(ABC):
    """Abstract base class defining the strategy lifecycle contract.

    Subclasses implement `_generate_signals_impl`; this base class handles
    input validation and parameter bookkeeping so every strategy is
    trivially usable in a parameter sweep (`param_key()` gives a stable,
    filesystem-safe identity for a given parameter combination).
    """

    #: Which approach this strategy belongs to. Declared here rather than in
    #: a separate lookup so a new strategy cannot be added without saying what
    #: kind of thing it is.
    family: str = "unclassified"

    def __init__(self, **params: Any) -> None:
        self.params: dict[str, Any] = params
        self.validate_params()

    def validate_params(self) -> None:
        """Override in subclasses to reject nonsensical parameter combos."""

    @property
    def name(self) -> str:
        return type(self).__name__

    def param_key(self) -> str:
        return "_".join(f"{k}={v}" for k, v in sorted(self.params.items()))

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        """Public entry point: validates input, delegates to the strategy's
        own signal logic, and guarantees the contract (Series of -1/0/1,
        same index as `data`, no forward-looking NaN-fill)."""
        self._validate_input(data)
        signals = self._generate_signals_impl(data)
        signals = signals.reindex(data.index).fillna(0).astype(int)
        if not signals.isin((-1, 0, 1)).all():
            raise StrategyValidationError(
                f"{self.name} produced signal values outside {{-1, 0, 1}}"
            )
        return signals.rename("signal")

    @abstractmethod
    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        """Strategy-specific signal logic. Implement in subclasses."""

    @staticmethod
    def _validate_input(data: pd.DataFrame) -> None:
        missing = [c for c in REQUIRED_COLUMNS if c not in data.columns]
        if missing:
            raise StrategyValidationError(f"data missing required columns: {missing}")
        if not isinstance(data.index, pd.DatetimeIndex):
            raise StrategyValidationError("data must be indexed by a DatetimeIndex")

    @staticmethod
    def _states_from_entries(long_entry: pd.Series, short_entry: pd.Series) -> pd.Series:
        """Turn two boolean entry-event series into a stateful position
        series: entering long sets state to 1 and holds until a short
        entry flips it to -1, and vice versa. This is the "always flips,
        naked" position convention shared by every reversion strategy here.
        """
        raw = pd.Series(np.nan, index=long_entry.index)
        raw[long_entry] = 1
        raw[short_entry] = -1
        return raw.ffill().fillna(0)


# --------------------------------------------------------------------------
# A. Mean reversion family
# --------------------------------------------------------------------------


class RSIReversion(BaseStrategy):
    family = "mean_reversion"
    """RSI Snapback: long when RSI crosses below the oversold threshold,
    flips short when RSI crosses above the overbought threshold."""

    def __init__(
        self,
        rsi_period: int = 14,
        oversold_threshold: float = 30,
        overbought_threshold: float = 70,
    ) -> None:
        super().__init__(
            rsi_period=rsi_period,
            oversold_threshold=oversold_threshold,
            overbought_threshold=overbought_threshold,
        )

    def validate_params(self) -> None:
        if self.params["rsi_period"] < 2:
            raise StrategyValidationError("rsi_period must be >= 2")
        if not (0 < self.params["oversold_threshold"] < self.params["overbought_threshold"] < 100):
            raise StrategyValidationError(
                "require 0 < oversold_threshold < overbought_threshold < 100"
            )

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        rsi_values = rsi(data["Close"], self.params["rsi_period"])
        oversold = self.params["oversold_threshold"]
        overbought = self.params["overbought_threshold"]

        crossed_below_oversold = (rsi_values < oversold) & (rsi_values.shift(1) >= oversold)
        crossed_above_overbought = (rsi_values > overbought) & (
            rsi_values.shift(1) <= overbought
        )
        return self._states_from_entries(crossed_below_oversold, crossed_above_overbought)


class BBReversion(BaseStrategy):
    family = "mean_reversion"
    """Bollinger Band Reversion: long when price closes below the lower
    band, flips short when price closes above the upper band."""

    def __init__(self, period: int = 20, num_std: float = 2.0) -> None:
        super().__init__(period=period, num_std=num_std)

    def validate_params(self) -> None:
        if self.params["period"] < 2:
            raise StrategyValidationError("period must be >= 2")
        if self.params["num_std"] <= 0:
            raise StrategyValidationError("num_std must be > 0")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        upper, _, lower = bollinger_bands(
            data["Close"], self.params["period"], self.params["num_std"]
        )
        long_entry = data["Close"] < lower
        short_entry = data["Close"] > upper
        return self._states_from_entries(long_entry, short_entry)


class KeltnerReversion(BaseStrategy):
    family = "mean_reversion"
    """Keltner Channel Reversion: long when price stretches below the lower
    envelope, flips short when it stretches above the upper envelope."""

    def __init__(
        self, ema_period: int = 20, atr_period: int = 10, multiplier: float = 1.5
    ) -> None:
        super().__init__(ema_period=ema_period, atr_period=atr_period, multiplier=multiplier)

    def validate_params(self) -> None:
        if self.params["ema_period"] < 2 or self.params["atr_period"] < 2:
            raise StrategyValidationError("ema_period and atr_period must be >= 2")
        if self.params["multiplier"] <= 0:
            raise StrategyValidationError("multiplier must be > 0")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        upper, _, lower = keltner_channel(
            data["High"],
            data["Low"],
            data["Close"],
            self.params["ema_period"],
            self.params["atr_period"],
            self.params["multiplier"],
        )
        long_entry = data["Close"] < lower
        short_entry = data["Close"] > upper
        return self._states_from_entries(long_entry, short_entry)


# --------------------------------------------------------------------------
# B. Trend & momentum family
# --------------------------------------------------------------------------


class MACrossover(BaseStrategy):
    family = "trend"
    """Moving Average Crossover: long while the fast MA is above the slow
    MA (golden cross), short while below (death cross)."""

    def __init__(self, fast_period: int = 50, slow_period: int = 200) -> None:
        super().__init__(fast_period=fast_period, slow_period=slow_period)

    def validate_params(self) -> None:
        if self.params["fast_period"] < 2:
            raise StrategyValidationError("fast_period must be >= 2")
        if self.params["slow_period"] <= self.params["fast_period"]:
            raise StrategyValidationError("slow_period must be > fast_period")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        fast_ma = sma(data["Close"], self.params["fast_period"])
        slow_ma = sma(data["Close"], self.params["slow_period"])
        diff = fast_ma - slow_ma
        signal = pd.Series(np.sign(diff), index=data.index)
        # np.sign(0) == 0, and NaN during warm-up both collapse to flat (0)
        # via the base class's fillna(0), which is the correct "no opinion
        # yet" state.
        return signal


class TurtleBreakout(BaseStrategy):
    family = "breakout"
    """Donchian Breakout / Turtle System: enters long on an N-day high
    breakout, short on an N-day low breakdown, and exits to flat when price
    breaks the (typically shorter) M-day channel in the opposite direction.

    Unlike the other strategies above, entry and exit here are governed by
    *different* lookback windows, so the resulting position is not a closed-
    form function of the current bar alone — it depends on which regime
    (long/short/flat) the previous bar was already in. That is inherent
    path dependency in the classic Turtle rules, not an implementation
    shortcut, so this is the one strategy in the library that advances
    state with an explicit sequential pass rather than a single vectorized
    expression. The per-bar work inside the loop is O(1) and every
    indicator feeding it (the two Donchian channels) is still computed
    vectorized up front.
    """

    def __init__(self, entry_period: int = 20, exit_period: int = 10) -> None:
        super().__init__(entry_period=entry_period, exit_period=exit_period)

    def validate_params(self) -> None:
        if self.params["entry_period"] < 2 or self.params["exit_period"] < 2:
            raise StrategyValidationError("entry_period and exit_period must be >= 2")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        entry_upper, entry_lower = donchian_channel(
            data["High"], data["Low"], self.params["entry_period"]
        )
        exit_upper, exit_lower = donchian_channel(
            data["High"], data["Low"], self.params["exit_period"]
        )
        close = data["Close"]

        long_entry = (close > entry_upper).to_numpy()
        short_entry = (close < entry_lower).to_numpy()
        long_exit = (close < exit_lower).to_numpy()
        short_exit = (close > exit_upper).to_numpy()

        n = len(data)
        state = np.zeros(n, dtype=int)
        current = 0
        for i in range(n):
            if current == 0:
                if long_entry[i]:
                    current = 1
                elif short_entry[i]:
                    current = -1
            elif current == 1:
                if long_exit[i]:
                    current = -1 if short_entry[i] else 0
            elif current == -1:
                if short_exit[i]:
                    current = 1 if long_entry[i] else 0
            state[i] = current

        return pd.Series(state, index=data.index)


class SimpleMomentum(BaseStrategy):
    family = "momentum"
    """Single-Asset (Time-Series) Momentum: long if price is above its
    level `lookback_period` days ago, short if below."""

    def __init__(self, lookback_period: int = 126) -> None:
        super().__init__(lookback_period=lookback_period)

    def validate_params(self) -> None:
        if self.params["lookback_period"] < 1:
            raise StrategyValidationError("lookback_period must be >= 1")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        close = data["Close"]
        past_close = close.shift(self.params["lookback_period"])
        diff = close - past_close
        return pd.Series(np.sign(diff), index=data.index)




# --------------------------------------------------------------------------
# C. Mean reversion — additional
# --------------------------------------------------------------------------


class ZScoreReversion(BaseStrategy):
    family = "mean_reversion"
    """Fade statistically extreme deviations from a rolling mean.

    Scale-free by construction: the same threshold means the same thing on a
    stock trading at 50 and an index at 20,000, which a raw price distance
    cannot do.
    """

    def __init__(self, period: int = 20, entry_z: float = 2.0) -> None:
        super().__init__(period=period, entry_z=entry_z)

    def validate_params(self) -> None:
        if self.params["period"] < 2:
            raise StrategyValidationError("period must be >= 2")
        if self.params["entry_z"] <= 0:
            raise StrategyValidationError("entry_z must be > 0")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        z = zscore(data["Close"], self.params["period"])
        threshold = self.params["entry_z"]
        return self._states_from_entries(z < -threshold, z > threshold)


class WilliamsRReversion(BaseStrategy):
    family = "mean_reversion"
    """Williams %R oversold/overbought reversion."""

    def __init__(self, period: int = 14, oversold: float = -80, overbought: float = -20) -> None:
        super().__init__(period=period, oversold=oversold, overbought=overbought)

    def validate_params(self) -> None:
        if self.params["period"] < 2:
            raise StrategyValidationError("period must be >= 2")
        if not (-100 <= self.params["oversold"] < self.params["overbought"] <= 0):
            raise StrategyValidationError("require -100 <= oversold < overbought <= 0")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        wr = williams_r(data["High"], data["Low"], data["Close"], self.params["period"])
        return self._states_from_entries(
            wr < self.params["oversold"], wr > self.params["overbought"]
        )


class CCIReversion(BaseStrategy):
    family = "mean_reversion"
    """Fade CCI extremes away from the typical price."""

    def __init__(self, period: int = 20, threshold: float = 100.0) -> None:
        super().__init__(period=period, threshold=threshold)

    def validate_params(self) -> None:
        if self.params["period"] < 2:
            raise StrategyValidationError("period must be >= 2")
        if self.params["threshold"] <= 0:
            raise StrategyValidationError("threshold must be > 0")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        values = cci(data["High"], data["Low"], data["Close"], self.params["period"])
        threshold = self.params["threshold"]
        return self._states_from_entries(values < -threshold, values > threshold)


# --------------------------------------------------------------------------
# D. Trend following — additional
# --------------------------------------------------------------------------


class MACDTrend(BaseStrategy):
    family = "trend"
    """Long while the MACD line leads its signal line, short while it lags."""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        super().__init__(fast=fast, slow=slow, signal=signal)

    def validate_params(self) -> None:
        if self.params["fast"] < 2 or self.params["signal"] < 2:
            raise StrategyValidationError("fast and signal must be >= 2")
        if self.params["slow"] <= self.params["fast"]:
            raise StrategyValidationError("slow must be > fast")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        line, signal_line, _ = macd(
            data["Close"], self.params["fast"], self.params["slow"], self.params["signal"]
        )
        return pd.Series(np.sign(line - signal_line), index=data.index)


class SupertrendFollow(BaseStrategy):
    family = "trend"
    """Follow the Supertrend band's direction."""

    def __init__(self, atr_period: int = 10, multiplier: float = 3.0) -> None:
        super().__init__(atr_period=atr_period, multiplier=multiplier)

    def validate_params(self) -> None:
        if self.params["atr_period"] < 2:
            raise StrategyValidationError("atr_period must be >= 2")
        if self.params["multiplier"] <= 0:
            raise StrategyValidationError("multiplier must be > 0")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        return supertrend(
            data["High"], data["Low"], data["Close"],
            self.params["atr_period"], self.params["multiplier"],
        )


class ADXTrend(BaseStrategy):
    family = "trend"
    """Trade direction only when the trend is strong enough to be worth it.

    ADX measures strength, not direction, so it gates rather than signals: a
    +DI/-DI crossover in a directionless market is noise, and standing aside
    is the point.
    """

    def __init__(self, period: int = 14, adx_threshold: float = 25.0) -> None:
        super().__init__(period=period, adx_threshold=adx_threshold)

    def validate_params(self) -> None:
        if self.params["period"] < 2:
            raise StrategyValidationError("period must be >= 2")
        if not 0 < self.params["adx_threshold"] < 100:
            raise StrategyValidationError("adx_threshold must be between 0 and 100")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        high, low, close = data["High"], data["Low"], data["Close"]
        strength = adx(high, low, close, self.params["period"])
        plus_di, minus_di = directional_index(high, low, close, self.params["period"])

        trending = strength > self.params["adx_threshold"]
        direction = np.sign(plus_di - minus_di)
        return pd.Series(np.where(trending, direction, 0.0), index=data.index)


# --------------------------------------------------------------------------
# E. Momentum — additional
# --------------------------------------------------------------------------


class ROCMomentum(BaseStrategy):
    family = "momentum"
    """Long when trailing return clears a threshold, short when it falls below.

    The dead band matters: requiring a minimum move avoids flipping on noise
    around zero, which a bare sign test does constantly.
    """

    def __init__(self, period: int = 63, threshold: float = 0.02) -> None:
        super().__init__(period=period, threshold=threshold)

    def validate_params(self) -> None:
        if self.params["period"] < 1:
            raise StrategyValidationError("period must be >= 1")
        if self.params["threshold"] < 0:
            raise StrategyValidationError("threshold must be >= 0")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        values = roc(data["Close"], self.params["period"])
        threshold = self.params["threshold"]
        return self._states_from_entries(values > threshold, values < -threshold)


class DualMomentum(BaseStrategy):
    family = "momentum"
    """Require agreement between a short and a long lookback.

    A single lookback flips on any wobble at its own horizon; demanding two
    horizons agree trades less and holds longer.
    """

    def __init__(self, short_period: int = 21, long_period: int = 126) -> None:
        super().__init__(short_period=short_period, long_period=long_period)

    def validate_params(self) -> None:
        if self.params["short_period"] < 1:
            raise StrategyValidationError("short_period must be >= 1")
        if self.params["long_period"] <= self.params["short_period"]:
            raise StrategyValidationError("long_period must be > short_period")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        close = data["Close"]
        short = roc(close, self.params["short_period"])
        long = roc(close, self.params["long_period"])
        return pd.Series(
            np.where((short > 0) & (long > 0), 1.0,
                     np.where((short < 0) & (long < 0), -1.0, 0.0)),
            index=data.index,
        )


# --------------------------------------------------------------------------
# F. Breakout — additional
# --------------------------------------------------------------------------


class VolatilityBreakout(BaseStrategy):
    family = "breakout"
    """Enter when price travels more than N x ATR from the prior close.

    Sizing the trigger in ATR rather than points makes it self-adjusting: the
    same setting is equally selective in calm and violent markets.
    """

    def __init__(self, atr_period: int = 14, multiplier: float = 1.5) -> None:
        super().__init__(atr_period=atr_period, multiplier=multiplier)

    def validate_params(self) -> None:
        if self.params["atr_period"] < 2:
            raise StrategyValidationError("atr_period must be >= 2")
        if self.params["multiplier"] <= 0:
            raise StrategyValidationError("multiplier must be > 0")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        close = data["Close"]
        prior_close = close.shift(1)
        band = self.params["multiplier"] * atr(
            data["High"], data["Low"], close, self.params["atr_period"]
        ).shift(1)
        return self._states_from_entries(
            close > prior_close + band, close < prior_close - band
        )


class SqueezeBreakout(BaseStrategy):
    family = "breakout"
    """Trade the expansion that follows a volatility squeeze.

    Bollinger bandwidth contracting into the lowest part of its own recent
    range marks coiled volatility; the trade is the direction of the break
    that resolves it.
    """

    def __init__(self, period: int = 20, num_std: float = 2.0, squeeze_lookback: int = 120,
                 squeeze_quantile: float = 0.25) -> None:
        super().__init__(period=period, num_std=num_std,
                         squeeze_lookback=squeeze_lookback, squeeze_quantile=squeeze_quantile)

    def validate_params(self) -> None:
        if self.params["period"] < 2:
            raise StrategyValidationError("period must be >= 2")
        if self.params["squeeze_lookback"] <= self.params["period"]:
            raise StrategyValidationError("squeeze_lookback must exceed period")
        if not 0 < self.params["squeeze_quantile"] < 1:
            raise StrategyValidationError("squeeze_quantile must be between 0 and 1")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        close = data["Close"]
        width = bollinger_bandwidth(close, self.params["period"], self.params["num_std"])
        # The squeeze test uses only prior bars, so today's own width cannot
        # decide whether today was a squeeze.
        floor = width.rolling(self.params["squeeze_lookback"], min_periods=self.params["period"]).quantile(
            self.params["squeeze_quantile"]
        ).shift(1)
        squeezed = width.shift(1) <= floor

        upper, _, lower = bollinger_bands(close, self.params["period"], self.params["num_std"])
        return self._states_from_entries(
            squeezed & (close > upper), squeezed & (close < lower)
        )


# --------------------------------------------------------------------------
# G. Volatility
# --------------------------------------------------------------------------


class VolatilityRegime(BaseStrategy):
    family = "volatility"
    """Hold risk only while realized volatility is subdued.

    A risk-on/risk-off filter rather than a directional edge: it expresses the
    observation that calm markets tend to drift up and violent ones do not.
    """

    def __init__(self, vol_period: int = 20, calm_quantile: float = 0.5,
                 lookback: int = 252) -> None:
        super().__init__(vol_period=vol_period, calm_quantile=calm_quantile, lookback=lookback)

    def validate_params(self) -> None:
        if self.params["vol_period"] < 2:
            raise StrategyValidationError("vol_period must be >= 2")
        if not 0 < self.params["calm_quantile"] < 1:
            raise StrategyValidationError("calm_quantile must be between 0 and 1")
        if self.params["lookback"] <= self.params["vol_period"]:
            raise StrategyValidationError("lookback must exceed vol_period")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        vol = realized_volatility(data["Close"], self.params["vol_period"])
        # Compared against its own history to date, never the full sample.
        threshold = vol.rolling(
            self.params["lookback"], min_periods=self.params["vol_period"]
        ).quantile(self.params["calm_quantile"])
        return pd.Series(np.where(vol <= threshold, 1.0, 0.0), index=data.index)


class VolatilityMeanReversion(BaseStrategy):
    family = "volatility"
    """Buy after a volatility spike exhausts itself.

    Volatility is strongly mean-reverting; an extreme reading followed by a
    contraction has historically marked capitulation rather than trend.
    """

    def __init__(self, vol_period: int = 20, spike_quantile: float = 0.9,
                 lookback: int = 252) -> None:
        super().__init__(vol_period=vol_period, spike_quantile=spike_quantile, lookback=lookback)

    def validate_params(self) -> None:
        if self.params["vol_period"] < 2:
            raise StrategyValidationError("vol_period must be >= 2")
        if not 0 < self.params["spike_quantile"] < 1:
            raise StrategyValidationError("spike_quantile must be between 0 and 1")
        if self.params["lookback"] <= self.params["vol_period"]:
            raise StrategyValidationError("lookback must exceed vol_period")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        vol = realized_volatility(data["Close"], self.params["vol_period"])
        threshold = vol.rolling(
            self.params["lookback"], min_periods=self.params["vol_period"]
        ).quantile(self.params["spike_quantile"])
        spiked = vol.shift(1) > threshold.shift(1)
        cooling = vol < vol.shift(1)
        return self._states_from_entries(spiked & cooling, vol > threshold)


# --------------------------------------------------------------------------
# H. Chart patterns / price structure
# --------------------------------------------------------------------------


class StructureBreak(BaseStrategy):
    family = "pattern"
    """Trade market structure: higher highs versus lower lows.

    The most basic chart read there is — price making a new swing high is an
    uptrend, a new swing low a downtrend — expressed without any indicator.
    """

    def __init__(self, swing_period: int = 20) -> None:
        super().__init__(swing_period=swing_period)

    def validate_params(self) -> None:
        if self.params["swing_period"] < 2:
            raise StrategyValidationError("swing_period must be >= 2")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        period = self.params["swing_period"]
        prior_high = swing_high(data["High"], period)
        prior_low = swing_low(data["Low"], period)
        close = data["Close"]
        return self._states_from_entries(close > prior_high, close < prior_low)


class InsideBarBreakout(BaseStrategy):
    family = "pattern"
    """An inside bar marks compression; trade the break of its range.

    The inside bar must be YESTERDAY's, so the pattern is complete before the
    break is judged — testing both on the same bar would be circular.
    """

    def __init__(self, confirm_bars: int = 1) -> None:
        super().__init__(confirm_bars=confirm_bars)

    def validate_params(self) -> None:
        if self.params["confirm_bars"] < 1:
            raise StrategyValidationError("confirm_bars must be >= 1")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        inside = is_inside_bar(data["High"], data["Low"]).shift(1).astype("boolean").fillna(False).astype(bool)
        prior_high = data["High"].shift(1)
        prior_low = data["Low"].shift(1)
        close = data["Close"]
        return self._states_from_entries(
            inside & (close > prior_high), inside & (close < prior_low)
        )


class EngulfingReversal(BaseStrategy):
    family = "pattern"
    """Bullish/bearish engulfing candles, optionally filtered by trend.

    An engulfing candle against a prevailing trend is a reversal candidate; in
    the direction of the trend it is usually just continuation noise, so the
    filter is what makes the pattern tradable.
    """

    def __init__(self, trend_period: int = 50, require_trend: int = 1) -> None:
        super().__init__(trend_period=trend_period, require_trend=require_trend)

    def validate_params(self) -> None:
        if self.params["trend_period"] < 2:
            raise StrategyValidationError("trend_period must be >= 2")
        if self.params["require_trend"] not in (0, 1):
            raise StrategyValidationError("require_trend must be 0 or 1")

    def _generate_signals_impl(self, data: pd.DataFrame) -> pd.Series:
        open_, close = data["Open"], data["Close"]
        bullish = is_bullish_engulfing(open_, close)
        bearish = is_bearish_engulfing(open_, close)

        if self.params["require_trend"]:
            trend = sma(close, self.params["trend_period"])
            # Only fade an existing move: buy engulfing below trend, sell above.
            bullish = bullish & (close < trend)
            bearish = bearish & (close > trend)

        return self._states_from_entries(
            bullish.astype("boolean").fillna(False).astype(bool),
            bearish.astype("boolean").fillna(False).astype(bool),
        )


# ==========================================================================
# STRATEGY REGISTRY
# ==========================================================================
# Strategy name -> class registry.
#
# This is the one place code wires a strategy *name* (as used in
# config/strategy_grid.yaml) to its implementation class. Adding a new
# strategy means adding one line here and a new class in strategies.py — the
# batch runner and config never need to change (Open/Closed Principle).


STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    # Mean reversion
    "RSIReversion": RSIReversion,
    "BBReversion": BBReversion,
    "KeltnerReversion": KeltnerReversion,
    "ZScoreReversion": ZScoreReversion,
    "WilliamsRReversion": WilliamsRReversion,
    "CCIReversion": CCIReversion,
    # Trend following
    "MACrossover": MACrossover,
    "MACDTrend": MACDTrend,
    "SupertrendFollow": SupertrendFollow,
    "ADXTrend": ADXTrend,
    # Momentum
    "SimpleMomentum": SimpleMomentum,
    "ROCMomentum": ROCMomentum,
    "DualMomentum": DualMomentum,
    # Breakout
    "TurtleBreakout": TurtleBreakout,
    "VolatilityBreakout": VolatilityBreakout,
    "SqueezeBreakout": SqueezeBreakout,
    # Volatility
    "VolatilityRegime": VolatilityRegime,
    "VolatilityMeanReversion": VolatilityMeanReversion,
    # Chart patterns / structure
    "StructureBreak": StructureBreak,
    "InsideBarBreakout": InsideBarBreakout,
    "EngulfingReversal": EngulfingReversal,
}


def strategies_by_family() -> dict[str, list[str]]:
    """Registered strategy names grouped by approach."""
    grouped: dict[str, list[str]] = {}
    for name, cls in STRATEGY_REGISTRY.items():
        grouped.setdefault(cls.family, []).append(name)
    return grouped


def get_strategy_class(name: str) -> type[BaseStrategy]:
    try:
        return STRATEGY_REGISTRY[name]
    except KeyError as exc:
        known = ", ".join(sorted(STRATEGY_REGISTRY))
        raise KeyError(f"Unknown strategy '{name}'. Known strategies: {known}") from exc
