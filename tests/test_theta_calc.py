"""Theta on the NIFTY positions summary.

These tests used to hand a MagicMock broker to the summary and populate
``mock_kite.instruments``. Instruments no longer come from the broker: since
the instrument cache landed, ``get_all_nifty_instruments`` reads the local
SQLite dump and takes its client argument only to keep old call sites
working (it is named ``_kite``). The mock was therefore never consulted, the
summary saw an empty contract table, every position was skipped for an
unknown token, and theta came back 0.0 -- which the test read as a theta
bug rather than as its own staleness.

They now inject through the seams the code actually has:
``prefetched_instruments`` for the summary, and the instrument cache itself
for the lookup.
"""

import datetime
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/../")

from positions_lib import get_all_nifty_instruments, get_nifty_positions_summary


def _instrument(symbol: str, kind: str, expiry: datetime.date, days: int) -> dict:
    return {
        "tradingsymbol": symbol,
        "expiry": expiry,
        "strike": 23000,
        "instrument_type": kind,
        "segment": "NFO-OPT",
        "exchange": "NFO",
        "days_to_expiry": days,
    }


def test_theta_calculation():
    """A short straddle decays in the holder's favour, so theta is positive."""
    today = datetime.date.today()
    expiry = today + datetime.timedelta(days=5)

    instruments = {
        123: _instrument("NIFTY24JAN23000CE", "CE", expiry, 5),
        124: _instrument("NIFTY24JAN23000PE", "PE", expiry, 5),
    }

    kite = MagicMock()
    kite.positions.return_value = {
        "net": [
            {"instrument_token": 123, "tradingsymbol": "NIFTY24JAN23000CE",
             "quantity": -50, "average_price": 100, "last_price": 95, "pnl": 250,
             "day_buy_quantity": 0, "day_sell_quantity": 0},
            {"instrument_token": 124, "tradingsymbol": "NIFTY24JAN23000PE",
             "quantity": -50, "average_price": 100, "last_price": 95, "pnl": 250,
             "day_buy_quantity": 0, "day_sell_quantity": 0},
        ]
    }
    kite.quote.return_value = {"NSE:NIFTY 50": {"last_price": 23000}}

    summary = get_nifty_positions_summary(kite, prefetched_instruments=instruments)

    for key in ("total_theta", "ce_theta", "pe_theta", "expiry_theta_map"):
        assert key in summary

    assert summary["total_theta"] > 0
    assert summary["ce_theta"] > 0
    assert summary["pe_theta"] > 0
    assert summary["positions"], "both legs should have been priced"
    for pos in summary["positions"]:
        assert pos["theta"] > 0


def test_days_to_expiry_counts_calendar_days_not_trading_days():
    """Wednesday to the Monday after is five days, not three.

    Holding over a weekend still costs two days of premium, so counting
    trading days would understate decay on every Friday position.
    """
    today = datetime.date(2026, 4, 1)      # a Wednesday
    expiry = datetime.date(2026, 4, 6)     # the Monday after

    cached = {
        123: {
            "tradingsymbol": "NIFTY26APR23000CE",
            "expiry": expiry,
            "strike": 23000,
            "instrument_type": "CE",
            "segment": "NFO-OPT",
            "exchange": "NFO",
        }
    }

    with patch("instrument_cache.get_all_fut_opt_instruments", return_value=cached), \
         patch("positions_lib.datetime") as clock:
        clock.date.today.return_value = today
        clock.date.fromisoformat = datetime.date.fromisoformat
        instruments = get_all_nifty_instruments(MagicMock())

    assert instruments[123]["days_to_expiry"] == 5


if __name__ == "__main__":
    pytest.main([__file__])
