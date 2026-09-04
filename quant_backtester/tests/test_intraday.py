"""Intraday session structure must not invent the order of events.

The IB study asks which of two things happened first. An OHLC bar does not
record that, so the whole value of these tests is pinning down what the code
does when the answer is genuinely unknowable — it must say so rather than
pick a side, because picking a side would manufacture the edge the study
exists to measure.
"""

from datetime import date, time

import numpy as np
import pandas as pd
import pytest

from quant_backtester.src.intraday import (
    HIGH,
    LOW,
    IntradayConfig,
    IntradayDataManager,
    analyse_session,
    build_initial_balance,
    first_break,
    normalize_bars,
    run_first_break_study,
    split_sessions,
    wilson_interval,
)

TZ = "Asia/Kolkata"
OPEN = time(9, 15)


def _session(bars, day="2026-08-03", start="09:15", freq="5min"):
    """Build a session frame from (open, high, low, close) tuples."""
    index = pd.date_range(f"{day} {start}", periods=len(bars), freq=freq, tz=TZ)
    return pd.DataFrame(bars, index=index, columns=["Open", "High", "Low", "Close"])


def _cfg(**overrides):
    base = dict(
        provider="flattrade", exchange="NSE", auth={}, interval="5",
        cache_dir=Path_tmp(), max_days_per_request=30, timezone=TZ,
        session_open=OPEN, session_close=time(15, 30), ib_minutes=60,
        break_measure="WICK", straddle_policy="ambiguous",
        instruments=[{"symbol": "Nifty 50", "alias": "NIFTY"}],
        lookback_days=120, confidence_level=0.95, results_dir=Path_tmp(),
    )
    base.update(overrides)
    return IntradayConfig(**base)


def Path_tmp():
    from pathlib import Path
    import tempfile

    return Path(tempfile.mkdtemp())


# -- IB construction ----------------------------------------------------


def test_ib_uses_only_the_opening_window():
    # 12 bars of 5min = the 60min IB; later bars are far outside it.
    bars = [(100, 101, 99, 100)] * 12 + [(100, 500, 1, 100)] * 12
    ib = build_initial_balance(_session(bars), ib_minutes=60, session_open=OPEN)

    assert ib.num_bars == 12
    assert ib.high == 101 and ib.low == 99


def test_ib_boundary_bar_belongs_to_the_period_it_opens():
    """The 10:15 bar opens the post-IB period and must be excluded."""
    bars = [(100, 101, 99, 100)] * 12 + [(100, 200, 50, 100)]
    ib = build_initial_balance(_session(bars), ib_minutes=60, session_open=OPEN)

    assert ib.num_bars == 12
    assert ib.high == 101


def test_formed_first_is_high_when_the_high_bar_comes_earlier():
    bars = [(100, 110, 99, 100)] + [(100, 101, 90, 100)] + [(100, 101, 99, 100)] * 10
    ib = build_initial_balance(_session(bars), 60, OPEN)

    assert ib.formed_first == HIGH


def test_formed_first_is_low_when_the_low_bar_comes_earlier():
    bars = [(100, 101, 90, 100)] + [(100, 110, 99, 100)] + [(100, 101, 99, 100)] * 10
    ib = build_initial_balance(_session(bars), 60, OPEN)

    assert ib.formed_first == LOW


def test_formed_first_is_undefined_when_one_bar_sets_both_extremes():
    """Not a coin flip — the ordering simply is not in the data."""
    bars = [(100, 110, 90, 100)] + [(100, 101, 99, 100)] * 11
    ib = build_initial_balance(_session(bars), 60, OPEN)

    assert ib.formed_first is None


def test_session_with_no_bars_in_the_window_yields_no_ib():
    late = _session([(100, 101, 99, 100)] * 5, start="11:00")
    assert build_initial_balance(late, 60, OPEN) is None


# -- first break --------------------------------------------------------


def test_break_is_only_detected_after_the_ib_window():
    # A bar inside the window exceeds the running high but cannot be a break.
    bars = [(100, 101, 99, 100)] * 11 + [(100, 120, 99, 100)]
    session = _session(bars)
    ib = build_initial_balance(session, 60, OPEN)

    assert first_break(session, ib, OPEN, 60).side is None


def test_high_break_detected():
    bars = [(100, 101, 99, 100)] * 12 + [(100, 105, 100, 104)]
    session = _session(bars)
    ib = build_initial_balance(session, 60, OPEN)

    outcome = first_break(session, ib, OPEN, 60)
    assert outcome.side == HIGH and outcome.is_resolved


def test_low_break_detected_when_it_comes_first():
    bars = [(100, 101, 99, 100)] * 12 + [(100, 100, 95, 96), (100, 110, 100, 109)]
    session = _session(bars)
    ib = build_initial_balance(session, 60, OPEN)

    assert first_break(session, ib, OPEN, 60).side == LOW


def test_a_bar_breaching_both_extremes_is_ambiguous_not_a_guess():
    bars = [(100, 101, 99, 100)] * 12 + [(100, 130, 70, 100)]
    session = _session(bars)
    ib = build_initial_balance(session, 60, OPEN)

    outcome = first_break(session, ib, OPEN, 60)
    assert outcome.side == "AMBIGUOUS"
    assert outcome.is_resolved is False


def test_close_measure_ignores_a_wick_that_does_not_close_beyond():
    bars = [(100, 101, 99, 100)] * 12 + [(100, 105, 100, 100.5)]
    session = _session(bars)
    ib = build_initial_balance(session, 60, OPEN)

    assert first_break(session, ib, OPEN, 60, "WICK").side == HIGH
    assert first_break(session, ib, OPEN, 60, "CLOSE").side is None


def test_quiet_session_never_breaks():
    bars = [(100, 101, 99, 100)] * 24
    session = _session(bars)
    ib = build_initial_balance(session, 60, OPEN)

    assert first_break(session, ib, OPEN, 60).side is None


# -- the study ----------------------------------------------------------


def _outcome(formed, broke, day=1):
    """A session reduced to just the two facts the study reads."""
    bars = [(100, 101, 99, 100)] * 12
    session = _session(bars, day=f"2026-08-{day:02d}")
    ib = build_initial_balance(session, 60, OPEN)
    from quant_backtester.src.intraday import BreakOutcome, InitialBalance, SessionOutcome

    ib = InitialBalance(
        session_date=ib.session_date, high=ib.high, low=ib.low,
        formed_first=formed, high_time=ib.high_time, low_time=ib.low_time,
        num_bars=ib.num_bars,
    )
    brk = BreakOutcome(broke, None, None)
    return SessionOutcome(ib.session_date, ib, brk)


def test_study_excludes_ambiguous_and_reports_the_count():
    sessions = (
        [_outcome(HIGH, LOW, d) for d in range(1, 8)]
        + [_outcome(HIGH, HIGH, d) for d in range(8, 11)]
        + [_outcome(HIGH, "AMBIGUOUS", d) for d in range(11, 15)]
        + [_outcome(HIGH, None, d) for d in range(15, 17)]
    )
    row = run_first_break_study(sessions).set_index("formed_first").loc[HIGH]

    assert row["resolved"] == 10          # 7 low + 3 high
    assert row["ambiguous"] == 4
    assert row["no_break"] == 2
    assert row["pct_broke_low"] == 70.0   # computed on resolved only


def test_study_flags_when_the_interval_still_contains_a_coin_flip():
    """A small sample cannot establish an edge, and must not claim one."""
    sessions = [_outcome(HIGH, LOW, d) for d in range(1, 8)] + [
        _outcome(HIGH, HIGH, d) for d in range(8, 11)
    ]
    row = run_first_break_study(sessions).set_index("formed_first").loc[HIGH]

    assert row["pct_broke_low"] == 70.0
    # 7/10 sounds convincing; the interval says otherwise.
    assert row["ci_low"] < 50 < row["ci_high"]
    assert bool(row["beats_coinflip"]) is False


def test_study_reports_an_edge_once_the_sample_supports_it():
    sessions = [_outcome(HIGH, LOW, 1) for _ in range(69)] + [
        _outcome(HIGH, HIGH, 2) for _ in range(31)
    ]
    row = run_first_break_study(sessions).set_index("formed_first").loc[HIGH]

    assert row["pct_broke_low"] == 69.0
    assert row["ci_low"] > 50
    assert bool(row["beats_coinflip"]) is True


def test_empty_study_does_not_divide_by_zero():
    frame = run_first_break_study([])
    assert len(frame) == 2
    assert frame["resolved"].sum() == 0
    assert not frame["beats_coinflip"].any()


def test_wilson_interval_brackets_the_estimate():
    lo, hi = wilson_interval(69, 100, 0.95)
    assert lo < 0.69 < hi
    assert 0.0 <= lo and hi <= 1.0


def test_wilson_interval_is_empty_for_no_trials():
    lo, hi = wilson_interval(0, 0)
    assert np.isnan(lo) and np.isnan(hi)


# -- bar normalization --------------------------------------------------


def test_normalize_parses_noren_records_into_ordered_bars():
    rows = [
        {"time": "03-08-2026 09:20:00", "into": "100.5", "inth": "101",
         "intl": "99.5", "intc": "100.8", "intv": "1200"},
        {"time": "03-08-2026 09:15:00", "into": "100", "inth": "100.9",
         "intl": "99.8", "intc": "100.5", "intv": "900"},
    ]
    frame = normalize_bars(rows, TZ)

    assert list(frame.index.strftime("%H:%M")) == ["09:15", "09:20"]  # sorted
    assert frame["Close"].iloc[0] == 100.5
    assert str(frame.index.tz) == TZ


def test_normalize_drops_rows_it_cannot_use():
    rows = [
        {"time": "not-a-date", "into": "1", "inth": "1", "intl": "1", "intc": "1"},
        {"time": "03-08-2026 09:15:00", "into": "1", "inth": "1",
         "intl": "1", "intc": None},
        {"time": "03-08-2026 09:20:00", "into": "1", "inth": "2",
         "intl": "0.5", "intc": "1.5"},
    ]
    frame = normalize_bars(rows, TZ)
    assert len(frame) == 1


def test_normalize_of_nothing_is_an_empty_typed_frame():
    frame = normalize_bars([], TZ)
    assert frame.empty and str(frame.index.tz) == TZ


# -- caching ------------------------------------------------------------


def test_manager_caches_and_does_not_refetch(tmp_path):
    calls = []

    def fake(symbol, start, end, interval):
        calls.append(symbol)
        stamps = pd.date_range("2026-08-03 09:15", periods=12, freq="5min", tz=TZ)
        return [
            {"time": t.strftime("%d-%m-%Y %H:%M:%S"), "into": "100", "inth": "101",
             "intl": "99", "intc": "100", "intv": "10"}
            for t in stamps
        ]

    cfg = _cfg(cache_dir=tmp_path)
    mgr = IntradayDataManager(cfg, fetcher=fake)
    day = date(2026, 8, 3)

    first = mgr.get_bars("NIFTY", day, day)
    second = mgr.get_bars("NIFTY", day, day)

    assert len(first) == 12 and len(second) == 12
    assert len(calls) == 1, "second call must be served from cache"


def test_manager_keeps_intraday_cache_separate_from_daily(tmp_path):
    cfg = _cfg(cache_dir=tmp_path)
    mgr = IntradayDataManager(cfg, fetcher=lambda *a, **k: [])
    path = mgr._cache_path("NIFTY")

    # The interval is in the filename: a daily NIFTY.csv can never collide.
    assert path.name == "NIFTY_5m.csv"


def test_split_sessions_groups_by_trading_day():
    idx = pd.date_range("2026-08-03 09:15", periods=4, freq="5min", tz=TZ)
    idx2 = pd.date_range("2026-08-04 09:15", periods=3, freq="5min", tz=TZ)
    frame = pd.DataFrame(
        [[1, 1, 1, 1]] * 7,
        index=idx.append(idx2),
        columns=["Open", "High", "Low", "Close"],
    )
    sessions = split_sessions(frame)

    assert [len(s) for s in sessions] == [4, 3]


def test_analyse_session_notes_a_holiday_shaped_gap():
    late = _session([(100, 101, 99, 100)] * 3, start="14:00")
    outcome = analyse_session(late, _cfg())

    assert outcome.ib is None
    assert "IB window" in outcome.note
