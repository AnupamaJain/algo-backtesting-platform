"""Path-dependent backtest for Initial Balance retracement trades.

`intraday.py` answers structural questions — where the IB is, which side broke
first, how often price retraces. Those are *frequencies*, and a frequency is
not a P&L. "Price reaches the far side on 29% of sessions" says nothing about
whether the stop was hit on the way, and that ordering is the entire
difference between a strategy and a statistic.

This module walks each session bar by bar and answers the P&L question.

THE SAME-BAR PROBLEM, AGAIN. One bar can contain both the stop and the
target. OHLC does not record which came first, and this is the single most
consequential ambiguity in the whole study: resolving it toward the target
turns losing systems into winning ones on paper. The default here is
`stop_first` — take the loss — and the alternative is to exclude the trade
and report it, never to assume the favourable fill.

Related conservatism, all deliberate:

  * A fill is only ever taken from a bar strictly AFTER the break bar. The
    break bar may well have traded through the entry level, but its internal
    ordering is unknown, so it is not used.
  * Entries fill at the limit price, exits at the stop/target level, with
    explicit slippage charged on both — no assuming a better print.
  * A bar that gaps through the stop fills at that bar's open, not the stop.
  * Everything is flat by `exit_at`; nothing is carried overnight, because
    overnight gap risk was never measured here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd

from .intraday import (
    HIGH,
    LOW,
    InitialBalance,
    IntradayConfig,
    analyse_session,
    wilson_interval,
)

logger = logging.getLogger(__name__)

__all__ = ["IBTradeConfig", "Trade", "simulate_session", "summarize_trades"]


# ==========================================================================
# CONFIG
# ==========================================================================


@dataclass(frozen=True)
class IBTradeConfig:
    """How one IB retracement trade is defined and executed."""

    mode: str
    entry_level: float
    stop_buffer_frac: float
    target: str | float
    entry_cutoff: time
    exit_at: time
    same_bar_policy: str
    cancel_on_double_break: bool
    per_side_pct: float
    slippage_frac: float

    @classmethod
    def from_dict(cls, raw: dict) -> "IBTradeConfig":
        mode = str(raw.get("mode", "REVERSION")).upper()
        if mode not in ("REVERSION", "CONTINUATION"):
            raise ValueError(f"mode must be REVERSION or CONTINUATION, got {mode!r}")

        policy = str(raw.get("same_bar_policy", "stop_first")).lower()
        if policy not in ("stop_first", "ambiguous"):
            # Refusing "target_first" is a correctness decision, not a
            # missing feature: it would let the backtest choose its own
            # outcome on exactly the bars that matter most.
            raise ValueError(
                f"same_bar_policy must be 'stop_first' or 'ambiguous', got {policy!r}"
            )

        target = raw.get("target", "FAR_SIDE")
        if not (isinstance(target, str) and target.upper() == "FAR_SIDE"):
            target = float(target)

        costs = raw.get("costs", {}) or {}
        return cls(
            mode=mode,
            entry_level=float(raw.get("entry_level", 0.5)),
            stop_buffer_frac=float(raw.get("stop_buffer_frac", 0.08)),
            target=target if isinstance(target, float) else "FAR_SIDE",
            entry_cutoff=_parse_time(raw.get("entry_cutoff", "14:30")),
            exit_at=_parse_time(raw.get("exit_at", "15:20")),
            same_bar_policy=policy,
            cancel_on_double_break=bool(raw.get("cancel_on_double_break", True)),
            per_side_pct=float(costs.get("per_side_pct", 0.0)),
            slippage_frac=float(costs.get("slippage_frac", 0.0)),
        )


def _parse_time(value) -> time:
    if isinstance(value, time):
        return value
    hours, minutes = str(value).split(":")
    return time(int(hours), int(minutes))


def load_trade_config(path: str | Path) -> IBTradeConfig:
    import yaml

    with Path(path).open("r", encoding="utf-8") as fh:
        return IBTradeConfig.from_dict(yaml.safe_load(fh)["ib_trade"])


# ==========================================================================
# ONE TRADE
# ==========================================================================


@dataclass
class Trade:
    """The full record of one session's setup, filled or not."""

    session_date: date
    #: LONG or SHORT — the direction actually traded.
    direction: str | None = None
    broke_first: str | None = None
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    fill_time: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_time: pd.Timestamp | None = None
    #: TARGET, STOP, TIME, AMBIGUOUS, or a reason the trade never happened.
    outcome: str = "NO_SETUP"
    #: Net of costs, in index points and in R multiples.
    points: float = 0.0
    r_multiple: float = 0.0
    ib_range: float = np.nan

    @property
    def was_filled(self) -> bool:
        return self.fill_time is not None

    @property
    def counts_in_pnl(self) -> bool:
        return self.was_filled and self.outcome != "AMBIGUOUS"


def _levels(ib: InitialBalance, broke: str, cfg: IBTradeConfig):
    """Entry, stop and target for one setup.

    REVERSION fades the break: after the IB low gives way, buy the retrace
    into the range with the stop beyond the broken low. CONTINUATION does the
    opposite — join the break on a shallow pullback.
    """
    buffer = cfg.stop_buffer_frac * ib.range

    if cfg.mode == "REVERSION":
        if broke == LOW:
            direction = "LONG"
            entry = ib.level(cfg.entry_level)
            stop = ib.low - buffer
            target = ib.high if cfg.target == "FAR_SIDE" else ib.level(float(cfg.target))
        else:
            direction = "SHORT"
            entry = ib.level(1.0 - cfg.entry_level)
            stop = ib.high + buffer
            target = ib.low if cfg.target == "FAR_SIDE" else ib.level(1.0 - float(cfg.target))
    else:  # CONTINUATION — join the break on a shallow pullback.
        # The pullback is measured back from the broken extreme, so a small
        # entry_level means a shallow retrace. Taking `1 - entry_level`
        # directly (as an earlier version did) put the entry exactly on the
        # midpoint stop, producing zero-risk trades and nonsense R multiples.
        pullback = min(max(cfg.entry_level, 0.05), 0.95)
        if broke == HIGH:
            direction = "LONG"
            entry = ib.high - pullback * 0.5 * ib.range
            stop = ib.mid
            target = ib.high + 0.5 * ib.range
        else:
            direction = "SHORT"
            entry = ib.low + pullback * 0.5 * ib.range
            stop = ib.mid
            target = ib.low - 0.5 * ib.range

    return direction, entry, stop, target


def simulate_session(
    session: pd.DataFrame, cfg: IntradayConfig, trade_cfg: IBTradeConfig
) -> Trade:
    """Walk one session bar by bar and produce its trade record."""
    outcome = analyse_session(session, cfg)
    if outcome.ib is None:
        return Trade(session_date=outcome.session_date, outcome="NO_IB")
    ib = outcome.ib
    if outcome.brk is None or not outcome.brk.is_resolved:
        reason = "NO_BREAK" if (outcome.brk is None or outcome.brk.side is None) else "AMBIGUOUS_BREAK"
        return Trade(outcome.session_date, broke_first=outcome.brk.side if outcome.brk else None,
                     outcome=reason, ib_range=ib.range)

    broke = outcome.brk.side
    direction, entry, stop, target = _levels(ib, broke, trade_cfg)

    # A setup whose stop sits on (or the wrong side of) its entry has no
    # risk to divide by, and would report an unbounded R multiple. That is
    # a misconfiguration, not a trade.
    long_side = direction == "LONG"
    risk_distance = (entry - stop) if long_side else (stop - entry)
    if not np.isfinite(risk_distance) or risk_distance <= 0.01 * ib.range:
        logger.debug(
            "Degenerate %s setup on %s: entry %.2f stop %.2f", direction, ib.session_date, entry, stop
        )
        return Trade(ib.session_date, direction=direction, broke_first=broke, entry=entry,
                     stop=stop, target=target, ib_range=ib.range, outcome="DEGENERATE")
    trade = Trade(
        session_date=ib.session_date,
        direction=direction,
        broke_first=broke,
        entry=entry,
        stop=stop,
        target=target,
        ib_range=ib.range,
        outcome="NO_FILL",
    )

    tz = session.index.tz
    day = ib.session_date
    cutoff = pd.Timestamp(datetime.combine(day, trade_cfg.entry_cutoff), tz=tz)
    flat_by = pd.Timestamp(datetime.combine(day, trade_cfg.exit_at), tz=tz)

    # A fill is only taken from a bar strictly after the break bar: the break
    # bar's own internal ordering is unknown.
    candidates = session.loc[session.index > outcome.brk.time]
    long = direction == "LONG"
    slip = trade_cfg.slippage_frac * ib.range

    filled_at = None
    for stamp, bar in candidates.iterrows():
        if stamp > flat_by:
            break

        if filled_at is None:
            if stamp > cutoff:
                trade.outcome = "CUTOFF"
                return trade
            # The premise is one failed push. A break of the other extreme
            # before the fill invalidates it.
            if trade_cfg.cancel_on_double_break:
                opposite = bar["High"] > ib.high if broke == LOW else bar["Low"] < ib.low
                if opposite:
                    trade.outcome = "DOUBLE_BREAK"
                    return trade
            # A limit order fills when the bar's RANGE CONTAINS the level.
            # Testing only `High >= entry` (for a long) is wrong whenever the
            # entry sits BELOW the current price, as it does for a
            # continuation pullback: price is already through the level, so
            # every bar "fills" instantly and no pullback is ever required.
            touched = bar["Low"] <= entry <= bar["High"]
            if touched:
                filled_at = stamp
                trade.fill_time = stamp
            continue

        # --- in the trade ------------------------------------------------
        hit_stop = bar["Low"] <= stop if long else bar["High"] >= stop
        hit_target = bar["High"] >= target if long else bar["Low"] <= target

        if hit_stop and hit_target:
            if trade_cfg.same_bar_policy == "ambiguous":
                trade.outcome = "AMBIGUOUS"
                trade.exit_time = stamp
                return trade
            hit_target = False  # stop_first: take the loss

        if hit_stop:
            gapped = bar["Open"] <= stop if long else bar["Open"] >= stop
            price = float(bar["Open"]) if gapped else stop
            return _close(trade, "STOP", price, stamp, ib, trade_cfg, slip, long)
        if hit_target:
            gapped = bar["Open"] >= target if long else bar["Open"] <= target
            price = float(bar["Open"]) if gapped else target
            return _close(trade, "TARGET", price, stamp, ib, trade_cfg, slip, long)

    if filled_at is not None:
        # Flat by the close, at whatever the last available print was.
        window = session.loc[session.index <= flat_by]
        last = window.iloc[-1] if not window.empty else session.iloc[-1]
        stamp = window.index[-1] if not window.empty else session.index[-1]
        return _close(trade, "TIME", float(last["Close"]), stamp, ib, trade_cfg, slip, long)

    return trade


def _close(trade, reason, price, stamp, ib, cfg, slip, long):
    """Book the exit, charging slippage and commission on both sides."""
    sign = 1.0 if long else -1.0
    # Slippage always works against the trade: worse entry, worse exit.
    effective_entry = trade.entry + sign * slip
    effective_exit = price - sign * slip

    gross = sign * (effective_exit - effective_entry)
    commission = cfg.per_side_pct * (abs(effective_entry) + abs(effective_exit))
    net = gross - commission

    risk = abs(effective_entry - trade.stop)
    trade.exit_price = float(price)
    trade.exit_time = stamp
    trade.outcome = reason
    trade.points = float(net)
    trade.r_multiple = float(net / risk) if risk > 0 else 0.0
    return trade


# ==========================================================================
# AGGREGATION
# ==========================================================================


def summarize_trades(trades: list[Trade], confidence: float = 0.95) -> dict:
    """Aggregate into the figures that decide whether this is tradable."""
    filled = [t for t in trades if t.counts_in_pnl]
    wins = [t for t in filled if t.points > 0]
    losses = [t for t in filled if t.points <= 0]
    n = len(filled)

    points = np.array([t.points for t in filled], dtype=float)
    r_values = np.array([t.r_multiple for t in filled], dtype=float)

    gross_win = sum(t.points for t in wins)
    gross_loss = abs(sum(t.points for t in losses))
    equity = np.cumsum(points) if n else np.array([0.0])
    peak = np.maximum.accumulate(equity)
    drawdown = float(np.max(peak - equity)) if n else 0.0

    lo, hi = wilson_interval(len(wins), n, confidence) if n else (np.nan, np.nan)

    # Is the average R different from zero, or is this noise?
    if n > 1 and r_values.std(ddof=1) > 0:
        t_stat = r_values.mean() / (r_values.std(ddof=1) / np.sqrt(n))
    else:
        t_stat = np.nan

    return {
        "sessions": len(trades),
        "setups": sum(1 for t in trades if t.direction is not None),
        "filled": n,
        "no_fill": sum(1 for t in trades if t.outcome == "NO_FILL"),
        "cutoff": sum(1 for t in trades if t.outcome == "CUTOFF"),
        "double_break": sum(1 for t in trades if t.outcome == "DOUBLE_BREAK"),
        "degenerate": sum(1 for t in trades if t.outcome == "DEGENERATE"),
        "ambiguous": sum(1 for t in trades if t.outcome == "AMBIGUOUS"),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100 * len(wins) / n, 2) if n else np.nan,
        "win_rate_ci": (round(100 * lo, 2), round(100 * hi, 2)) if n else (np.nan, np.nan),
        "target_hits": sum(1 for t in filled if t.outcome == "TARGET"),
        "stop_hits": sum(1 for t in filled if t.outcome == "STOP"),
        "time_exits": sum(1 for t in filled if t.outcome == "TIME"),
        "total_points": round(float(points.sum()), 1) if n else 0.0,
        "avg_points": round(float(points.mean()), 2) if n else np.nan,
        "expectancy_r": round(float(r_values.mean()), 3) if n else np.nan,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else np.inf,
        "max_drawdown_points": round(drawdown, 1),
        "t_stat": round(float(t_stat), 2) if np.isfinite(t_stat) else np.nan,
    }


def trades_to_frame(trades: list[Trade]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": t.session_date,
                "broke_first": t.broke_first,
                "direction": t.direction,
                "entry": t.entry,
                "stop": t.stop,
                "target": t.target,
                "fill_time": t.fill_time,
                "exit_time": t.exit_time,
                "exit_price": t.exit_price,
                "outcome": t.outcome,
                "points": t.points,
                "r": t.r_multiple,
                "ib_range": t.ib_range,
            }
            for t in trades
        ]
    )
