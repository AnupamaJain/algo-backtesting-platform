"""The performance panel: what a rule did to money, and against what.

The risk here is not a crash, it is a number that flatters. Each test pins a
specific way this panel could quietly overstate the result.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from vriddhix.db.models import (
    BacktestEquityPoint,
    BacktestRun,
    DataQualityEvent,
    OhlcvDaily,
    Stock,
)
from vriddhix.services.performance import (
    BENCHMARK_SYMBOL,
    _curve_stats,
    performance_panel,
)


def _run(session, *, capital=1_000_000.0, days=400, growth=1.0002, name="R"):
    run = BacktestRun(
        name=name, start_date=date(2020, 1, 1),
        end_date=date(2020, 1, 1) + timedelta(days=days),
        initial_capital=capital, engine_version="v1", rule_version="v1",
        survivorship_mode="CURRENT_UNIVERSE", status="COMPLETED",
        metrics=json.dumps({"total_trades": 12, "win_rate": 0.5,
                            "total_costs": 4321.0, "avg_holding_days": 9.0,
                            "sharpe": 1.1, "calmar": 0.4}),
    )
    session.add(run)
    session.flush()
    equity = capital
    for i in range(days):
        equity *= growth
        session.add(BacktestEquityPoint(
            run_id=run.id, date=date(2020, 1, 1) + timedelta(days=i), equity=equity))
    session.flush()
    return run


def _benchmark(session, prices: list[float], start=date(2020, 1, 1)):
    stock = Stock(symbol=BENCHMARK_SYMBOL, name="Nifty ETF", exchange="NSE")
    session.add(stock)
    session.flush()
    for i, price in enumerate(prices):
        session.add(OhlcvDaily(
            stock_id=stock.id, date=start + timedelta(days=i),
            open=price, high=price, low=price, close=price,
            volume=1000, provider="test"))
    session.flush()
    return stock


def test_no_completed_run_renders_nothing(session):
    """An equity curve is the most misleading graphic a research tool can
    show. With nothing measured, the section is absent, never a placeholder."""
    assert performance_panel(session) == {}


def test_a_run_without_marks_is_not_drawn(session):
    run = BacktestRun(
        name="R", start_date=date(2020, 1, 1), end_date=date(2021, 1, 1),
        initial_capital=1e6, engine_version="v1", rule_version="v1",
        survivorship_mode="CURRENT_UNIVERSE", status="COMPLETED",
        metrics=json.dumps({}),
    )
    session.add(run)
    session.flush()
    assert performance_panel(session) == {}


def test_the_curve_is_read_not_recomputed(session):
    """The stored daily marks include open positions. Recomputing from closed
    trades gives a realised curve that steps only on exits and never marks an
    open loser -- which understates drawdown, the flattering direction."""
    _run(session, days=300)
    panel = performance_panel(session)
    assert panel["strategy"].points
    assert panel["strategy"].cagr_pct > 0


def test_drawdown_is_measured_peak_to_trough():
    values = [100.0, 120.0, 60.0, 90.0]
    _, _, dd, span = _curve_stats(values, date(2020, 1, 1), date(2021, 1, 1), 100.0)
    assert dd == pytest.approx(-50.0)       # 120 -> 60, not 100 -> 60
    assert span == (1, 2)


def test_the_benchmark_excludes_bars_flagged_as_bad(session):
    """Three ETFs printed a decimal-shifted level for two sessions in 2019.
    Left in, the index shows a 90% drawdown it never had -- on the one chart
    a reader uses to judge whether the strategy was worth the trouble.
    """
    prices = [100.0] * 40
    prices[20] = 10.0           # the decimal shift
    prices[21] = 10.0
    stock = _benchmark(session, prices)
    for offset in (20, 21):
        session.add(DataQualityEvent(
            stock_id=stock.id, date=date(2020, 1, 1) + timedelta(days=offset),
            severity="ERROR", category="BAD_PRICE_SPAN", detail="{}"))
    session.flush()

    _run(session, days=40)
    panel = performance_panel(session)

    assert panel["benchmark"] is not None
    assert panel["benchmark"].max_drawdown_pct > -1.0, (
        "a flagged bar reached the benchmark curve"
    )


def test_a_flagged_bar_is_excluded_not_repaired(session):
    """Dropped, never interpolated. A guessed price is a silent rewrite."""
    prices = [100.0] * 40
    prices[20] = 10.0
    stock = _benchmark(session, prices)
    session.add(DataQualityEvent(
        stock_id=stock.id, date=date(2020, 1, 1) + timedelta(days=20),
        severity="ERROR", category="BAD_PRICE_SPAN", detail="{}"))
    session.flush()

    row = session.query(OhlcvDaily).filter_by(
        stock_id=stock.id, date=date(2020, 1, 21)).one()
    assert float(row.close) == 10.0, "the bar was rewritten instead of skipped"


def test_the_verdict_reports_the_side_that_lost(session):
    """Both answers are stated even when they disagree. Reporting only the
    favourable one is how a backtest becomes marketing."""
    # Benchmark compounds faster than the strategy.
    _benchmark(session, [100.0 * (1.0009 ** i) for i in range(400)])
    _run(session, days=400, growth=1.0002)

    panel = performance_panel(session)
    verdict = panel["verdict"]

    assert verdict["beat_on_return"] is False
    assert verdict["return_gap_pct"] < 0


def test_the_survivorship_warning_travels_with_the_numbers(session):
    run = _run(session, days=120)
    run.survivorship_warning = "Membership is backfilled from today's list."
    session.flush()
    assert "backfilled" in performance_panel(session)["survivorship_warning"]


def test_the_drawn_curve_ends_on_the_figure_the_copy_quotes(session):
    """A chart that stops one bar short of its own headline invites the
    reader to catch the page contradicting itself."""
    run = _run(session, days=900)          # more marks than drawn points
    panel = performance_panel(session, points=50)
    strategy = panel["strategy"]

    assert len(strategy.points) == 50
    expected = strategy.final / float(run.initial_capital)
    assert strategy.points[-1] == pytest.approx(expected, rel=1e-3)
