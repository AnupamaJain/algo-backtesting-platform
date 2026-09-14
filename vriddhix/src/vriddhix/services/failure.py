"""The failure engine. Phase 7.

Every recorded breakout is followed until it resolves. Three outcomes:

* **FAILED** -- price closed below the pivot by more than the buffer. The row
  is kept, with the reason and the day it happened.
* **CONFIRMED** -- price held above the pivot for ``confirm_within_days``
  sessions. The pattern completes.
* still **PENDING** -- neither has happened yet; MFE and MAE are updated so a
  half-resolved breakout still has honest running numbers.

This is the piece that makes the breakout ledger a research database rather
than a list of alerts. A platform that records breakouts but never records
what became of them can only ever report on the ones that worked, because
those are the only ones anybody remembers to close.

The failure rule is CLOSE-based, not intraday-low-based. A wick through the
pivot on an illiquid open is not a failed breakout, and treating it as one
would put the failure rate somewhere between pessimistic and fictional.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Breakout, BreakoutOutcome, Pattern, Stock
from ..domain.types import PatternStatus, can_transition
from .lifecycle import _transition, fail_breakout

logger = logging.getLogger(__name__)

#: Reasons are a closed set so the failure-analysis panel can group by them.
REASON_CLOSE_BELOW_PIVOT = "CLOSE_BELOW_PIVOT"


@dataclass
class FailureReport:
    evaluated: int = 0
    failed: int = 0
    confirmed: int = 0
    pending: int = 0
    missing_prices: list[str] = field(default_factory=list)


def open_breakouts(session: Session, as_of: date) -> list[Breakout]:
    """Breakouts still awaiting an outcome on ``as_of``."""
    return list(
        session.scalars(
            select(Breakout)
            .where(Breakout.status == "PENDING", Breakout.breakout_date <= as_of)
            .order_by(Breakout.breakout_date)
        ).all()
    )


def _excursions(bars: pd.DataFrame, entry: float) -> tuple[float | None, float | None]:
    """Best and worst percentage excursion since entry.

    Measured from highs and lows rather than closes: MFE/MAE describe how far
    the trade actually travelled, which is the number that tells a user
    whether a stop would have been hit.
    """
    if bars.empty or entry <= 0:
        return None, None
    mfe = (float(bars["high"].max()) - entry) / entry * 100.0
    mae = (float(bars["low"].min()) - entry) / entry * 100.0
    return mfe, mae


def evaluate_breakouts(
    session: Session,
    as_of: date,
    *,
    failure_buffer_pct: float = 2.0,
    confirm_within_days: int = 3,
    prices: dict[str, pd.DataFrame] | None = None,
) -> FailureReport:
    """Resolve every open breakout against the bars up to ``as_of``.

    ``prices`` may be supplied by a caller that already loaded them (the daily
    scan has them in hand); otherwise they are read from the database. Either
    way the bars are truncated at ``as_of``, so running this for a past date
    produces exactly what it would have produced on that date.
    """
    from .scanner import load_prices  # local: avoids an import cycle

    report = FailureReport()
    breakouts = open_breakouts(session, as_of)
    if not breakouts:
        return report

    symbols = {
        sid: symbol
        for sid, symbol in session.execute(select(Stock.id, Stock.symbol)).all()
    }

    for breakout in breakouts:
        report.evaluated += 1
        symbol = symbols.get(breakout.stock_id)
        if symbol is None:
            continue

        bars = (prices or {}).get(symbol)
        if bars is None:
            bars = load_prices(session, symbol, as_of=as_of)
        else:
            bars = bars.loc[bars.index <= pd.Timestamp(as_of)]

        after = bars.loc[bars.index > pd.Timestamp(breakout.breakout_date)]
        if after.empty:
            # The breakout bar itself is the only bar so far; nothing has
            # happened to it yet.
            report.pending += 1
            continue

        entry = float(breakout.breakout_price)
        pivot = float(breakout.pivot_price)
        stop = pivot * (1.0 - failure_buffer_pct / 100.0)

        below = after.index[after["close"] < stop]
        if len(below):
            failed_on = below[0].date()
            price = float(after.loc[below[0], "close"])
            fail_breakout(
                session, breakout, on=failed_on, price=price,
                reason=REASON_CLOSE_BELOW_PIVOT,
            )
            # MFE/MAE are measured only up to the exit: excursions after a
            # position would have been closed are not the trade's numbers.
            until = after.loc[after.index <= below[0]]
            mfe, mae = _excursions(until, entry)
            outcome = breakout.outcome
            outcome.mfe_pct = mfe
            outcome.mae_pct = mae
            outcome.drawdown_pct = mae
            report.failed += 1
            continue

        held = len(after)
        mfe, mae = _excursions(after, entry)
        outcome = breakout.outcome or BreakoutOutcome(breakout_id=breakout.id)
        outcome.mfe_pct = mfe
        outcome.mae_pct = mae
        outcome.drawdown_pct = mae
        outcome.days_held = held
        last_close = float(after["close"].iloc[-1])
        outcome.return_pct = (last_close - entry) / entry * 100.0
        session.add(outcome)
        breakout.outcome = outcome

        if held >= confirm_within_days:
            breakout.status = "CONFIRMED"
            _confirm_pattern(session, breakout, on=as_of)
            report.confirmed += 1
        else:
            report.pending += 1

    session.flush()
    return report


def _confirm_pattern(session: Session, breakout: Breakout, *, on: date) -> None:
    """Advance the pattern to CONFIRMED, refusing rather than forcing.

    A pattern that is not in BREAKOUT when its breakout confirms means the
    detector and the stored state disagree; overwriting would destroy the
    evidence of that disagreement.
    """
    if not breakout.pattern_id:
        return
    pattern = session.get(Pattern, breakout.pattern_id)
    if pattern is None:
        return
    current = PatternStatus(pattern.status)
    if current is PatternStatus.CONFIRMED:
        return
    if not can_transition(current, PatternStatus.CONFIRMED):
        logger.warning(
            "breakout %s confirmed but pattern %s is %s; leaving it alone",
            breakout.id, pattern.id, current.value,
        )
        return
    _transition(
        session, pattern, PatternStatus.CONFIRMED, on,
        reason="held above pivot",
        payload={"breakout_id": breakout.id},
    )


def failure_statistics(session: Session) -> dict:
    """Aggregate the ledger. Failures are the point, not an exclusion.

    Returns None rather than 0.0 for rates over an empty population: a
    failure rate computed from no breakouts is undefined, and 0.0 would read
    as "nothing ever failed".
    """
    rows = list(session.scalars(select(Breakout)).all())
    resolved = [b for b in rows if b.status in ("CONFIRMED", "FAILED")]
    failed = [b for b in resolved if b.status == "FAILED"]

    reasons: dict[str, int] = {}
    for breakout in failed:
        if breakout.outcome and breakout.outcome.failure_reason:
            reason = breakout.outcome.failure_reason
            reasons[reason] = reasons.get(reason, 0) + 1

    days = [
        b.outcome.days_to_failure for b in failed
        if b.outcome and b.outcome.days_to_failure is not None
    ]
    return {
        "total": len(rows),
        "pending": sum(1 for b in rows if b.status == "PENDING"),
        "resolved": len(resolved),
        "failed": len(failed),
        "confirmed": len(resolved) - len(failed),
        "failure_rate": len(failed) / len(resolved) if resolved else None,
        "reasons": reasons,
        "avg_days_to_failure": sum(days) / len(days) if days else None,
    }
