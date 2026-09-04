"""The risk overlay must cut losses without inventing profits.

These tests exist because a stop-loss is the easiest place in a backtest to
accidentally cheat: exit at a price you never traded, ignore gaps, or peek at
the bar you are supposedly reacting to.
"""

import numpy as np
import pandas as pd
import pytest

from quant_backtester.src.risk import RiskOverlay


def _frame(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    index = pd.date_range("2020-01-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, index=index, columns=["Open", "High", "Low", "Close"])


def _steady(n: int, price: float = 100.0, span: float = 1.0) -> pd.DataFrame:
    """Flat bars with a constant range, so ATR is predictable."""
    return _frame([(price, price + span, price - span, price)] * n)


def test_stop_closes_the_position_and_blocks_immediate_reentry():
    bars = _steady(30)
    # One violent down bar well beyond a 2-ATR stop.
    crash = bars.copy()
    crash.iloc[25] = (100.0, 100.0, 80.0, 82.0)
    position = pd.Series(1.0, index=bars.index)

    result = RiskOverlay(stop_atr=2.0, atr_period=14).apply(crash, position)

    assert result.num_stops == 1
    assert result.exit_reason.iloc[25] == "stop"
    # The signal never changes, so the overlay must not buy straight back in.
    assert (result.position.iloc[26:] == 0).all()


def test_stopped_bar_is_priced_at_the_stop_not_the_close():
    bars = _steady(30)
    bars.iloc[25] = (100.0, 100.0, 90.0, 91.0)
    position = pd.Series(1.0, index=bars.index)

    result = RiskOverlay(stop_atr=2.0, atr_period=14).apply(bars, position)

    booked = result.gross_returns.iloc[25]
    to_close = 91.0 / 100.0 - 1.0
    # The stop sits at 100 - 2*ATR = 96, so the loss booked must be the loss
    # to 96, not the deeper loss to the close.
    assert booked > to_close
    assert booked == pytest.approx(96.0 / 100.0 - 1.0)


def test_a_gap_through_the_stop_fills_at_the_open():
    bars = _steady(30)
    # Opens below the stop: there was never a chance to trade at 96.
    bars.iloc[25] = (85.0, 86.0, 80.0, 81.0)
    position = pd.Series(1.0, index=bars.index)

    result = RiskOverlay(stop_atr=2.0, atr_period=14).apply(bars, position)

    assert result.gross_returns.iloc[25] == pytest.approx(85.0 / 100.0 - 1.0)


def test_target_takes_profit_at_the_target_level():
    bars = _steady(30)
    bars.iloc[25] = (100.0, 112.0, 100.0, 111.0)
    position = pd.Series(1.0, index=bars.index)

    result = RiskOverlay(stop_atr=2.0, target_atr=4.0, atr_period=14).apply(bars, position)

    assert result.exit_reason.iloc[25] == "target"
    # ATR is 2.0 on these bars, so the 4x target sits at 108 — reached
    # intrabar (high 112) without gapping past it at the open.
    assert result.gross_returns.iloc[25] == pytest.approx(108.0 / 100.0 - 1.0)


def test_trailing_stop_only_ever_tightens():
    rows = [(100.0, 101.0, 99.0, 100.0)] * 20
    # A steady rally, then a sharp give-back.
    rows += [(100.0 + i, 101.0 + i, 99.0 + i, 100.0 + i) for i in range(1, 11)]
    rows += [(110.0, 110.0, 100.0, 101.0)]
    bars = _frame(rows)
    position = pd.Series(1.0, index=bars.index)

    trailed = RiskOverlay(stop_atr=2.0, trailing=True, atr_period=14).apply(bars, position)
    fixed = RiskOverlay(stop_atr=2.0, trailing=False, atr_period=14).apply(bars, position)

    # The trailing stop must have ratcheted up under the rally and fired on
    # the give-back; the fixed stop, still down at ~98, must not have.
    assert trailed.num_stops == 1
    assert fixed.num_stops == 0


def test_overlay_never_looks_inside_the_bar_it_reacts_to():
    """Changing a bar's future must not change any earlier decision."""
    bars = _steady(40)
    position = pd.Series(1.0, index=bars.index)
    baseline = RiskOverlay(stop_atr=2.0, atr_period=14).apply(bars, position)

    tampered = bars.copy()
    tampered.iloc[30:] = (100.0, 500.0, 1.0, 400.0)
    perturbed = RiskOverlay(stop_atr=2.0, atr_period=14).apply(tampered, position)

    pd.testing.assert_series_equal(
        baseline.position.iloc[:30], perturbed.position.iloc[:30]
    )
    pd.testing.assert_series_equal(
        baseline.gross_returns.iloc[:30], perturbed.gross_returns.iloc[:30]
    )


def test_flat_signal_produces_no_exposure_and_no_return():
    bars = _steady(30)
    position = pd.Series(0.0, index=bars.index)

    result = RiskOverlay(stop_atr=2.0, atr_period=14).apply(bars, position)

    assert (result.position == 0).all()
    assert (result.gross_returns == 0).all()


def test_untouched_position_matches_the_plain_close_to_close_return():
    """With no stop hit, the overlay must be a no-op on returns."""
    rows = [(100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i) for i in range(40)]
    bars = _frame(rows)
    position = pd.Series(1.0, index=bars.index)

    result = RiskOverlay(stop_atr=10.0, atr_period=14).apply(bars, position)
    plain = bars["Close"].pct_change().fillna(0.0)

    assert result.num_stops == 0
    live = result.position != 0
    pd.testing.assert_series_equal(
        result.gross_returns[live], plain[live], check_names=False
    )


def test_short_positions_stop_out_on_upside():
    bars = _steady(30)
    bars.iloc[25] = (100.0, 120.0, 100.0, 119.0)
    position = pd.Series(-1.0, index=bars.index)

    result = RiskOverlay(stop_atr=2.0, atr_period=14).apply(bars, position)

    assert result.exit_reason.iloc[25] == "stop"
    # Short stopped at 104: a loss, so the booked return must be negative.
    assert result.gross_returns.iloc[25] == pytest.approx(-(104.0 / 100.0 - 1.0))


def test_reentry_allowed_once_the_signal_actually_changes():
    bars = _steady(40)
    bars.iloc[20] = (100.0, 100.0, 80.0, 82.0)
    position = pd.Series(1.0, index=bars.index)
    position.iloc[25:30] = 0.0  # signal goes flat, then returns

    result = RiskOverlay(stop_atr=2.0, atr_period=14).apply(bars, position)

    assert result.position.iloc[21] == 0.0
    assert (result.position.iloc[31:] == 1.0).any()


def test_config_switch_is_off_by_default():
    assert RiskOverlay.from_config(None) is None
    assert RiskOverlay.from_config({"enabled": False, "stop_atr": 2}) is None
    overlay = RiskOverlay.from_config({"enabled": True, "stop_atr": 3, "trailing": True})
    assert overlay.stop_atr == 3.0 and overlay.trailing is True


def test_rejects_a_close_only_frame():
    closes = pd.DataFrame({"Close": [1.0, 2.0]}, index=pd.date_range("2020", periods=2))
    with pytest.raises(ValueError, match="OHLC"):
        RiskOverlay().apply(closes, pd.Series(1.0, index=closes.index))
