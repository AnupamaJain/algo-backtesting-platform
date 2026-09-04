"""Post-earnings announcement drift.

The claim, from Ball & Brown (1968) and Bernard & Thomas (1989): after a
company reports earnings that beat expectations, its price keeps drifting
upward for weeks. Investors under-react to the news and the adjustment is
gradual rather than instant.

Why this is worth testing when the indicator strategies were not: it has a
stated cause. A moving-average crossover works or does not for no reason
anyone can articulate, which is why sweeping its parameters finds nothing but
noise. Under-reaction to news is a claim about behaviour that predicts *where*
the effect should appear (right after announcements, strongest for the biggest
surprises) and where it should not (everywhere else). That makes it falsifiable
in a way a parameter sweep is not.

The signal here:

    on the first session strictly AFTER an announcement whose surprise
    exceeds `min_surprise_pct`, take a position in the direction of the
    surprise, and hold it for `holding_days` sessions

Position sizing, costs and validation are all the platform's existing
machinery — this module only decides direction and timing.

WHAT WOULD MAKE THIS FAKE. Three things, all guarded:

  * Trading the announcement session itself. Most US firms report after the
    close, so that session's return already contains the news. Entry is
    always the *next* session (see `events.first_tradeable_session`).
  * Using the surprise before it was published. The signal is switched on at
    the entry session and never before it.
  * Look-ahead through the holding window. Positions are set forward from
    entry only; nothing reads a future bar.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .events import EarningsEvent, first_tradeable_session

logger = logging.getLogger(__name__)

__all__ = ["pead_signals", "PEADConfig"]


class PEADConfig:
    """Parameters of the drift trade."""

    def __init__(
        self,
        min_surprise_pct: float = 5.0,
        holding_days: int = 20,
        direction: str = "both",
    ) -> None:
        if holding_days < 1:
            raise ValueError("holding_days must be at least 1")
        if direction not in ("both", "long_only", "short_only"):
            raise ValueError("direction must be both, long_only or short_only")
        self.min_surprise_pct = float(min_surprise_pct)
        self.holding_days = int(holding_days)
        self.direction = direction

    def describe(self) -> str:
        return (
            f"PEAD surprise>{self.min_surprise_pct}% hold {self.holding_days}d "
            f"({self.direction})"
        )

    @property
    def param_key(self) -> str:
        return (
            f"min_surprise_pct={self.min_surprise_pct}"
            f"_holding_days={self.holding_days}_direction={self.direction}"
        )


def pead_signals(
    prices: pd.DataFrame | pd.Series,
    events: list[EarningsEvent],
    config: PEADConfig,
) -> pd.Series:
    """Target position in {-1, 0, +1} for each session.

    Returned on the price index, so the existing backtester applies its usual
    one-bar lag on top. That lag is *additional* conservatism here: the entry
    session is already strictly after the announcement.
    """
    index = prices.index if isinstance(prices, pd.Series) else prices.index
    signals = pd.Series(0.0, index=index, name="signal")
    if not events or len(index) == 0:
        return signals

    sessions = pd.DatetimeIndex(index)

    for event in events:
        if not event.has_surprise:
            continue
        surprise = event.surprise_pct
        if abs(surprise) < config.min_surprise_pct:
            continue

        direction = 1.0 if surprise > 0 else -1.0
        if config.direction == "long_only" and direction < 0:
            continue
        if config.direction == "short_only" and direction > 0:
            continue

        entry = first_tradeable_session(event, sessions)
        if entry is None:
            continue

        start = sessions.get_loc(entry)
        stop = min(start + config.holding_days, len(sessions))
        # Overlapping windows from consecutive quarters simply overwrite:
        # the newer surprise is the better information.
        signals.iloc[start:stop] = direction

    return signals


def pead_portfolio_returns(
    price_data: dict,
    calendars: dict,
    config: PEADConfig,
    backtester,
) -> pd.Series:
    """Equal-weight portfolio of the drift trade across a universe.

    Capital is spread over the names currently holding a position, so the
    reported return is that of the book actually being run. Averaging over
    the whole universe instead — including every idle name — is the same
    strategy levered down by whatever fraction happens to be active, and it
    makes a real effect look like nothing.

    Averaging across names is also what makes this testable at all: one
    stock gives four announcements a year, far too few to say anything.
    """
    curves = []
    for symbol, data in price_data.items():
        events = calendars.get(symbol)
        if not events:
            continue
        signals = pead_signals(data, events, config)
        if (signals != 0).sum() == 0:
            continue
        try:
            result = backtester.run(data["Close"], signals)
        except Exception as exc:  # noqa: BLE001 - one symbol must not kill the run
            logger.debug("PEAD backtest failed for %s: %s", symbol, exc)
            continue
        curves.append(result.net_returns.rename(symbol))

    if not curves:
        return pd.Series(dtype=float)

    frame = pd.concat(curves, axis=1).fillna(0.0)

    # Spread capital across the names that HAVE a position, not across the
    # whole universe. Dividing by 87 when 7 names are active understates the
    # strategy by an order of magnitude — it reports the return of a book
    # that is 92% in cash while pretending to be fully invested.
    #
    # `active` counts the names carrying exposure each day. Days with no
    # position return zero, which is correct: the strategy is genuinely flat.
    active = (frame != 0).sum(axis=1)
    scaled = frame.sum(axis=1).div(active.where(active > 0)).fillna(0.0)
    return scaled


def exposure_stats(
    price_data: dict, calendars: dict, config: PEADConfig
) -> dict:
    """How much of the time this strategy is actually in the market.

    A drift trade is idle most of the year. Reporting that prevents a Sharpe
    computed over a mostly-flat series from being read as a full-time result.
    """
    total = active = trades = 0
    for symbol, data in price_data.items():
        events = calendars.get(symbol)
        if not events:
            continue
        signals = pead_signals(data, events, config)
        total += len(signals)
        active += int((signals != 0).sum())
        trades += int((signals.diff().fillna(signals) != 0).sum())
    return {
        "bars": total,
        "bars_in_market": active,
        "time_in_market_pct": round(100 * active / total, 2) if total else 0.0,
        "position_changes": trades,
    }
