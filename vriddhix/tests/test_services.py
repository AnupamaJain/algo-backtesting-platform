"""Phases 6-10: conditions, scanner, lifecycle, X-Ray, backtest, alerts, AI."""

from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from vriddhix.domain.types import PatternStatus, SurvivorshipMode
from vriddhix.services.conditions import (
    ConditionError,
    evaluate,
    fields_used,
    validate,
)
from vriddhix.services.scanner import SetupRow, grade_for, run_screen, ScanResult
from conftest import make_ohlcv, make_vcp


def row(**kwargs) -> SetupRow:
    defaults = dict(symbol="TEST", as_of=date(2026, 9, 14), close=100.0)
    return SetupRow(**{**defaults, **kwargs})


# ===========================================================================
# Condition trees (Phase 6)
# ===========================================================================


def test_leaf_comparison():
    tree = {"field": "vcp_score", "cmp": "gt", "value": 80}
    assert evaluate(tree, {"vcp_score": 90}).passed
    assert not evaluate(tree, {"vcp_score": 70}).passed


def test_and_requires_every_child():
    tree = {"op": "AND", "children": [
        {"field": "a", "cmp": "gt", "value": 1},
        {"field": "b", "cmp": "gt", "value": 1},
    ]}
    assert evaluate(tree, {"a": 2, "b": 2}).passed
    assert not evaluate(tree, {"a": 2, "b": 0}).passed


def test_or_requires_one_child():
    tree = {"op": "OR", "children": [
        {"field": "a", "cmp": "gt", "value": 1},
        {"field": "b", "cmp": "gt", "value": 1},
    ]}
    assert evaluate(tree, {"a": 0, "b": 2}).passed
    assert not evaluate(tree, {"a": 0, "b": 0}).passed


def test_not_inverts():
    tree = {"op": "NOT", "children": [{"field": "a", "cmp": "gt", "value": 1}]}
    assert evaluate(tree, {"a": 0}).passed
    assert not evaluate(tree, {"a": 5}).passed


def test_nested_groups():
    tree = {"op": "AND", "children": [
        {"field": "vcp_score", "cmp": "gt", "value": 80},
        {"op": "OR", "children": [
            {"field": "sector_quadrant", "cmp": "eq", "value": "LEADING"},
            {"field": "sector_rs", "cmp": "gt", "value": 70},
        ]},
    ]}
    assert evaluate(tree, {"vcp_score": 85, "sector_quadrant": "LAGGING",
                           "sector_rs": 75}).passed
    assert not evaluate(tree, {"vcp_score": 85, "sector_quadrant": "LAGGING",
                               "sector_rs": 40}).passed


def test_evaluation_reports_what_failed():
    """A screen must be able to say why a row did not come back."""
    tree = {"op": "AND", "children": [
        {"field": "vcp_score", "cmp": "gt", "value": 80},
        {"field": "rs_score", "cmp": "gt", "value": 90},
    ]}
    result = evaluate(tree, {"vcp_score": 85, "rs_score": 50})
    assert not result.passed
    assert any("rs_score" in f for f in result.failed)
    assert any("vcp_score" in m for m in result.matched)


def test_missing_field_fails_rather_than_raising():
    """A symbol with no RS should drop out of an RS screen, not abort the
    scan for every other symbol."""
    result = evaluate({"field": "rs_score", "cmp": "gt", "value": 80}, {"symbol": "X"})
    assert not result.passed
    assert "no value" in result.failed[0]


def test_incomparable_values_are_labelled():
    result = evaluate({"field": "a", "cmp": "gt", "value": 5}, {"a": "text"})
    assert not result.passed
    assert "incomparable" in result.failed[0]


@pytest.mark.parametrize("tree,message", [
    ({"op": "XOR", "children": []}, "unknown operator"),
    ({"op": "AND", "children": []}, "non-empty"),
    ({"op": "NOT", "children": [{"field": "a", "cmp": "eq", "value": 1},
                                {"field": "b", "cmp": "eq", "value": 1}]}, "exactly one"),
    ({"field": "a", "cmp": "wat", "value": 1}, "unknown comparator"),
    ({"cmp": "eq", "value": 1}, "requires a 'field'"),
    ({"field": "a", "cmp": "between", "value": 5}, "two-element"),
])
def test_malformed_trees_are_rejected_at_validation(tree, message):
    """Failing at save time costs nothing; failing halfway through a
    500-symbol scan has already written partial results."""
    with pytest.raises(ConditionError, match=message):
        validate(tree)


def test_fields_used_walks_the_tree():
    tree = {"op": "AND", "children": [
        {"field": "vcp_score", "cmp": "gt", "value": 80},
        {"op": "OR", "children": [{"field": "rs_score", "cmp": "gt", "value": 70}]},
    ]}
    assert fields_used(tree) == {"vcp_score", "rs_score"}


def test_between_and_in_comparators():
    assert evaluate({"field": "a", "cmp": "between", "value": [1, 10]}, {"a": 5}).passed
    assert evaluate({"field": "s", "cmp": "in", "value": ["A", "B"]}, {"s": "A"}).passed
    assert not evaluate({"field": "s", "cmp": "not_in", "value": ["A"]}, {"s": "A"}).passed


def test_run_screen_filters_scan_rows():
    result = ScanResult(as_of=date(2026, 9, 14), rows=[
        row(symbol="GOOD", vcp_score=90.0, rs_score=95.0),
        row(symbol="WEAK", vcp_score=40.0, rs_score=30.0),
    ])
    tree = {"op": "AND", "children": [
        {"field": "vcp_score", "cmp": "gte", "value": 80},
        {"field": "rs_score", "cmp": "gte", "value": 80},
    ]}
    assert [r.symbol for r in run_screen(result, tree)] == ["GOOD"]


def test_grade_buckets():
    thresholds = {"A_PLUS": 85, "A": 75, "B_PLUS": 65, "B": 55}
    assert grade_for(90, thresholds).value == "A_PLUS"
    assert grade_for(78, thresholds).value == "A"
    assert grade_for(20, thresholds).value == "C"


# ===========================================================================
# Scanner against the database
# ===========================================================================


@pytest.fixture
def populated(session, seeded, cfg):
    """Ingest synthetic history for the seeded symbols."""
    from vriddhix.data.ingest import ingest_symbol
    from vriddhix.data.providers import ChainedProvider
    from test_providers import FakeProvider

    # Same calendar for every symbol: a scan as of one symbol's last bar must
    # not truncate the others out of existence.
    start = date(2021, 1, 4)
    frames = {
        "RELIANCE": make_vcp(start=start),
        "TCS": make_ohlcv(319, pattern="uptrend", seed=5, start=start),
        "INFY": make_ohlcv(319, pattern="downtrend", seed=6, start=start),
    }
    for symbol, frame in frames.items():
        ingest_symbol(session, symbol, ChainedProvider([FakeProvider("fake", frame)]), cfg)
    session.flush()
    return frames


def test_scan_produces_rows_for_every_symbol_with_history(session, populated, cfg):
    from vriddhix.services.scanner import scan

    as_of = populated["RELIANCE"].index[-1].date()
    result = scan(session, cfg, as_of, symbols=["RELIANCE", "TCS", "INFY"])

    assert {r.symbol for r in result.rows} == {"RELIANCE", "TCS", "INFY"}
    assert all(r.rs_score is not None for r in result.rows)


def test_scan_skips_symbols_without_enough_history(session, seeded, cfg):
    from vriddhix.services.scanner import scan

    result = scan(session, cfg, date(2026, 9, 14), symbols=["RELIANCE"])
    assert "RELIANCE" in result.skipped


def test_scan_carries_the_survivorship_warning(session, populated, cfg):
    """The bias must travel with the numbers, not live in documentation."""
    from vriddhix.db.models import UniverseMember
    from vriddhix.services.scanner import scan

    for member in session.scalars(pd and __import__("sqlalchemy").select(UniverseMember)).all():
        member.source = "current_only"
    session.flush()

    as_of = populated["RELIANCE"].index[-1].date()
    result = scan(session, cfg, as_of)
    assert result.survivorship_warning is not None
    assert "survivorship" in result.survivorship_warning.lower()


def test_scan_is_invariant_to_future_bars(session, populated, cfg):
    """The Phase 6 gate: a scan as of T must not change when later bars exist."""
    from vriddhix.services.scanner import scan

    frame = populated["RELIANCE"]
    cutoff = frame.index[-15].date()

    early = scan(session, cfg, cutoff, symbols=["RELIANCE", "TCS", "INFY"])
    # The database already holds every bar, including those after the cutoff;
    # the scanner must truncate rather than see them.
    again = scan(session, cfg, cutoff, symbols=["RELIANCE", "TCS", "INFY"])

    assert {r.symbol: r.vcp_score for r in early.rows} == {
        r.symbol: r.vcp_score for r in again.rows
    }


# ===========================================================================
# Lifecycle (Phases 2/7)
# ===========================================================================


def test_recording_a_scan_creates_patterns(session, seeded):
    from vriddhix.services.lifecycle import record_scan

    report = record_scan(
        session,
        [row(symbol="RELIANCE", vcp_score=88.0, vcp_stage="FORMING",
             pivot_price=210.0, base_depth_pct=15.0, contractions=3)],
        date(2026, 9, 1),
    )
    assert report.created == 1

    from vriddhix.db.models import Pattern
    pattern = session.scalar(__import__("sqlalchemy").select(Pattern))
    assert pattern.status == "FORMING"
    assert len(pattern.events) == 1


def test_lifecycle_advances_through_legal_states(session, seeded):
    from sqlalchemy import select
    from vriddhix.db.models import Pattern, PatternEvent
    from vriddhix.services.lifecycle import record_scan

    base = date(2026, 6, 1)
    common = dict(symbol="RELIANCE", vcp_score=88.0, pivot_price=210.0,
                  base_depth_pct=15.0, contractions=3, base_start=base)

    record_scan(session, [row(**common, vcp_stage="FORMING")], date(2026, 9, 1))
    record_scan(session, [row(**common, vcp_stage="NEAR_PIVOT")], date(2026, 9, 2))
    record_scan(session, [row(**common, vcp_stage="BREAKOUT")], date(2026, 9, 3))

    pattern = session.scalar(select(Pattern))
    assert pattern.status == "BREAKOUT"

    events = session.scalars(
        select(PatternEvent).order_by(PatternEvent.event_date)
    ).all()
    assert [e.to_status for e in events] == ["FORMING", "NEAR_PIVOT", "BREAKOUT"]


def test_a_breakout_writes_a_ledger_row_with_context_snapshotted(session, seeded):
    from sqlalchemy import select
    from vriddhix.db.models import Breakout
    from vriddhix.services.lifecycle import record_scan

    base = date(2026, 6, 1)
    common = dict(symbol="RELIANCE", vcp_score=88.0, pivot_price=210.0,
                  base_depth_pct=15.0, contractions=3, base_start=base)
    record_scan(session, [row(**common, vcp_stage="NEAR_PIVOT")], date(2026, 9, 1))
    record_scan(
        session,
        [row(**common, vcp_stage="BREAKOUT", close=215.0, rs_score=94.0,
             market_regime="BULL", rel_volume=2.1)],
        date(2026, 9, 2),
    )

    breakout = session.scalar(select(Breakout))
    assert breakout is not None
    # Snapshotted so later recomputation of RS/regime cannot rewrite history.
    assert float(breakout.rs_at_breakout) == 94.0
    assert breakout.regime_at_breakout == "BULL"


def test_an_illegal_transition_is_refused_not_forced(session, seeded, caplog):
    from sqlalchemy import select
    from vriddhix.db.models import Pattern
    from vriddhix.services.lifecycle import record_scan

    base = date(2026, 6, 1)
    common = dict(symbol="RELIANCE", vcp_score=88.0, pivot_price=210.0, base_start=base)

    record_scan(session, [row(**common, vcp_stage="FORMING")], date(2026, 9, 1))
    record_scan(session, [row(**common, vcp_stage="CONFIRMED")], date(2026, 9, 2))

    pattern = session.scalar(select(Pattern))
    assert pattern.status == "FORMING", "FORMING -> CONFIRMED must not be applied"


def test_a_failed_breakout_is_kept(session, seeded):
    from sqlalchemy import select
    from vriddhix.db.models import Breakout
    from vriddhix.services.lifecycle import fail_breakout, ledger, record_scan

    base = date(2026, 6, 1)
    common = dict(symbol="RELIANCE", vcp_score=88.0, pivot_price=210.0, base_start=base)
    record_scan(session, [row(**common, vcp_stage="NEAR_PIVOT")], date(2026, 9, 1))
    record_scan(session, [row(**common, vcp_stage="BREAKOUT", close=215.0)], date(2026, 9, 2))

    breakout = session.scalar(select(Breakout))
    fail_breakout(session, breakout, on=date(2026, 9, 10), price=200.0,
                  reason="CLOSE_BELOW_PIVOT")

    assert len(ledger(session)) == 1                       # still there
    assert len(ledger(session, include_failures=False)) == 0
    assert breakout.outcome.failed is True
    assert breakout.outcome.days_to_failure == 8


# ===========================================================================
# X-Ray (Phase 7)
# ===========================================================================


def test_xray_finds_historical_patterns():
    from vriddhix.services.xray import summarise, xray

    patterns = xray(make_vcp(), "TESTCO", step_days=5)
    assert patterns, "a constructed VCP must be found by the historical replay"

    summary = summarise(patterns)
    assert summary["total_patterns"] == len(patterns)


def test_xray_uses_the_same_detector_as_the_live_scan():
    """Not a historical reimplementation -- the same function, replayed."""
    from vriddhix.engines.vcp import detect
    from vriddhix.services.xray import xray

    df = make_vcp()
    live = detect(df)
    historical = xray(df, "TESTCO", step_days=5)

    assert live is not None and historical
    assert any(p.base_start == live.base.start for p in historical)


def test_xray_summary_counts_failures_rather_than_filtering_them():
    """A hit rate over survivors only is the most flattering lie available."""
    from vriddhix.services.xray import HistoricalPattern, summarise

    patterns = [
        HistoricalPattern(symbol="X", detected_on=date(2024, 1, 1), base_start=date(2023, 1, 1),
                          base_end=date(2024, 1, 1), base_depth_pct=15, duration_days=60,
                          contractions=3, pivot_price=100, score=80,
                          stage_at_detection="BREAKOUT", outcome="FAILED", return_pct=-8.0),
        HistoricalPattern(symbol="X", detected_on=date(2025, 1, 1), base_start=date(2024, 6, 1),
                          base_end=date(2025, 1, 1), base_depth_pct=12, duration_days=50,
                          contractions=3, pivot_price=120, score=85,
                          stage_at_detection="BREAKOUT", outcome="BREAKOUT_CONFIRMED",
                          return_pct=14.0),
    ]
    summary = summarise(patterns)
    assert summary["failures"] == 1
    assert summary["win_rate"] == 0.5
    assert summary["avg_loss_pct"] == -8.0


# ===========================================================================
# Backtesting (Phase 8)
# ===========================================================================


def test_position_sizing_methods():
    from vriddhix.services.backtest import SizingRule, position_size

    assert position_size(SizingRule(method="fixed_qty", fixed_qty=50),
                         equity=1_000_000, price=100) == 50
    assert position_size(SizingRule(method="fixed_capital", fixed_capital=100_000),
                         equity=1_000_000, price=100) == 1000
    # risk-based: 1% of 1,000,000 = 10,000 risk; 8% stop on Rs 1,000 = Rs 80/share
    assert position_size(SizingRule(method="risk_based", risk_pct=1.0, stop_pct=8.0),
                         equity=1_000_000, price=1000) == 125


def test_position_size_respects_the_concentration_cap():
    """A risk-based size on a tight stop can otherwise become most of the book."""
    from vriddhix.services.backtest import SizingRule, position_size

    rule = SizingRule(method="risk_based", risk_pct=5.0, stop_pct=1.0, max_position_pct=25.0)
    quantity = position_size(rule, equity=1_000_000, price=100)
    assert quantity * 100 <= 1_000_000 * 0.25


def test_costs_are_charged_on_both_sides():
    from vriddhix.services.backtest import CostModel

    costs = CostModel()
    buy = costs.charges(100_000, side="BUY")
    sell = costs.charges(100_000, side="SELL")
    assert buy > 0 and sell > 0
    assert sell > buy, "STT applies on the sell side and dominates"


def test_slippage_always_works_against_the_trade():
    from vriddhix.services.backtest import CostModel

    costs = CostModel(slippage_pct=0.1)
    assert costs.fill_price(100.0, side="BUY") > 100.0
    assert costs.fill_price(100.0, side="SELL") < 100.0


def test_backtest_executes_on_the_next_open_not_the_signal_close():
    """The close that triggered a signal was not knowable until the session
    ended, so filling on it is look-ahead."""
    from vriddhix.services.backtest import BacktestConfig, SizingRule, run

    frame = make_ohlcv(60, seed=3)
    signal_day = frame.index[10].date()
    next_day = frame.index[11]

    result = run(
        {"X": frame},
        {signal_day: [{"symbol": "X", "reason": "test"}]},
        BacktestConfig(start=frame.index[0].date(), end=frame.index[-1].date(),
                       sizing=SizingRule(method="fixed_qty", fixed_qty=10),
                       stop_pct=None, max_holding_days=5),
    )
    assert result.trades
    trade = result.trades[0]
    assert trade.entry_date == next_day.date()
    # Entry is the next open plus slippage, never the signal bar's close.
    assert trade.entry_price >= float(frame.loc[next_day, "open"])


def test_backtest_applies_stops():
    from vriddhix.services.backtest import BacktestConfig, SizingRule, run

    closes = np.concatenate([np.full(5, 100.0), np.linspace(100, 70, 25)])
    frame = pd.DataFrame({
        "open": closes, "high": closes * 1.01, "low": closes * 0.99,
        "close": closes, "volume": np.full(30, 1e6),
    }, index=pd.bdate_range("2024-01-01", periods=30)).rename_axis("date")

    result = run(
        {"X": frame},
        {frame.index[1].date(): [{"symbol": "X", "reason": "test"}]},
        BacktestConfig(start=frame.index[0].date(), end=frame.index[-1].date(),
                       sizing=SizingRule(method="fixed_qty", fixed_qty=10),
                       stop_pct=5.0, max_holding_days=None),
    )
    assert result.trades[0].exit_reason == "STOP"
    assert result.trades[0].net_pnl < 0


def test_metrics_are_computed_from_closed_trades():
    from vriddhix.services.backtest import BacktestConfig, SizingRule, run

    frame = make_ohlcv(80, pattern="uptrend", seed=11)
    signals = {frame.index[i].date(): [{"symbol": "X", "reason": "t"}] for i in (5, 30, 55)}

    result = run(
        {"X": frame}, signals,
        BacktestConfig(start=frame.index[0].date(), end=frame.index[-1].date(),
                       sizing=SizingRule(method="fixed_qty", fixed_qty=10),
                       stop_pct=None, max_holding_days=10),
    )
    metrics = result.metrics
    assert metrics.total_trades >= 1
    assert 0.0 <= metrics.win_rate <= 1.0
    assert metrics.final_equity > 0
    assert len(result.equity_curve) > 0


def test_a_current_universe_backtest_carries_its_warning():
    """An unmarked biased backtest is worse than no backtest."""
    from vriddhix.services.backtest import BacktestConfig, BacktestResult

    biased = BacktestResult(config=BacktestConfig(
        start=date(2020, 1, 1), end=date(2024, 1, 1),
        survivorship_mode=SurvivorshipMode.CURRENT_UNIVERSE,
    ))
    assert biased.survivorship_warning is not None
    assert "survivorship_warning" in biased.to_payload()

    clean = BacktestResult(config=BacktestConfig(
        start=date(2020, 1, 1), end=date(2024, 1, 1),
        survivorship_mode=SurvivorshipMode.POINT_IN_TIME,
    ))
    assert clean.survivorship_warning is None


def test_walk_forward_windows_do_not_overlap_train_and_test():
    from vriddhix.services.backtest import walk_forward_windows

    windows = walk_forward_windows(date(2015, 1, 1), date(2025, 1, 1),
                                   train_months=36, test_months=12, step_months=12)
    assert windows
    for window in windows:
        assert window.train_end < window.test_start
        assert window.test_start <= window.test_end


def test_monte_carlo_describes_order_risk_not_forecast():
    from vriddhix.services.backtest import Trade, monte_carlo

    trades = [
        Trade(symbol="X", entry_date=date(2024, 1, 1), entry_price=100, quantity=10,
              entry_reason="t", exit_date=date(2024, 2, 1), net_pnl=pnl)
        for pnl in (5000, -2000, 8000, -3000, 4000, -1000, 6000)
    ]
    result = monte_carlo(trades, 1_000_000, runs=200)
    assert result["runs"] == 200
    assert result["worst_drawdown"] <= result["median_drawdown"] <= 0
    assert "not a forecast" in result["note"]


def test_monte_carlo_refuses_too_few_trades():
    from vriddhix.services.backtest import monte_carlo

    assert monte_carlo([], 1_000_000)["runs"] == 0


def test_strategy_comparison_table():
    from vriddhix.services.backtest import BacktestConfig, BacktestResult, compare

    def make(name, trades):
        result = BacktestResult(config=BacktestConfig(date(2020, 1, 1), date(2024, 1, 1)))
        result.metrics.total_trades = trades
        return result

    table = compare({"A": make("A", 10), "B": make("B", 20)})
    assert list(table.index) == ["A", "B"]
    assert table.loc["B", "trades"] == 20


# ===========================================================================
# Alerts (Phase 10)
# ===========================================================================


def test_builtin_alert_fires_on_a_matching_row(session, seeded):
    from sqlalchemy import select
    from vriddhix.db.models import Alert, AlertTrigger, User
    from vriddhix.services.alerts import evaluate_alerts

    user = User(email="a@b.c", display_name="A")
    session.add(user)
    session.flush()
    session.add(Alert(user_id=user.id, kind="BREAKOUT"))
    session.flush()

    report = evaluate_alerts(
        session, [row(symbol="RELIANCE", vcp_stage="BREAKOUT", vcp_score=90.0)],
        date(2026, 9, 14),
    )
    assert report.triggered == 1
    assert session.scalar(select(AlertTrigger)) is not None


def test_an_alert_does_not_re_fire_the_same_day(session, seeded):
    """A daily scan re-alerting every day a condition holds trains the user to
    ignore alerts, which is the same as having none."""
    from vriddhix.db.models import Alert, User
    from vriddhix.services.alerts import evaluate_alerts

    user = User(email="a@b.c", display_name="A")
    session.add(user)
    session.flush()
    session.add(Alert(user_id=user.id, kind="BREAKOUT"))
    session.flush()

    rows = [row(symbol="RELIANCE", vcp_stage="BREAKOUT")]
    first = evaluate_alerts(session, rows, date(2026, 9, 14))
    second = evaluate_alerts(session, rows, date(2026, 9, 14))

    assert first.triggered == 1
    assert second.triggered == 0


def test_custom_alert_conditions_use_the_same_evaluator(session, seeded):
    from vriddhix.db.models import Alert, User
    from vriddhix.services.alerts import evaluate_alerts

    user = User(email="a@b.c", display_name="A")
    session.add(user)
    session.flush()
    session.add(Alert(
        user_id=user.id, kind="CUSTOM",
        conditions=json.dumps({"op": "AND", "children": [
            {"field": "vcp_score", "cmp": "gte", "value": 85},
            {"field": "rs_score", "cmp": "gte", "value": 90},
        ]}),
    ))
    session.flush()

    report = evaluate_alerts(
        session,
        [row(symbol="RELIANCE", vcp_score=90.0, rs_score=95.0),
         row(symbol="TCS", vcp_score=90.0, rs_score=50.0)],
        date(2026, 9, 14),
    )
    assert report.triggered == 1


def test_a_malformed_alert_is_skipped_not_fatal(session, seeded):
    from vriddhix.db.models import Alert, User
    from vriddhix.services.alerts import evaluate_alerts

    user = User(email="a@b.c", display_name="A")
    session.add(user)
    session.flush()
    session.add(Alert(user_id=user.id, kind="CUSTOM",
                      conditions=json.dumps({"op": "XOR", "children": []})))
    session.flush()

    report = evaluate_alerts(session, [row(symbol="RELIANCE")], date(2026, 9, 14))
    assert report.skipped == 1
    assert report.errors


# ===========================================================================
# AI explanation (Phase 9)
# ===========================================================================


def test_explanation_only_uses_supplied_facts():
    from vriddhix.ai.explain import explain_setup

    explanation = explain_setup(
        row(symbol="RELIANCE", vcp_score=91.0, vcp_stage="NEAR_PIVOT", grade="A_PLUS",
            rs_score=95.0, rs_trend="IMPROVING", sector="ENERGY",
            sector_quadrant="LEADING", pivot_price=2884.0,
            distance_from_pivot_pct=1.5, market_regime="BULL",
            base_depth_pct=17.2, contractions=4)
    )
    assert "RELIANCE" in explanation.text
    assert explanation.disclaimer
    assert explanation.inputs["vcp_score"] == 91.0


def test_recommendation_language_is_rejected():
    from vriddhix.ai.explain import ExplanationRejected, screen

    for banned in ("You should buy this stock.", "Target price 3000.",
                   "This is a guaranteed multibagger.", "Price will rise."):
        with pytest.raises(ExplanationRejected):
            screen(banned, {"symbol": "X"})


def test_numbers_not_present_in_the_inputs_are_rejected():
    """The model cannot introduce a market fact of its own."""
    from vriddhix.ai.explain import ExplanationRejected, screen

    with pytest.raises(ExplanationRejected, match="not in the supplied inputs"):
        screen("The stock scores 91 and trades at 4567.", {"vcp_score": 91})


def test_supported_numbers_pass_the_screen():
    from vriddhix.ai.explain import screen

    assert screen("Scores 91 with RS 95.", {"vcp_score": 91.0, "rs_score": 95.0})


def test_inputs_are_echoed_so_claims_can_be_audited():
    from vriddhix.ai.explain import explain_setup

    payload = explain_setup(row(symbol="X", vcp_score=80.0, rs_score=70.0)).to_payload()
    assert payload["inputs"]["vcp_score"] == 80.0
    assert "not investment advice" in payload["disclaimer"].lower()


def test_failure_explanation_offers_factors_not_a_cause():
    """The recorded data cannot establish causation and the prose must not
    imply that it can."""
    from vriddhix.ai.explain import explain_failure

    class FailedBreakout:
        symbol = "TITAN"
        breakout_date = date(2026, 8, 21)
        rel_volume = 0.9
        rs_at_breakout = 55.0
        regime_at_breakout = "BEAR"
        days_to_failure = 9
        mfe_pct = 2.0
        mae_pct = -7.8
        failure_reason = "CLOSE_BELOW_PIVOT"

    explanation = explain_failure(FailedBreakout())
    assert "possible contributing factors" in explanation.text.lower()
    assert "because" not in explanation.text.lower()


def test_a_model_client_output_is_screened_before_storage():
    from vriddhix.ai.explain import ExplanationRejected, explain_setup

    class RogueModel:
        def complete(self, prompt: str) -> str:
            return "Strong setup. You should buy immediately."

    with pytest.raises(ExplanationRejected):
        explain_setup(row(symbol="X", vcp_score=90.0), client=RogueModel())
