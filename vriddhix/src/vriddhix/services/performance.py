"""What a backtested rule did to money, against the obvious alternative.

Every figure here comes from a stored backtest run: real fills at the next
open, Indian statutory costs applied on both sides, and a daily mark that
includes open positions. None of it is a projection.

The benchmark is not decoration. A strategy return shown on its own invites
the reader to compare it against zero, and almost any long-only equity rule
beats zero over a decade in a market that tripled. The question worth
answering is whether the work beat buying the index and doing nothing, and
here it does not -- so that is what the panel says.

Two things are deliberately load-bearing:

*Bad bars are excluded from the benchmark, not repaired.* Three ETFs printed
a decimal-shifted level for two sessions in December 2019. Those bars are
flagged BAD_PRICE_SPAN and dropped from the comparison series; left in, the
index shows a 90% drawdown it never had.

*The survivorship warning travels with the numbers.* The universe is
backfilled from today's constituent list, so both columns describe the names
that are liquid now.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import (
    BacktestEquityPoint,
    BacktestRun,
    DataQualityEvent,
    OhlcvDaily,
    Stock,
)

#: The Nifty 50 ETF. A real instrument with real closes rather than an index
#: level, so the comparison is against something a reader could have bought.
BENCHMARK_SYMBOL = "NIFTYBEES"
BENCHMARK_LABEL = "Nifty 50 ETF, bought once and held"


@dataclass(frozen=True)
class Series:
    """One equity path, already reduced to what a chart needs."""

    label: str
    total_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    final: float
    points: list[float]          # normalised to the starting capital
    peak_to_trough: tuple[int, int] | None = None
    #: (fraction across the series, label) for drawing a time axis.
    ticks: list[tuple[float, str]] | None = None


def _curve_stats(values: list[float], start: date, end: date,
                 capital: float) -> tuple[float, float, float, tuple[int, int] | None]:
    """Total return, CAGR, worst drawdown, and where that drawdown happened."""
    if len(values) < 2:
        return 0.0, 0.0, 0.0, None

    final = values[-1]
    total = final / capital - 1.0
    years = max((end - start).days / 365.25, 1e-9)
    cagr = (final / capital) ** (1 / years) - 1.0 if final > 0 else -1.0

    peak = values[0]
    peak_at = trough_at = 0
    worst = 0.0
    best_pair: tuple[int, int] | None = None
    for i, v in enumerate(values):
        if v > peak:
            peak, peak_at = v, i
        drop = v / peak - 1.0
        if drop < worst:
            worst, trough_at, best_pair = drop, i, (peak_at, i)

    return total * 100.0, cagr * 100.0, worst * 100.0, best_pair


def _benchmark_closes(session: Session, start: date, end: date) -> list[tuple[date, float]]:
    """Benchmark closes with flagged bars removed.

    Dropped rather than interpolated: a guessed price is a silent rewrite, and
    for a buy-and-hold path two missing sessions change nothing that matters.
    """
    stock_id = session.scalar(select(Stock.id).where(Stock.symbol == BENCHMARK_SYMBOL))
    if stock_id is None:
        return []

    bad = {
        d for (d,) in session.execute(
            select(DataQualityEvent.date).where(
                DataQualityEvent.stock_id == stock_id,
                DataQualityEvent.category == "BAD_PRICE_SPAN",
                DataQualityEvent.date.is_not(None),
            )
        ).all()
    }

    rows = session.execute(
        select(OhlcvDaily.date, OhlcvDaily.close).where(
            OhlcvDaily.stock_id == stock_id,
            OhlcvDaily.date >= start,
            OhlcvDaily.date <= end,
        ).order_by(OhlcvDaily.date)
    ).all()
    return [(d, float(c)) for d, c in rows if d not in bad and c]


def _year_ticks(days: list[date], points: int) -> list[tuple[float, str]]:
    """One mark per calendar year, positioned by where it falls in the series.

    Spaced by index rather than by date so a tick lands on the same x the
    curve does; the two disagree whenever the market closes for a week.
    """
    if not days:
        return []
    ticks: list[tuple[float, str]] = []
    seen: set[int] = set()
    for i, day in enumerate(days):
        if day.year not in seen:
            seen.add(day.year)
            ticks.append((i / max(len(days) - 1, 1), str(day.year)))
    # A partial first year sits almost on top of the next one; drop it and
    # let the axis start at the first whole year.
    if len(ticks) > 1 and ticks[1][0] < 0.05:
        ticks = ticks[1:]

    # Thin by a whole-year stride, so the gaps stay even. Picking by
    # fractional index instead skips 2019 and 2023 and keeps 2024.
    if len(ticks) > 8:
        stride = -(-len(ticks) // 8)
        ticks = ticks[::stride]
    return ticks


def _resample(values: list[float], points: int) -> list[float]:
    """Thin a curve for drawing, always keeping the last value.

    The final point is the one every figure in the copy is computed from; a
    chart that stops one bar short of it invites the reader to catch the page
    contradicting itself.
    """
    # Four places, not two. These are multiples of the starting capital, so
    # 0.01 is a full percent -- coarse enough to quantise the line into
    # visible steps and to round the final point away from the figure the
    # copy quotes beside it.
    if len(values) <= points:
        return [round(v, 4) for v in values]
    step = len(values) / points
    out = [values[int(i * step)] for i in range(points)]
    out[-1] = values[-1]
    return [round(v, 4) for v in out]


def latest_run(session: Session) -> BacktestRun | None:
    return session.scalars(
        select(BacktestRun)
        .where(BacktestRun.status == "COMPLETED")
        .order_by(BacktestRun.finished_at.desc())
        .limit(1)
    ).first()


def performance_panel(session: Session, points: int = 150) -> dict:
    """The strategy curve, the benchmark curve, and the verdict between them.

    Returns {} when there is no completed run -- the section is dropped
    rather than rendered with a placeholder, because a fabricated equity
    curve is the single most misleading graphic a research tool could show.
    """
    run = latest_run(session)
    if run is None or not run.metrics:
        return {}

    marks = session.execute(
        select(BacktestEquityPoint.date, BacktestEquityPoint.equity)
        .where(BacktestEquityPoint.run_id == run.id)
        .order_by(BacktestEquityPoint.date)
    ).all()
    if len(marks) < 30:
        return {}

    capital = float(run.initial_capital)
    metrics = json.loads(run.metrics)

    equity = [float(e) for _, e in marks]
    first_day, last_day = marks[0][0], marks[-1][0]
    s_total, s_cagr, s_dd, s_span = _curve_stats(equity, first_day, last_day, capital)

    strategy = Series(
        label=run.name,
        total_return_pct=s_total,
        cagr_pct=s_cagr,
        max_drawdown_pct=s_dd,
        final=equity[-1],
        points=_resample([v / capital for v in equity], points),
        peak_to_trough=s_span,
        ticks=_year_ticks([d for d, _ in marks], points),
    )

    benchmark: Series | None = None
    closes = _benchmark_closes(session, first_day, last_day)
    if len(closes) >= 30:
        base = closes[0][1]
        path = [capital * (c / base) for _, c in closes]
        b_total, b_cagr, b_dd, b_span = _curve_stats(
            path, closes[0][0], closes[-1][0], capital
        )
        benchmark = Series(
            label=BENCHMARK_LABEL,
            total_return_pct=b_total,
            cagr_pct=b_cagr,
            max_drawdown_pct=b_dd,
            final=path[-1],
            points=_resample([v / capital for v in path], points),
            peak_to_trough=b_span,
        )

    return {
        "run_id": run.id,
        "name": run.name,
        "from": first_day,
        "to": last_day,
        "capital": capital,
        "strategy": strategy,
        "benchmark": benchmark,
        "verdict": _verdict(strategy, benchmark),
        "trades": int(metrics.get("total_trades") or 0),
        "win_rate_pct": _pct(metrics.get("win_rate")),
        "costs": float(metrics.get("total_costs") or 0.0),
        "avg_holding_days": float(metrics.get("avg_holding_days") or 0.0),
        "sharpe": _num(metrics.get("sharpe")),
        "calmar": _num(metrics.get("calmar")),
        "survivorship_warning": run.survivorship_warning,
        "engine_version": run.engine_version,
        "rule_version": run.rule_version,
    }


def _pct(value) -> float | None:
    return None if value is None else round(float(value) * 100.0, 1)


def _num(value) -> float | None:
    return None if value is None else round(float(value), 2)


def _verdict(strategy: Series, benchmark: Series | None) -> dict | None:
    """State plainly which side won on return, and which on drawdown.

    Written as a comparison rather than a headline because the two answers
    point in opposite directions here, and reporting only the favourable one
    is how a backtest becomes marketing.
    """
    if benchmark is None:
        return None

    return {
        "return_gap_pct": round(strategy.cagr_pct - benchmark.cagr_pct, 2),
        "drawdown_gap_pct": round(
            abs(benchmark.max_drawdown_pct) - abs(strategy.max_drawdown_pct), 1
        ),
        "beat_on_return": strategy.cagr_pct > benchmark.cagr_pct,
        "beat_on_drawdown": abs(strategy.max_drawdown_pct) < abs(benchmark.max_drawdown_pct),
    }
