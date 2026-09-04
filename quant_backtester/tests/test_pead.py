"""PEAD must never trade the announcement it is reacting to.

Most US companies report after the close, so the announcement session's
return already contains the news. A backtest that enters on that session
shows a large, entirely fictitious edge — it is the standard way this
anomaly gets faked. These tests pin the timing down.
"""

import pandas as pd
import pytest

from quant_backtester.src.events import EarningsEvent, first_tradeable_session
from quant_backtester.src.pead import PEADConfig, pead_signals, exposure_stats


def _sessions(n=40, start="2026-01-05"):
    # Business days only, like a real exchange calendar.
    return pd.date_range(start, periods=n, freq="B")


def _prices(n=40, start="2026-01-05"):
    idx = _sessions(n, start)
    return pd.DataFrame({"Close": range(100, 100 + n)}, index=idx)


def _event(when, surprise=10.0, symbol="TEST"):
    return EarningsEvent(
        symbol=symbol, announced_at=pd.Timestamp(when),
        eps_estimate=1.0, eps_reported=1.1, surprise_pct=surprise,
    )


# -- the timing guard ---------------------------------------------------


def test_entry_is_always_after_the_announcement_session():
    sessions = _sessions()
    # Announced during the session on the 9th.
    entry = first_tradeable_session(_event("2026-01-09 16:30"), sessions)
    assert entry > pd.Timestamp("2026-01-09")


def test_a_before_open_announcement_still_waits_a_session():
    """Vendor timestamps are not reliable to the minute, so even a
    before-open report does not trade that same session."""
    sessions = _sessions()
    entry = first_tradeable_session(_event("2026-01-09 07:00"), sessions)
    assert entry > pd.Timestamp("2026-01-09")


def test_a_midnight_timestamp_is_treated_as_after_close():
    """No time information means assume the worst, not the best."""
    assert _event("2026-01-09 00:00").announced_after_close is True


def test_an_announcement_after_the_last_session_yields_no_entry():
    sessions = _sessions(10)
    assert first_tradeable_session(_event("2030-01-01"), sessions) is None


def test_the_signal_is_zero_on_and_before_the_announcement_day():
    prices = _prices()
    signals = pead_signals(prices, [_event("2026-01-09 16:30")], PEADConfig())

    on_or_before = signals[signals.index <= pd.Timestamp("2026-01-09")]
    assert (on_or_before == 0).all(), "traded the announcement session or earlier"


# -- signal construction ------------------------------------------------


def test_a_positive_surprise_goes_long_for_the_holding_period():
    prices = _prices()
    config = PEADConfig(min_surprise_pct=5.0, holding_days=5)
    signals = pead_signals(prices, [_event("2026-01-09 16:30", surprise=12.0)], config)

    assert (signals == 1.0).sum() == 5


def test_a_negative_surprise_goes_short():
    prices = _prices()
    signals = pead_signals(
        prices, [_event("2026-01-09 16:30", surprise=-12.0)],
        PEADConfig(min_surprise_pct=5.0, holding_days=5),
    )
    assert (signals == -1.0).sum() == 5


def test_a_surprise_below_the_threshold_is_ignored():
    prices = _prices()
    signals = pead_signals(
        prices, [_event("2026-01-09 16:30", surprise=1.0)],
        PEADConfig(min_surprise_pct=5.0),
    )
    assert (signals == 0).all()


def test_long_only_mode_skips_negative_surprises():
    prices = _prices()
    config = PEADConfig(min_surprise_pct=5.0, holding_days=5, direction="long_only")
    signals = pead_signals(prices, [_event("2026-01-09 16:30", surprise=-20.0)], config)
    assert (signals == 0).all()


def test_events_without_a_surprise_figure_are_skipped():
    prices = _prices()
    event = EarningsEvent("TEST", pd.Timestamp("2026-01-09"), 1.0, None, None)
    assert (pead_signals(prices, [event], PEADConfig()) == 0).all()


def test_a_later_announcement_overwrites_an_open_window():
    """Consecutive quarters overlap; the newer surprise is better information."""
    prices = _prices(60)
    config = PEADConfig(min_surprise_pct=5.0, holding_days=30)
    signals = pead_signals(
        prices,
        [_event("2026-01-09 16:30", surprise=10.0),
         _event("2026-01-20 16:30", surprise=-10.0)],
        config,
    )
    # The second event flips the direction from its own entry onward.
    after = signals[signals.index > pd.Timestamp("2026-01-21")]
    assert (after.head(5) == -1.0).all()


def test_the_holding_window_does_not_run_past_the_data():
    prices = _prices(12)
    signals = pead_signals(
        prices, [_event("2026-01-15 16:30")], PEADConfig(holding_days=999)
    )
    assert len(signals) == len(prices)
    assert signals.notna().all()


def test_no_events_produces_a_flat_signal():
    prices = _prices()
    assert (pead_signals(prices, [], PEADConfig()) == 0).all()


# -- honesty about exposure ---------------------------------------------


def test_exposure_reports_how_little_time_is_spent_in_the_market():
    """A drift trade is idle most of the year; a Sharpe over a mostly-flat
    series must not be read as a full-time result."""
    prices = _prices(100)
    stats = exposure_stats(
        {"TEST": prices}, {"TEST": [_event("2026-01-09 16:30")]},
        PEADConfig(holding_days=10),
    )
    assert stats["bars_in_market"] == 10
    assert 0 < stats["time_in_market_pct"] < 20


def test_config_rejects_nonsense():
    with pytest.raises(ValueError):
        PEADConfig(holding_days=0)
    with pytest.raises(ValueError):
        PEADConfig(direction="sideways")


def test_capital_is_spread_over_active_names_not_the_whole_universe():
    """Dividing by every symbol, including idle ones, understates the
    strategy by the fraction of the universe holding a position."""
    from quant_backtester.src.pead import pead_portfolio_returns

    class Backtester:
        def run(self, prices, signals):
            class R:  # returns 1% whenever a position is held
                net_returns = (signals != 0).astype(float) * 0.01
            return R()

    prices = {s: _prices() for s in ("A", "B", "C", "D")}
    # Only A ever has an event; B, C, D sit flat throughout.
    cal = {"A": [_event("2026-01-09 16:30")]}
    out = pead_portfolio_returns(prices, cal, PEADConfig(holding_days=5), Backtester())

    live = out[out != 0]
    assert len(live) == 5
    # 1% on the one active name, not 1%/4 spread over idle ones.
    assert live.iloc[0] == pytest.approx(0.01)


def test_a_day_with_no_position_returns_zero_not_nan():
    from quant_backtester.src.pead import pead_portfolio_returns

    class Backtester:
        def run(self, prices, signals):
            class R:
                net_returns = (signals != 0).astype(float) * 0.01
            return R()

    prices = {"A": _prices()}
    out = pead_portfolio_returns(
        prices, {"A": [_event("2026-01-09 16:30")]}, PEADConfig(holding_days=3), Backtester()
    )
    assert out.notna().all()
    assert (out == 0).any(), "flat days must be zero, not dropped"
