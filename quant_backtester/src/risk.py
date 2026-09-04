"""Bar-level risk overlay: stops, targets and trailing exits.

The strategies in `strategies.py` are *naked* — they express a view and hold
it until the view changes. That is the right way to test whether an edge
exists, because a stop-loss can rescue a signal that has no edge at all and
make it look tradable.

But the reverse is also true: a real edge can be untradable naked, because a
single unbounded loss inside a position ruins the drawdown profile even when
the signal is right on average. This module is the bridge. It takes a
position series a strategy has already decided on and imposes a risk contract
on top of it:

    * a stop at `stop_atr` ATRs against the entry
    * an optional target at `target_atr` ATRs in favour
    * an optional trailing stop that ratchets but never loosens

Two properties matter more than the features:

**No look-ahead.** The stop distance is set from the ATR *as of the bar
before the position was opened*, and is never recomputed from information
inside the bar it protects. The trailing stop ratchets on completed bars only.

**Honest exit prices.** When a stop is hit intrabar, the return booked for
that bar is the return *to the stop level*, not the full bar return. Skipping
this is the single most common way a backtest with stops flatters itself: it
takes the exit but keeps marking the position at a close it never traded at.

Gaps are handled the way the market handles them — if the bar opens through
the stop, the fill is the open, not the stop. A stop is not a guarantee of
price, and a backtest that pretends otherwise understates tail risk exactly
where it matters most.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .strategies import atr as compute_atr

__all__ = ["RiskOverlay", "OverlayResult"]


@dataclass(frozen=True)
class OverlayResult:
    """Positions after risk management, plus the returns actually realized."""

    #: Exposure held during each bar, after stops/targets have cut positions.
    position: pd.Series
    #: Per-bar return of the position, with stop-out bars marked to the exit
    #: price rather than the close.
    gross_returns: pd.Series
    #: Which bars ended in a forced exit, and why.
    exit_reason: pd.Series

    @property
    def num_stops(self) -> int:
        return int((self.exit_reason == "stop").sum())

    @property
    def num_targets(self) -> int:
        return int((self.exit_reason == "target").sum())


class RiskOverlay:
    """Impose stops and targets on a strategy's positions.

    Distances are in ATR multiples rather than percentages so the same
    configuration means the same thing on a 2%-a-day crypto pair and on a
    0.4%-a-day utility ETF. A fixed 3% stop is a scalp on one and noise on
    the other.
    """

    def __init__(
        self,
        stop_atr: float = 2.0,
        target_atr: float | None = None,
        trailing: bool = False,
        atr_period: int = 14,
    ) -> None:
        if stop_atr <= 0:
            raise ValueError("stop_atr must be positive")
        if target_atr is not None and target_atr <= 0:
            raise ValueError("target_atr must be positive when set")
        self.stop_atr = float(stop_atr)
        self.target_atr = float(target_atr) if target_atr is not None else None
        self.trailing = bool(trailing)
        self.atr_period = int(atr_period)

    def describe(self) -> str:
        parts = [f"stop {self.stop_atr}x ATR{self.atr_period}"]
        if self.target_atr:
            parts.append(f"target {self.target_atr}x")
        if self.trailing:
            parts.append("trailing")
        return ", ".join(parts)

    def apply(self, data: pd.DataFrame, position: pd.Series) -> OverlayResult:
        """Apply the risk contract to an already-shifted position series.

        `position` must be the exposure *held during* each bar — that is, the
        strategy's signal already lagged by one bar. The overlay never undoes
        that lag; it only cuts positions short.
        """
        required = {"Open", "High", "Low", "Close"}
        missing = required - set(data.columns)
        if missing:
            raise ValueError(f"risk overlay needs OHLC columns; missing {sorted(missing)}")

        position = position.reindex(data.index).fillna(0.0)

        # The ATR that sizes a stop must be knowable before the bar it
        # protects, so it is lagged like every other decision input.
        atr = compute_atr(
            data["High"], data["Low"], data["Close"], period=self.atr_period
        ).shift(1)

        open_ = data["Open"].to_numpy(dtype=float)
        high = data["High"].to_numpy(dtype=float)
        low = data["Low"].to_numpy(dtype=float)
        close = data["Close"].to_numpy(dtype=float)
        prev_close = np.concatenate(([np.nan], close[:-1]))
        atr_values = atr.to_numpy(dtype=float)
        desired = position.to_numpy(dtype=float)

        n = len(desired)
        out_position = np.zeros(n)
        out_returns = np.zeros(n)
        reasons = np.full(n, "", dtype=object)

        active = 0.0  # exposure currently open
        entry_price = np.nan
        stop_level = np.nan
        target_level = np.nan
        # After a forced exit we refuse to re-enter on the *same* standing
        # signal — otherwise the overlay would buy straight back into the
        # position it just stopped out of, every bar, until the signal moved.
        blocked_signal = 0.0

        for i in range(n):
            want = desired[i]
            reference = prev_close[i]

            if want != blocked_signal:
                blocked_signal = 0.0

            # --- open a new position ------------------------------------
            if active == 0.0 and want != 0.0 and want != blocked_signal:
                if np.isfinite(reference) and np.isfinite(atr_values[i]) and atr_values[i] > 0:
                    active = want
                    entry_price = reference
                    distance = self.stop_atr * atr_values[i]
                    stop_level = entry_price - np.sign(active) * distance
                    target_level = (
                        entry_price + np.sign(active) * self.target_atr * atr_values[i]
                        if self.target_atr
                        else np.nan
                    )

            if active == 0.0:
                out_position[i] = 0.0
                out_returns[i] = 0.0
                continue

            # --- did this bar take us out? -------------------------------
            long = active > 0
            hit_stop = low[i] <= stop_level if long else high[i] >= stop_level
            hit_target = (
                (high[i] >= target_level if long else low[i] <= target_level)
                if np.isfinite(target_level)
                else False
            )

            exit_price = None
            if hit_stop:
                # A gap through the stop fills at the open. Pretending
                # otherwise is how backtests hide their worst days.
                gapped = open_[i] <= stop_level if long else open_[i] >= stop_level
                exit_price = open_[i] if gapped else stop_level
                reasons[i] = "stop"
            elif hit_target:
                gapped = open_[i] >= target_level if long else open_[i] <= target_level
                exit_price = open_[i] if gapped else target_level
                reasons[i] = "target"

            out_position[i] = active

            if exit_price is not None:
                out_returns[i] = active * (exit_price / reference - 1.0)
                active = 0.0
                blocked_signal = want
                entry_price = stop_level = target_level = np.nan
                continue

            out_returns[i] = active * (close[i] / reference - 1.0)

            # --- ratchet the trailing stop on the completed bar ----------
            if self.trailing and np.isfinite(atr_values[i]) and atr_values[i] > 0:
                distance = self.stop_atr * atr_values[i]
                candidate = close[i] - np.sign(active) * distance
                # Only ever tightens. A trailing stop that can loosen is not
                # a stop, it is a hope.
                stop_level = max(stop_level, candidate) if long else min(stop_level, candidate)

            # --- the strategy itself may want out ------------------------
            if want == 0.0 or np.sign(want) != np.sign(active):
                active = 0.0
                entry_price = stop_level = target_level = np.nan

        index = data.index
        return OverlayResult(
            position=pd.Series(out_position, index=index, name="position"),
            gross_returns=pd.Series(out_returns, index=index, name="gross_returns"),
            exit_reason=pd.Series(reasons, index=index, name="exit_reason"),
        )

    @classmethod
    def from_config(cls, config: dict | None) -> "RiskOverlay | None":
        """Build from config, or None when risk management is switched off."""
        if not config or not config.get("enabled", False):
            return None
        return cls(
            stop_atr=float(config.get("stop_atr", 2.0)),
            target_atr=(
                float(config["target_atr"]) if config.get("target_atr") is not None else None
            ),
            trailing=bool(config.get("trailing", False)),
            atr_period=int(config.get("atr_period", 14)),
        )
