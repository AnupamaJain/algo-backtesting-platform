"""Pattern lifecycle persistence. Phases 2 and 7.

Turns scan output into durable rows, enforcing two rules:

**No state is skipped.** ``FORMING -> CONFIRMED`` is rejected, because the
breakout row it would have skipped is what the research database is built on.

**Nothing is deleted.** A failed breakout keeps its pattern, its event log and
its outcome row. A hit rate computed over survivors only is the single most
flattering lie a research tool can tell.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.types import PatternStatus, can_transition
from ..db.models import Breakout, BreakoutOutcome, Pattern, PatternEvent, Stock
from ..versioning import SCORING_RULE_VERSION, VCP_ENGINE_VERSION

logger = logging.getLogger(__name__)


class LifecycleError(ValueError):
    pass


@dataclass
class LifecycleReport:
    created: int = 0
    advanced: int = 0
    breakouts: int = 0
    failures: int = 0
    unchanged: int = 0


def _transition(
    session: Session, pattern: Pattern, to_status: PatternStatus, on: date,
    reason: str, payload: dict | None = None,
) -> None:
    """Record a state change as an event, then update the denormalised column."""
    current = PatternStatus(pattern.status)
    if current is to_status:
        return
    if not can_transition(current, to_status):
        raise LifecycleError(
            f"illegal transition {current.value} -> {to_status.value} "
            f"for pattern {pattern.id}"
        )

    session.add(
        PatternEvent(
            pattern_id=pattern.id,
            event_date=on,
            from_status=current.value,
            to_status=to_status.value,
            reason=reason,
            payload=json.dumps(payload) if payload else None,
        )
    )
    pattern.status = to_status.value


def find_open_pattern(session: Session, stock_id: int, base_start: date) -> Pattern | None:
    """An existing, non-terminal pattern for the same base."""
    return session.scalar(
        select(Pattern).where(
            Pattern.stock_id == stock_id,
            Pattern.base_start == base_start,
            Pattern.status.notin_([PatternStatus.FAILED.value, PatternStatus.COMPLETED.value]),
        )
    )


def record_scan(session: Session, rows, as_of: date) -> LifecycleReport:
    """Persist one scan's patterns, advancing the lifecycle where it moved."""
    report = LifecycleReport()

    symbols = {r.symbol for r in rows if r.vcp_score is not None}
    if not symbols:
        return report
    stock_ids = {
        symbol: sid
        for symbol, sid in session.execute(
            select(Stock.symbol, Stock.id).where(Stock.symbol.in_(symbols))
        ).all()
    }

    for row in rows:
        if row.vcp_score is None or row.symbol not in stock_ids:
            continue

        stock_id = stock_ids[row.symbol]
        base_start = _as_date(row, "base_start", as_of)
        pattern = find_open_pattern(session, stock_id, base_start)
        new_status = PatternStatus(row.vcp_stage)

        if pattern is None:
            pattern = Pattern(
                stock_id=stock_id,
                pattern_type="VCP",
                detected_on=as_of,
                base_start=base_start,
                base_end=as_of,
                base_depth_pct=row.base_depth_pct,
                base_duration_days=None,
                contraction_count=row.contractions,
                pivot_price=row.pivot_price,
                status=PatternStatus.FORMING.value,
                score=row.vcp_score,
                score_breakdown=json.dumps(
                    [{"name": c.name, "raw": c.raw, "weight": c.weight}
                     for c in row.score_components]
                ) if row.score_components else None,
                engine_version=VCP_ENGINE_VERSION,
                rule_version=SCORING_RULE_VERSION,
            )
            session.add(pattern)
            session.flush()
            session.add(
                PatternEvent(
                    pattern_id=pattern.id, event_date=as_of,
                    from_status=None, to_status=PatternStatus.FORMING.value,
                    reason="pattern detected",
                )
            )
            report.created += 1
        else:
            pattern.base_end = as_of
            pattern.score = row.vcp_score
            pattern.pivot_price = row.pivot_price
            pattern.contraction_count = row.contractions

        if new_status is not PatternStatus(pattern.status):
            try:
                _transition(
                    session, pattern, new_status, as_of,
                    reason=f"stage change to {new_status.value}",
                    payload={"score": row.vcp_score,
                             "distance_from_pivot_pct": row.distance_from_pivot_pct},
                )
                report.advanced += 1
            except LifecycleError as exc:
                # Refuse rather than force: an illegal transition means the
                # detector and the stored state disagree, and silently
                # overwriting would destroy the evidence of that.
                logger.warning("%s", exc)
                continue

            if new_status is PatternStatus.BREAKOUT:
                session.add(
                    Breakout(
                        pattern_id=pattern.id,
                        stock_id=stock_id,
                        breakout_date=as_of,
                        breakout_price=row.close,
                        pivot_price=row.pivot_price or row.close,
                        volume=row.volume,
                        rel_volume=row.rel_volume,
                        rs_at_breakout=row.rs_score,
                        sector_rank_at_breakout=row.sector_rank,
                        regime_at_breakout=row.market_regime,
                        setup_score=row.vcp_score,
                        status="PENDING",
                        engine_version=VCP_ENGINE_VERSION,
                    )
                )
                report.breakouts += 1
        else:
            report.unchanged += 1

    session.flush()
    return report


def fail_breakout(
    session: Session, breakout: Breakout, *, on: date, price: float, reason: str
) -> None:
    """Mark a breakout failed. The row stays; only its outcome is written."""
    breakout.status = "FAILED"

    outcome = breakout.outcome or BreakoutOutcome(breakout_id=breakout.id)
    outcome.failed = True
    outcome.failure_date = on
    outcome.failure_reason = reason
    outcome.exit_date = on
    outcome.exit_price = price
    outcome.days_to_failure = (on - breakout.breakout_date).days
    outcome.return_pct = (
        (price - float(breakout.breakout_price)) / float(breakout.breakout_price) * 100.0
    )
    session.add(outcome)
    # Attach to the relationship as well as the session: without this the
    # in-session `breakout.outcome` stays None until a refresh.
    breakout.outcome = outcome

    if breakout.pattern_id:
        pattern = session.get(Pattern, breakout.pattern_id)
        if pattern and can_transition(PatternStatus(pattern.status), PatternStatus.FAILED):
            _transition(session, pattern, PatternStatus.FAILED, on, reason)
    session.flush()


def ledger(session: Session, *, include_failures: bool = True) -> list[Breakout]:
    """The breakout ledger.

    ``include_failures`` defaults to True and callers must opt OUT. A default
    that hid failures would quietly turn every aggregate into a survivor
    statistic.
    """
    stmt = select(Breakout).order_by(Breakout.breakout_date.desc())
    if not include_failures:
        stmt = stmt.where(Breakout.status != "FAILED")
    return list(session.scalars(stmt).all())


def _as_date(row, attribute: str, fallback: date) -> date:
    value = getattr(row, attribute, None)
    return value if isinstance(value, date) else fallback
