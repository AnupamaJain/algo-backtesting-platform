"""Live IB-60 execution.

The failure that matters most is a restart re-entering a position already
held: at 10:20 that is a doubled position, not a fresh one. State
persistence and idempotency are therefore the bulk of these tests.
"""

from datetime import date, time

import pandas as pd
import pytest

from quant_backtester.src.ib_backtest import IBTradeConfig
from quant_backtester.src.intraday import IntradayConfig
from quant_backtester.src.orbis_live import OrbisLiveEngine, SessionState, StateStore

TZ = "Asia/Kolkata"


def _cfg(tmp_path):
    return IntradayConfig(
        provider="flattrade", exchange="NSE", auth={}, interval="5",
        cache_dir=tmp_path, max_days_per_request=30, timezone=TZ,
        session_open=time(9, 15), session_close=time(15, 30), ib_minutes=60,
        break_measure="WICK", straddle_policy="ambiguous",
        instruments=[{"symbol": "Nifty 50", "alias": "NIFTY"}],
        lookback_days=5, confidence_level=0.95, results_dir=tmp_path,
    )


def _trade():
    return IBTradeConfig.from_dict({
        "mode": "REVERSION", "entry_level": 0.5, "stop_buffer_frac": 0.08,
        "target": "FAR_SIDE", "entry_cutoff": "14:30", "exit_at": "15:20",
        "same_bar_policy": "stop_first", "cancel_on_double_break": True,
        "costs": {"per_side_pct": 0.0, "slippage_frac": 0.0},
    })


class FakeData:
    def __init__(self, bars):
        self.bars = bars

    def get_bars(self, alias, start, end, force_refresh=False):
        return self.bars


def _bars(rows, day="2026-09-03"):
    idx = pd.date_range(f"{day} 09:15", periods=len(rows), freq="5min", tz=TZ)
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"])


# A 12-bar IB spanning 100..200 (mid 150, stop 92 after the 8% buffer).
IB = [(150, 200, 100, 150)] + [(150, 160, 140, 150)] * 11


def _engine(tmp_path, rows):
    return OrbisLiveEngine(
        _cfg(tmp_path), _trade(), FakeData(_bars(rows)),
        state_dir=tmp_path / "orbis",
    )


# -- progression through the session -------------------------------------


def test_before_the_ib_completes_nothing_is_armed(tmp_path):
    result = _engine(tmp_path, IB[:6]).run("NIFTY", date(2026, 9, 3))
    assert result["phase"] == "none"
    assert "IB window has not completed" in result["note"]


def test_the_ib_is_reported_once_the_window_closes(tmp_path):
    # The 13th bar is 10:15 — the first OUTSIDE the window, which is how you
    # know the IB is final rather than still forming.
    rows = IB + [(150, 155, 145, 150)]
    result = _engine(tmp_path, rows).run("NIFTY", date(2026, 9, 3))
    assert result["ib_high"] == 200 and result["ib_low"] == 100
    assert "no break yet" in result["note"]


def test_a_still_forming_ib_is_never_armed(tmp_path):
    """At 10:10 the balance is still moving; arming on it trades a level
    that has not settled."""
    result = _engine(tmp_path, IB).run("NIFTY", date(2026, 9, 3))
    assert result["phase"] == "none"
    assert "has not completed" in result["note"]


def test_a_break_arms_the_trade(tmp_path):
    rows = IB + [(150, 150, 90, 95)]   # the 10:15 bar breaks the IB low
    result = _engine(tmp_path, rows).run("NIFTY", date(2026, 9, 3))
    assert result["broke_first"] == "LOW"
    assert result["phase"] == "armed"
    assert result["direction"] == "LONG"
    assert result["entry"] == 150 and result["stop"] == 92


def test_a_retrace_to_the_entry_fills(tmp_path):
    rows = IB + [(150, 150, 90, 95), (95, 150, 95, 150)]
    result = _engine(tmp_path, rows).run("NIFTY", date(2026, 9, 3))
    assert result["phase"] == "filled"
    assert result["fill_price"] == 150


def test_the_stop_closes_the_position(tmp_path):
    rows = IB + [(150, 150, 90, 95), (95, 150, 95, 150), (150, 150, 85, 88)]
    result = _engine(tmp_path, rows).run("NIFTY", date(2026, 9, 3))
    assert result["phase"] == "closed"
    assert result["exit_reason"] == "STOP"


def test_a_bar_holding_both_stop_and_target_takes_the_loss(tmp_path):
    rows = IB + [(150, 150, 90, 95), (95, 150, 95, 150), (150, 205, 85, 150)]
    result = _engine(tmp_path, rows).run("NIFTY", date(2026, 9, 3))
    assert result["exit_reason"] == "STOP"


# -- the restart problem -------------------------------------------------


def test_state_survives_a_restart(tmp_path):
    """A restart that forgot an open position would re-enter it."""
    rows = IB + [(150, 150, 90, 95), (95, 150, 95, 150)]
    first = _engine(tmp_path, rows).run("NIFTY", date(2026, 9, 3))
    assert first["phase"] == "filled"

    second = _engine(tmp_path, rows).run("NIFTY", date(2026, 9, 3))
    assert second["phase"] == "filled"
    assert second["fill_price"] == first["fill_price"]


def test_a_second_pass_does_not_place_a_second_order(tmp_path):
    placed = []

    class Service:
        def place(self, order):
            placed.append(order)
            return order.copy_with(broker_order_id=f"OID-{len(placed)}")

    rows = IB + [(150, 150, 90, 95), (95, 150, 95, 150)]
    cfg, trade = _cfg(tmp_path), _trade()
    for _ in range(3):
        OrbisLiveEngine(
            cfg, trade, FakeData(_bars(rows)), service=Service(),
            state_dir=tmp_path / "orbis",
        ).run("NIFTY", date(2026, 9, 3), dry_run=False)

    assert len(placed) == 1, f"re-entered a held position: {len(placed)} orders"


def test_each_instrument_keeps_its_own_state(tmp_path):
    store = StateStore(tmp_path / "orbis")
    a = SessionState(instrument="NIFTY", session_date="2026-09-03", phase="filled")
    b = SessionState(instrument="BANKNIFTY", session_date="2026-09-03", phase="armed")
    store.save(a)
    store.save(b)

    assert store.load("NIFTY", date(2026, 9, 3)).phase == "filled"
    assert store.load("BANKNIFTY", date(2026, 9, 3)).phase == "armed"


def test_each_session_starts_clean(tmp_path):
    store = StateStore(tmp_path / "orbis")
    store.save(SessionState(instrument="NIFTY", session_date="2026-09-02", phase="closed"))
    assert store.load("NIFTY", date(2026, 9, 3)).phase == "none"


# -- honesty -------------------------------------------------------------


def test_every_result_carries_the_research_verdict(tmp_path):
    """The backtest found no edge; a live runner must not imply otherwise."""
    result = _engine(tmp_path, IB).run("NIFTY", date(2026, 9, 3))
    assert "no edge" in result["evidence"]
    assert "t = −0.01" in result["evidence"] or "-0.01" in result["evidence"]


def test_dry_run_places_nothing(tmp_path):
    placed = []

    class Service:
        def place(self, order):
            placed.append(order)

    rows = IB + [(150, 150, 90, 95), (95, 150, 95, 150)]
    OrbisLiveEngine(
        _cfg(tmp_path), _trade(), FakeData(_bars(rows)), service=Service(),
        state_dir=tmp_path / "orbis",
    ).run("NIFTY", date(2026, 9, 3), dry_run=True)
    assert placed == []
