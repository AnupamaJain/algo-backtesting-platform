"""Persist SMC and FVG output. Phases 4 and 5.

The engines already compute swings, breaks, order blocks, liquidity events and
gaps during a scan; without this they were recomputed on every read and
discarded afterwards. Storing them is what lets the structure endpoints be a
projection rather than a computation.

Two properties are preserved on the way in, because losing either would make
the stored rows say something the engine did not:

* **Confirmation lag.** A swing is written with the date it could first be
  KNOWN (``confirmed_on``), not only the date it occurred. Reading swings
  back by ``date`` alone would hand a backtest information from the future.
* **Mitigation is monotone.** An FVG's ``mitigation_pct`` is a running
  maximum. Re-persisting a zone never lowers it, so a gap that filled 80% and
  then saw price retreat stays 80% mitigated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import (
    FvgZone,
    LiquidityEventRow,
    OrderBlockRow,
    StructureBreakRow,
    SwingPointRow,
)
from ..versioning import FVG_ENGINE_VERSION, SMC_ENGINE_VERSION

logger = logging.getLogger(__name__)


@dataclass
class StructureReport:
    swings: int = 0
    breaks: int = 0
    order_blocks: int = 0
    liquidity: int = 0
    gaps: int = 0


def _confirmed_on(
    swing_date: date, right_bars: int, calendar: pd.DatetimeIndex | None
) -> date | None:
    """The trading date on which a swing became knowable.

    Counted in BARS along the symbol's own calendar, not calendar days: NSE
    holidays would otherwise make the lag uneven and the value wrong by up to
    a week around Diwali.
    """
    if calendar is None or len(calendar) == 0:
        return None
    stamp = pd.Timestamp(swing_date)
    positions = calendar.get_indexer([stamp])
    if positions[0] == -1:
        return None
    target = positions[0] + right_bars
    if target >= len(calendar):
        return None  # not yet confirmed as of the last bar
    return calendar[target].date()


def persist_smc(
    session: Session,
    stock_id: int,
    state,
    *,
    timeframe: str = "D1",
    left_bars: int = 3,
    right_bars: int = 3,
    calendar: pd.DatetimeIndex | None = None,
) -> StructureReport:
    """Write one symbol's structure. Existing rows are updated, not duplicated."""
    report = StructureReport()
    if state is None:
        return report

    for swing in (state.last_confirmed_high, state.last_confirmed_low):
        if swing is None:
            continue
        existing = session.scalar(
            select(SwingPointRow).where(
                SwingPointRow.stock_id == stock_id,
                SwingPointRow.timeframe == timeframe,
                SwingPointRow.date == swing.date,
                SwingPointRow.swing_type == swing.type.value,
            )
        )
        if existing is None:
            session.add(
                SwingPointRow(
                    stock_id=stock_id,
                    timeframe=timeframe,
                    date=swing.date,
                    price=float(swing.price),
                    swing_type=swing.type.value,
                    strength=float(swing.strength),
                    left_bars=left_bars,
                    right_bars=right_bars,
                    confirmed_on=_confirmed_on(swing.date, right_bars, calendar),
                    engine_version=SMC_ENGINE_VERSION,
                )
            )
            report.swings += 1

    for event in state.breaks:
        existing = session.scalar(
            select(StructureBreakRow).where(
                StructureBreakRow.stock_id == stock_id,
                StructureBreakRow.timeframe == timeframe,
                StructureBreakRow.date == event.date,
                StructureBreakRow.kind == event.kind.value,
                StructureBreakRow.direction == event.direction.value,
            )
        )
        if existing is None:
            session.add(
                StructureBreakRow(
                    stock_id=stock_id,
                    timeframe=timeframe,
                    date=event.date,
                    kind=event.kind.value,
                    direction=event.direction.value,
                    broken_level=float(event.broken_level),
                    broken_swing_date=event.broken_swing_date,
                    close_price=float(event.close_price),
                    volume=float(event.volume) if event.volume is not None else None,
                    prior_structure=event.prior_structure,
                    engine_version=SMC_ENGINE_VERSION,
                )
            )
            report.breaks += 1

    for block in state.order_blocks:
        existing = session.scalar(
            select(OrderBlockRow).where(
                OrderBlockRow.stock_id == stock_id,
                OrderBlockRow.timeframe == timeframe,
                OrderBlockRow.date == block.date,
                OrderBlockRow.direction == block.direction.value,
            )
        )
        if existing is None:
            session.add(
                OrderBlockRow(
                    stock_id=stock_id,
                    timeframe=timeframe,
                    date=block.date,
                    direction=block.direction.value,
                    upper=float(block.upper),
                    lower=float(block.lower),
                    origin_break_date=block.origin_break,
                    status=block.status,
                    mitigated_at=block.mitigated_at,
                    engine_version=SMC_ENGINE_VERSION,
                )
            )
            report.order_blocks += 1
        else:
            # Status moves forward only. A mitigated block does not re-open
            # because a later scan looked at a shorter window.
            if existing.status == "OPEN" and block.status != "OPEN":
                existing.status = block.status
                existing.mitigated_at = block.mitigated_at

    for event in state.liquidity:
        existing = session.scalar(
            select(LiquidityEventRow).where(
                LiquidityEventRow.stock_id == stock_id,
                LiquidityEventRow.timeframe == timeframe,
                LiquidityEventRow.date == event.date,
                LiquidityEventRow.kind == event.kind,
                LiquidityEventRow.level == float(event.level),
            )
        )
        if existing is None:
            session.add(
                LiquidityEventRow(
                    stock_id=stock_id,
                    timeframe=timeframe,
                    date=event.date,
                    kind=event.kind,
                    level=float(event.level),
                    direction=event.direction.value if event.direction else None,
                    reclaimed=bool(event.reclaimed),
                    linked_break_date=event.linked_break,
                    engine_version=SMC_ENGINE_VERSION,
                )
            )
            report.liquidity += 1
        elif event.reclaimed and not existing.reclaimed:
            existing.reclaimed = True

    session.flush()
    return report


def persist_gaps(
    session: Session, stock_id: int, gaps, *, timeframe: str = "D1"
) -> int:
    """Write FVG zones, advancing mitigation monotonically."""
    written = 0
    for gap in gaps:
        existing = session.scalar(
            select(FvgZone).where(
                FvgZone.stock_id == stock_id,
                FvgZone.timeframe == timeframe,
                FvgZone.created_at_bar == gap.created_at,
                FvgZone.direction == gap.direction.value,
            )
        )
        mitigation = float(gap.mitigation_pct)
        if existing is None:
            session.add(
                FvgZone(
                    stock_id=stock_id,
                    timeframe=timeframe,
                    direction=gap.direction.value,
                    created_at_bar=gap.created_at,
                    upper_bound=float(gap.upper),
                    lower_bound=float(gap.lower),
                    size_pct=float(gap.size_pct),
                    status=gap.status.value,
                    mitigation_pct=mitigation,
                    mitigated_at=gap.mitigated_at,
                    engine_version=FVG_ENGINE_VERSION,
                )
            )
            written += 1
        else:
            stored = float(existing.mitigation_pct or 0.0)
            if mitigation > stored:
                existing.mitigation_pct = mitigation
                existing.status = gap.status.value
                existing.mitigated_at = gap.mitigated_at

    session.flush()
    return written


def persist_scan_structure(
    session: Session, structures: dict, *, timeframe: str = "D1", smc_cfg=None
) -> StructureReport:
    """Persist structure for every symbol a scan analysed.

    ``structures`` maps symbol -> {"stock_id", "state", "gaps", "calendar"}.
    """
    total = StructureReport()
    left = getattr(smc_cfg, "swing_left_bars", 3)
    right = getattr(smc_cfg, "swing_right_bars", 3)

    for symbol, payload in structures.items():
        stock_id = payload.get("stock_id")
        if stock_id is None:
            logger.debug("no stock row for %s; structure not persisted", symbol)
            continue

        part = persist_smc(
            session, stock_id, payload.get("state"),
            timeframe=timeframe, left_bars=left, right_bars=right,
            calendar=payload.get("calendar"),
        )
        total.swings += part.swings
        total.breaks += part.breaks
        total.order_blocks += part.order_blocks
        total.liquidity += part.liquidity
        total.gaps += persist_gaps(
            session, stock_id, payload.get("gaps") or [], timeframe=timeframe
        )

    return total


def open_gaps_for(
    session: Session, stock_id: int, *, timeframe: str = "D1", direction: str | None = None
) -> list[FvgZone]:
    stmt = select(FvgZone).where(
        FvgZone.stock_id == stock_id,
        FvgZone.timeframe == timeframe,
        FvgZone.status.in_(("OPEN", "PARTIALLY_FILLED")),
    )
    if direction:
        stmt = stmt.where(FvgZone.direction == direction.upper())
    return list(session.scalars(stmt.order_by(FvgZone.created_at_bar.desc())).all())
