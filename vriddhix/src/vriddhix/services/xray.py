"""Pattern X-Ray: every base a symbol has formed, and what happened next.

Phase 7. This is the feature that turns a screener into a research tool -- not
"here is a setup", but "here are the eleven times this setup appeared in this
stock since 2019, and here is what followed each one".

It works by replaying the SAME detector over history, one evaluation date at a
time, with the frame truncated at that date. No separate historical
implementation exists, so an X-Ray entry is exactly what the live scanner
would have shown on the day.

Outcomes (MFE, MAE, return) are measured from bars AFTER the pattern resolved.
That is not look-ahead: the detection used only past data, and the outcome is
explicitly a retrospective measurement of what followed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from ..engines import vcp as vcp_engine
from ..features.technical import compute_features


@dataclass(frozen=True, slots=True)
class HistoricalPattern:
    """One base found in history, with what followed it."""

    symbol: str
    detected_on: date
    base_start: date
    base_end: date
    base_depth_pct: float
    duration_days: int
    contractions: int
    pivot_price: float
    score: float
    stage_at_detection: str

    breakout_date: date | None = None
    breakout_price: float | None = None
    outcome: str = "UNRESOLVED"        # BREAKOUT_CONFIRMED | FAILED | UNRESOLVED
    mfe_pct: float | None = None
    mae_pct: float | None = None
    return_pct: float | None = None
    days_held: int | None = None
    failure_reason: str | None = None


def _measure_outcome(
    prices: pd.DataFrame,
    from_date: date,
    entry_price: float,
    horizon_days: int,
    stop_price: float | None,
) -> dict:
    """What happened in the ``horizon_days`` bars after ``from_date``."""
    future = prices.loc[prices.index > pd.Timestamp(from_date)].head(horizon_days)
    if future.empty:
        return {"outcome": "UNRESOLVED"}

    highs = future["high"].to_numpy(dtype=float)
    lows = future["low"].to_numpy(dtype=float)
    closes = future["close"].to_numpy(dtype=float)

    mfe = (highs.max() - entry_price) / entry_price * 100.0
    mae = (lows.min() - entry_price) / entry_price * 100.0

    stopped_at = None
    if stop_price is not None:
        hits = np.where(closes < stop_price)[0]
        if len(hits):
            stopped_at = int(hits[0])

    if stopped_at is not None:
        exit_price = float(closes[stopped_at])
        return {
            "outcome": "FAILED",
            "mfe_pct": mfe,
            "mae_pct": mae,
            "return_pct": (exit_price - entry_price) / entry_price * 100.0,
            "days_held": stopped_at + 1,
            "failure_reason": "CLOSE_BELOW_PIVOT",
        }

    exit_price = float(closes[-1])
    return {
        "outcome": "BREAKOUT_CONFIRMED",
        "mfe_pct": mfe,
        "mae_pct": mae,
        "return_pct": (exit_price - entry_price) / entry_price * 100.0,
        "days_held": len(future),
    }


def xray(
    prices: pd.DataFrame,
    symbol: str,
    cfg: vcp_engine.VcpConfig | None = None,
    *,
    step_days: int = 5,
    horizon_days: int = 40,
    failure_buffer_pct: float | None = None,
) -> list[HistoricalPattern]:
    """Replay the detector across a symbol's history.

    ``step_days`` trades resolution for cost: evaluating every fifth bar finds
    the same bases as evaluating every bar (a base persists for weeks) at a
    fifth of the work. Breakout dates are then pinned to the exact bar by
    scanning forward from the pivot, so the saving costs no precision where it
    matters.
    """
    cfg = cfg or vcp_engine.VcpConfig()
    failure_buffer_pct = (
        cfg.failure_buffer_pct if failure_buffer_pct is None else failure_buffer_pct
    )

    minimum = cfg.min_base_days + cfg.min_prior_trend_days
    if len(prices) <= minimum:
        return []

    # Computed once over the whole series and sliced per evaluation, which is
    # legitimate precisely because every feature is causal.
    features = compute_features(prices, validate=False)

    found: list[HistoricalPattern] = []
    seen_bases: set[date] = set()

    for i in range(minimum, len(prices), step_days):
        window = prices.iloc[: i + 1]
        result = vcp_engine.detect(window, cfg, features=features.iloc[: i + 1])
        if result is None or result.base.start in seen_bases:
            continue
        seen_bases.add(result.base.start)

        pivot = result.base.pivot_price
        after = prices.loc[prices.index > pd.Timestamp(result.base.end)]
        crossings = after.index[after["close"] > pivot]

        if len(crossings) == 0:
            found.append(
                HistoricalPattern(
                    symbol=symbol,
                    detected_on=window.index[-1].date(),
                    base_start=result.base.start,
                    base_end=result.base.end,
                    base_depth_pct=result.base.depth_pct,
                    duration_days=result.base.duration_days,
                    contractions=len(result.base.contractions),
                    pivot_price=pivot,
                    score=result.score.total,
                    stage_at_detection=result.stage.value,
                )
            )
            continue

        breakout_date = crossings[0].date()
        breakout_price = float(after.loc[crossings[0], "close"])
        outcome = _measure_outcome(
            prices,
            breakout_date,
            breakout_price,
            horizon_days,
            stop_price=pivot * (1 - failure_buffer_pct / 100.0),
        )

        found.append(
            HistoricalPattern(
                symbol=symbol,
                detected_on=window.index[-1].date(),
                base_start=result.base.start,
                base_end=result.base.end,
                base_depth_pct=result.base.depth_pct,
                duration_days=result.base.duration_days,
                contractions=len(result.base.contractions),
                pivot_price=pivot,
                score=result.score.total,
                stage_at_detection=result.stage.value,
                breakout_date=breakout_date,
                breakout_price=breakout_price,
                **{k: v for k, v in outcome.items()},
            )
        )

    return found


def _mean(values) -> float | None:
    """Mean of the values that exist. An unresolved pattern has no MFE, which
    is a legitimate state rather than missing data."""
    present = [v for v in values if v is not None]
    return float(np.mean(present)) if present else None


def summarise(patterns: list[HistoricalPattern]) -> dict:
    """Aggregate an X-Ray into the numbers the panel shows.

    Failures are counted, never filtered. A hit rate computed over survivors
    only is the single most flattering lie a research tool can tell.
    """
    resolved = [p for p in patterns if p.outcome in ("BREAKOUT_CONFIRMED", "FAILED")]
    wins = [p for p in resolved if (p.return_pct or 0) > 0]
    losses = [p for p in resolved if (p.return_pct or 0) <= 0]

    return {
        "total_patterns": len(patterns),
        "resolved": len(resolved),
        "unresolved": len(patterns) - len(resolved),
        "breakouts": sum(1 for p in patterns if p.breakout_date is not None),
        "failures": sum(1 for p in patterns if p.outcome == "FAILED"),
        "win_rate": len(wins) / len(resolved) if resolved else None,
        "avg_return_pct": _mean(p.return_pct for p in resolved),
        "avg_win_pct": _mean(p.return_pct for p in wins),
        "avg_loss_pct": _mean(p.return_pct for p in losses),
        "avg_mfe_pct": _mean(p.mfe_pct for p in resolved),
        "avg_mae_pct": _mean(p.mae_pct for p in resolved),
        "avg_score": _mean(p.score for p in patterns),
    }
