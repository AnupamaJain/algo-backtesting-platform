"""The backtest worker. Phase 9.

``POST /api/v1/backtests`` enqueues a run; this is what consumes the queue.
Without it a user could create a backtest that stayed QUEUED forever, which
is worse than not offering backtests at all.

**Signals come from stored patterns, not from a re-run of the detector.**
The daily scan already recorded what each engine saw on each date, with the
engine version that produced it. Replaying those rows means a backtest
measures exactly what the scanner showed at the time — re-detecting here
would be the second implementation the architecture forbids, and it would
quietly use today's engine on yesterday's question.

Entry rules are evaluated by ``services.conditions``, the same evaluator the
live screener uses. A strategy therefore cannot mean one thing on the
dashboard and another in its own backtest.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_config
from ..db.base import session_scope
from ..db.models import (
    BacktestEquityPoint,
    BacktestRun,
    BacktestTrade,
    Industry,
    MarketRegimeRow,
    Pattern,
    RelativeStrength,
    Sector,
    SectorMetric,
    Stock,
)
from ..domain.types import SurvivorshipMode
from ..services import backtest as engine
from ..services.conditions import evaluate, validate
from ..services.scanner import load_prices
from ..versioning import SCORING_RULE_VERSION, VCP_ENGINE_VERSION

logger = logging.getLogger(__name__)


@dataclass
class WorkerReport:
    claimed: int = 0
    completed: int = 0
    failed: int = 0
    trades_written: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.errors


def claim_next(session: Session) -> BacktestRun | None:
    """Take the oldest queued run and mark it RUNNING.

    Marked before any work starts so a second worker cannot pick up the same
    run, and so a crash leaves a visible RUNNING row rather than a job that
    silently never happened.
    """
    run = session.scalar(
        select(BacktestRun)
        .where(BacktestRun.status == "QUEUED")
        .order_by(BacktestRun.id)
        .limit(1)
    )
    if run is None:
        return None
    run.status = "RUNNING"
    run.started_at = datetime.now(timezone.utc)
    session.flush()
    return run


def signal_rows(
    session: Session, start: date, end: date
) -> dict[date, list[dict]]:
    """Stored patterns in the window, as flat rows, keyed by date.

    Each row carries the context recorded on that date -- RS, sector standing
    and regime as they were then, not as they are now. Joining today's values
    onto a past date is a look-ahead that no test of the engines would catch,
    because the engines never see it.
    """
    rows = session.execute(
        select(
            Pattern, Stock.symbol, Sector.code,
            RelativeStrength.rs_score, RelativeStrength.rs_rank,
            RelativeStrength.rs_trend,
            SectorMetric.rs_score, SectorMetric.rs_rank, SectorMetric.quadrant,
        )
        .join(Stock, Stock.id == Pattern.stock_id)
        .outerjoin(Industry, Industry.id == Stock.industry_id)
        .outerjoin(Sector, Sector.id == Industry.sector_id)
        .outerjoin(
            RelativeStrength,
            (RelativeStrength.stock_id == Stock.id)
            & (RelativeStrength.date == Pattern.base_end),
        )
        .outerjoin(
            SectorMetric,
            (SectorMetric.sector_id == Sector.id)
            & (SectorMetric.date == Pattern.base_end),
        )
        .where(Pattern.base_end >= start, Pattern.base_end <= end)
        .order_by(Pattern.base_end)
    ).all()

    regimes = {
        d: r for d, r in session.execute(
            select(MarketRegimeRow.date, MarketRegimeRow.regime)
        ).all()
    }

    by_date: dict[date, list[dict]] = {}
    for (pattern, symbol, sector_code, rs, rs_rank, rs_trend,
         sector_rs, sector_rank, quadrant) in rows:
        as_of = pattern.base_end
        by_date.setdefault(as_of, []).append({
            "symbol": symbol,
            "as_of": as_of,
            "sector": sector_code,
            "vcp_score": float(pattern.score) if pattern.score is not None else None,
            "vcp_stage": pattern.status,
            "pivot_price": float(pattern.pivot_price) if pattern.pivot_price else None,
            "base_depth_pct": (
                float(pattern.base_depth_pct) if pattern.base_depth_pct else None
            ),
            "contractions": pattern.contraction_count,
            "rs_score": float(rs) if rs is not None else None,
            "rs_rank": rs_rank,
            "rs_trend": rs_trend,
            "sector_rs": float(sector_rs) if sector_rs is not None else None,
            "sector_rank": sector_rank,
            "sector_quadrant": quadrant,
            "market_regime": regimes.get(as_of),
            "pattern_id": pattern.id,
        })
    return by_date


def _filtered(signals: dict[date, list[dict]], tree: dict | None) -> dict[date, list[dict]]:
    """Apply the strategy's entry rule with the shared evaluator."""
    if not tree:
        return signals
    out: dict[date, list[dict]] = {}
    for as_of, rows in signals.items():
        kept = [r for r in rows if evaluate(tree, r).passed]
        if kept:
            out[as_of] = kept
    return out


def _price_data(
    session: Session, symbols: set[str], end: date
) -> dict[str, pd.DataFrame]:
    frames = {}
    for symbol in sorted(symbols):
        frame = load_prices(session, symbol, as_of=end)
        if not frame.empty:
            frames[symbol] = frame
    return frames


def _config_for(run: BacktestRun, stored: dict) -> engine.BacktestConfig:
    sizing = stored.get("sizing") or {}
    costs = stored.get("costs") or {}
    return engine.BacktestConfig(
        start=run.start_date,
        end=run.end_date,
        initial_capital=float(run.initial_capital),
        entry=stored.get("entry") or {},
        exit=stored.get("exit"),
        stop_pct=stored.get("stop_pct", 8.0),
        target_pct=stored.get("target_pct"),
        trailing_stop_pct=stored.get("trailing_stop_pct"),
        max_holding_days=stored.get("max_holding_days", 60),
        max_positions=int(stored.get("max_positions", 10)),
        rebalance_days=int(stored.get("rebalance_days", 5)),
        sizing=engine.SizingRule(**sizing) if sizing else engine.SizingRule(),
        costs=engine.CostModel(**costs) if costs else engine.CostModel(),
        survivorship_mode=SurvivorshipMode(run.survivorship_mode),
        survivorship_warning=run.survivorship_warning,
    )


def _persist_trades(session: Session, run: BacktestRun, result) -> int:
    stock_ids = dict(session.execute(select(Stock.symbol, Stock.id)).all())
    written = 0
    for trade in result.trades:
        session.add(
            BacktestTrade(
                run_id=run.id,
                stock_id=stock_ids.get(trade.symbol),
                symbol=trade.symbol,
                entry_date=trade.entry_date,
                entry_price=trade.entry_price,
                exit_date=trade.exit_date,
                exit_price=trade.exit_price,
                quantity=trade.quantity,
                gross_pnl=trade.gross_pnl,
                costs=trade.costs,
                net_pnl=trade.net_pnl,
                return_pct=trade.return_pct,
                mfe_pct=trade.mfe_pct,
                mae_pct=trade.mae_pct,
                days_held=trade.days_held,
                entry_reason=(trade.entry_reason or "")[:128] or None,
                exit_reason=(trade.exit_reason or "")[:48] or None,
                context=json.dumps(trade.context, default=str) if trade.context else None,
            )
        )
        written += 1

    # The curve and the metrics the engine already computed. Recomputing them
    # downstream from the trade ledger can only give a realised curve, which
    # understates drawdown -- so keep what was measured rather than letting a
    # weaker version of it be derived later.
    run.metrics = json.dumps(result.metrics.__dict__, default=float)
    for ts, equity in result.equity_curve.items():
        session.add(BacktestEquityPoint(
            run_id=run.id,
            date=ts.date() if hasattr(ts, "date") else ts,
            equity=float(equity),
        ))

    session.flush()
    return written


def execute_run(session: Session, run: BacktestRun, cfg=None) -> int:
    """Run one backtest to completion and persist its trades."""
    cfg = cfg or get_config()
    stored = {}
    if run.config:
        try:
            stored = json.loads(run.config)
        except json.JSONDecodeError:
            logger.warning("run %s has unparseable config; using defaults", run.id)

    tree = stored.get("entry") or None
    if tree:
        # Validated here as well as at creation: a run enqueued before a
        # field was renamed must fail loudly rather than silently match
        # nothing and report a flawless zero-trade strategy.
        validate(tree)

    signals = _filtered(signal_rows(session, run.start_date, run.end_date), tree)
    symbols = {row["symbol"] for rows in signals.values() for row in rows}
    if not symbols:
        logger.info("run %s matched no stored patterns", run.id)
        return 0

    result = engine.run(
        _price_data(session, symbols, run.end_date),
        signals,
        _config_for(run, stored),
    )
    return _persist_trades(session, run, result)


def run_pending(session: Session, cfg=None, *, limit: int = 5) -> WorkerReport:
    """Drain up to ``limit`` queued runs."""
    cfg = cfg or get_config()
    report = WorkerReport()

    for _ in range(limit):
        run = claim_next(session)
        if run is None:
            break
        report.claimed += 1
        try:
            report.trades_written += execute_run(session, run, cfg)
            run.status = "COMPLETED"
            run.engine_version = VCP_ENGINE_VERSION
            run.rule_version = SCORING_RULE_VERSION
            run.finished_at = datetime.now(timezone.utc)
            report.completed += 1
        except Exception as exc:  # noqa: BLE001
            # FAILED with the reason, never a silent revert to QUEUED: a run
            # that keeps being retried forever hides the bug that breaks it.
            run.status = "FAILED"
            run.finished_at = datetime.now(timezone.utc)
            report.failed += 1
            report.errors.append(f"run {run.id}: {type(exc).__name__}: {exc}")
            logger.exception("backtest run %s failed", run.id)
        session.flush()

    return report


def run_once(limit: int = 5) -> WorkerReport:
    with session_scope() as session:
        return run_pending(session, limit=limit)


def describe(report: WorkerReport) -> str:
    if not report.claimed:
        return "no queued backtests"
    bits = [f"{report.completed}/{report.claimed} completed",
            f"{report.trades_written} trades"]
    if report.failed:
        bits.append(f"{report.failed} FAILED")
    return " · ".join(bits)


__all__ = [
    "WorkerReport", "claim_next", "describe", "execute_run",
    "run_once", "run_pending", "signal_rows",
]
