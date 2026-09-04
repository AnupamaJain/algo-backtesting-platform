"""The IB trade simulator must never resolve an ambiguity in its own favour.

A path-dependent backtest decides its own result on the bars where the stop
and the target both appear. These tests pin that behaviour down, along with
the look-ahead rules around the break bar and the session close.
"""

from datetime import date, time

import numpy as np
import pandas as pd
import pytest

from quant_backtester.src.ib_backtest import (
    IBTradeConfig,
    simulate_session,
    summarize_trades,
)
from quant_backtester.src.intraday import IntradayConfig

TZ = "Asia/Kolkata"
OPEN = time(9, 15)


def _cfg():
    from pathlib import Path
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    return IntradayConfig(
        provider="flattrade", exchange="NSE", auth={}, interval="5",
        cache_dir=tmp, max_days_per_request=30, timezone=TZ,
        session_open=OPEN, session_close=time(15, 30), ib_minutes=60,
        break_measure="WICK", straddle_policy="ambiguous",
        instruments=[{"symbol": "X", "alias": "X"}],
        lookback_days=120, confidence_level=0.95, results_dir=tmp,
    )


def _trade_cfg(**over):
    base = {
        "mode": "REVERSION", "entry_level": 0.5, "stop_buffer_frac": 0.08,
        "target": "FAR_SIDE", "entry_cutoff": "14:30", "exit_at": "15:20",
        "same_bar_policy": "stop_first", "cancel_on_double_break": True,
        "costs": {"per_side_pct": 0.0, "slippage_frac": 0.0},
    }
    base.update(over)
    return IBTradeConfig.from_dict(base)


def _session(bars):
    """Bars from 09:15 at 5-minute spacing."""
    index = pd.date_range("2026-08-03 09:15", periods=len(bars), freq="5min", tz=TZ)
    return pd.DataFrame(bars, index=index, columns=["Open", "High", "Low", "Close"])


# A 12-bar IB spanning exactly 100..200, so levels are easy to read:
#   IB low 100, IB high 200, mid 150, stop buffer 8 -> stop at 92.
IB = [(150, 200, 100, 150)] + [(150, 160, 140, 150)] * 11


def test_low_break_produces_a_long_faded_back_into_the_range():
    bars = IB + [(150, 150, 90, 95)]          # break the IB low
    bars += [(95, 150, 95, 150)]              # retrace up to the midpoint
    trade = simulate_session(_session(bars), _cfg(), _trade_cfg())

    assert trade.broke_first == "LOW"
    assert trade.direction == "LONG"
    assert trade.entry == 150 and trade.stop == 92 and trade.target == 200


def test_high_break_produces_a_short():
    bars = IB + [(150, 210, 150, 205)] + [(205, 205, 150, 150)]
    trade = simulate_session(_session(bars), _cfg(), _trade_cfg())

    assert trade.broke_first == "HIGH"
    assert trade.direction == "SHORT"
    assert trade.entry == 150 and trade.stop == 208 and trade.target == 100


def test_the_break_bar_itself_never_fills_the_entry():
    """The break bar may trade through the entry, but its ordering is unknown."""
    # This bar breaks the low AND reaches back up to 150 in the same bar.
    bars = IB + [(150, 155, 90, 95)]
    trade = simulate_session(_session(bars), _cfg(), _trade_cfg())

    assert trade.broke_first == "LOW"
    assert trade.was_filled is False
    assert trade.outcome == "NO_FILL"


def test_target_reached_books_a_win():
    bars = IB + [(150, 150, 90, 95), (95, 150, 95, 150), (150, 205, 150, 200)]
    trade = simulate_session(_session(bars), _cfg(), _trade_cfg())

    assert trade.outcome == "TARGET"
    assert trade.points == pytest.approx(50.0)      # 150 -> 200
    assert trade.r_multiple == pytest.approx(50 / 58)


def test_stop_hit_books_the_loss():
    bars = IB + [(150, 150, 90, 95), (95, 150, 95, 150), (150, 150, 85, 88)]
    trade = simulate_session(_session(bars), _cfg(), _trade_cfg())

    assert trade.outcome == "STOP"
    assert trade.points == pytest.approx(-58.0)     # 150 -> 92
    assert trade.r_multiple == pytest.approx(-1.0)


def test_same_bar_stop_and_target_takes_the_loss_by_default():
    """The decisive case: never let the backtest pick the good outcome."""
    bars = IB + [(150, 150, 90, 95), (95, 150, 95, 150), (150, 205, 85, 150)]
    trade = simulate_session(_session(bars), _cfg(), _trade_cfg())

    assert trade.outcome == "STOP"
    assert trade.points < 0


def test_same_bar_can_be_excluded_instead_but_never_resolved_favourably():
    bars = IB + [(150, 150, 90, 95), (95, 150, 95, 150), (150, 205, 85, 150)]
    trade = simulate_session(_session(bars), _cfg(), _trade_cfg(same_bar_policy="ambiguous"))

    assert trade.outcome == "AMBIGUOUS"
    assert trade.counts_in_pnl is False


def test_target_first_is_rejected_as_a_configuration():
    with pytest.raises(ValueError, match="same_bar_policy"):
        _trade_cfg(same_bar_policy="target_first")


def test_gap_through_the_stop_fills_at_the_open():
    bars = IB + [(150, 150, 90, 95), (95, 150, 95, 150), (80, 85, 75, 80)]
    trade = simulate_session(_session(bars), _cfg(), _trade_cfg())

    assert trade.outcome == "STOP"
    # Filled at the open (80), not at the untouchable stop level (92).
    assert trade.points == pytest.approx(80 - 150)


def test_double_break_before_fill_cancels_the_setup():
    bars = IB + [(150, 150, 90, 95), (95, 210, 95, 205)]
    trade = simulate_session(_session(bars), _cfg(), _trade_cfg())

    assert trade.outcome == "DOUBLE_BREAK"
    assert trade.was_filled is False


def test_unfilled_setup_after_the_cutoff_is_dropped_not_chased():
    bars = IB + [(150, 150, 90, 95)]
    # Quiet bars well past 14:30, never reaching the entry.
    bars += [(95, 96, 94, 95)] * 70
    trade = simulate_session(_session(bars), _cfg(), _trade_cfg())

    assert trade.outcome == "CUTOFF"
    assert trade.was_filled is False


def test_open_position_is_flat_by_the_session_close():
    bars = IB + [(150, 150, 90, 95), (95, 150, 95, 150)]
    bars += [(150, 155, 145, 152)] * 70          # never hits stop or target
    trade = simulate_session(_session(bars), _cfg(), _trade_cfg())

    assert trade.outcome == "TIME"
    assert trade.exit_time.time() <= time(15, 20)


def test_costs_and_slippage_always_work_against_the_trade():
    bars = IB + [(150, 150, 90, 95), (95, 150, 95, 150), (150, 205, 150, 200)]
    free = simulate_session(_session(bars), _cfg(), _trade_cfg())
    costed = simulate_session(
        _session(bars), _cfg(),
        _trade_cfg(costs={"per_side_pct": 0.0003, "slippage_frac": 0.02}),
    )

    assert costed.points < free.points


def test_costs_make_a_loss_worse_not_better():
    bars = IB + [(150, 150, 90, 95), (95, 150, 95, 150), (150, 150, 85, 88)]
    free = simulate_session(_session(bars), _cfg(), _trade_cfg())
    costed = simulate_session(
        _session(bars), _cfg(),
        _trade_cfg(costs={"per_side_pct": 0.0003, "slippage_frac": 0.02}),
    )

    assert costed.points < free.points < 0


def test_session_without_a_break_yields_no_setup():
    trade = simulate_session(_session(IB + [(150, 155, 145, 150)] * 5), _cfg(), _trade_cfg())
    assert trade.outcome == "NO_BREAK"
    assert trade.direction is None


def test_continuation_mode_joins_the_break_instead_of_fading_it():
    bars = IB + [(150, 210, 150, 205), (205, 205, 175, 180)]
    trade = simulate_session(_session(bars), _cfg(), _trade_cfg(mode="CONTINUATION"))

    assert trade.broke_first == "HIGH"
    assert trade.direction == "LONG"        # joins the upside break
    assert trade.stop == 150                # the midpoint
    assert trade.target == 250              # 0.5x range extension


# -- aggregation --------------------------------------------------------


def _mk(outcome, points, r):
    from quant_backtester.src.ib_backtest import Trade

    t = Trade(session_date=date(2026, 8, 3), outcome=outcome, points=points, r_multiple=r)
    if outcome in ("TARGET", "STOP", "TIME", "AMBIGUOUS"):
        t.fill_time = pd.Timestamp("2026-08-03 10:30", tz=TZ)
    return t


def test_summary_excludes_ambiguous_trades_from_pnl():
    trades = [
        _mk("TARGET", 50, 1.0), _mk("STOP", -50, -1.0),
        _mk("AMBIGUOUS", 0, 0.0), _mk("NO_FILL", 0, 0.0),
    ]
    s = summarize_trades(trades)

    assert s["filled"] == 2
    assert s["ambiguous"] == 1
    assert s["no_fill"] == 1
    assert s["total_points"] == 0.0


def test_summary_reports_a_confidence_interval_on_the_win_rate():
    trades = [_mk("TARGET", 50, 1.0) for _ in range(6)] + [
        _mk("STOP", -50, -1.0) for _ in range(4)
    ]
    s = summarize_trades(trades)

    assert s["win_rate"] == 60.0
    lo, hi = s["win_rate_ci"]
    assert lo < 60 < hi
    assert lo < 50, "10 trades cannot establish a 60% win rate"


def test_summary_of_nothing_does_not_divide_by_zero():
    s = summarize_trades([])
    assert s["filled"] == 0 and s["total_points"] == 0.0
    assert np.isnan(s["win_rate"])


def test_profit_factor_and_expectancy_are_consistent_with_the_trades():
    trades = [_mk("TARGET", 100, 2.0), _mk("TARGET", 50, 1.0), _mk("STOP", -50, -1.0)]
    s = summarize_trades(trades)

    assert s["profit_factor"] == pytest.approx(150 / 50)
    assert s["expectancy_r"] == pytest.approx(2.0 / 3, abs=1e-3)


def test_a_setup_with_no_risk_is_rejected_not_reported_as_infinite_r():
    """Entry on top of the stop gives risk ~0 and an absurd R multiple.

    An earlier CONTINUATION geometry did exactly this and reported +17 R per
    trade — a configuration bug wearing the costume of an edge.
    """
    bars = IB + [(150, 150, 90, 95), (95, 150, 95, 150)]
    # Entry exactly at the IB low with no stop buffer: entry == stop.
    trade = simulate_session(
        _session(bars), _cfg(), _trade_cfg(entry_level=0.0, stop_buffer_frac=0.0)
    )
    assert trade.outcome == "DEGENERATE"
    assert trade.r_multiple == 0.0


def test_continuation_entry_sits_between_the_break_and_the_stop():
    bars = IB + [(150, 210, 150, 205), (205, 205, 175, 180)]
    trade = simulate_session(_session(bars), _cfg(), _trade_cfg(mode="CONTINUATION"))

    assert trade.direction == "LONG"
    # A long must have its stop strictly below its entry.
    assert trade.stop < trade.entry < trade.target


def test_a_pullback_entry_below_price_requires_an_actual_pullback():
    """The entry is a limit order: the bar must trade THROUGH the level.

    Testing only "high >= entry" for a long filled every continuation setup
    on the next bar, because price was already above the pullback level.
    """
    bars = IB + [(150, 210, 150, 205)]
    # Price stays well above the pullback entry (~175) and never comes back.
    bars += [(205, 212, 200, 208)] * 8
    trade = simulate_session(_session(bars), _cfg(), _trade_cfg(mode="CONTINUATION"))

    assert trade.was_filled is False


def test_a_pullback_entry_fills_once_price_trades_back_to_it():
    bars = IB + [(150, 210, 150, 205)]
    bars += [(205, 208, 170, 180)]          # dips through the entry level
    trade = simulate_session(_session(bars), _cfg(), _trade_cfg(mode="CONTINUATION"))

    assert trade.was_filled is True
