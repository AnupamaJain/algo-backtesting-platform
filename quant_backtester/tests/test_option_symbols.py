"""Option symbols, across three different spellings of the same contract.

The exchange calls it NIFTY26O0623450CE. Dhan's scrip master calls it
NIFTY-Oct2026-23450-CE. The terminal, the strategies and the contract
dropdowns all speak the exchange's version, and nothing translated -- so
Dhan could list an option, the dropdown could offer it, and the quote call
still answered "not in the Dhan scrip master". No option could be priced,
which for an options terminal is the whole job.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quant_backtester.src.broker.dhan import DhanInstruments, _nse_contract_prefix
from quant_backtester.src.broker.exceptions import InstrumentNotFound
from quant_backtester.src.broker.flattrade import _split_symbol
from quant_backtester.src.broker.models import InstrumentType

STATE = Path(__file__).resolve().parent.parent / "state"
pytestmark = pytest.mark.skipif(
    not (STATE / "dhan_scrip_master.csv").exists(),
    reason="the scrip master is a cached download, not in the repository",
)


@pytest.fixture(scope="module")
def instruments() -> DhanInstruments:
    return DhanInstruments(STATE)


class TestNseContractPrefix:
    """The exchange writes weekly and monthly contracts differently."""

    def test_a_weekly_month_is_one_character(self):
        from datetime import date
        assert _nse_contract_prefix("NIFTY", date(2026, 10, 6), monthly=False) == "NIFTY26O06"
        assert _nse_contract_prefix("NIFTY", date(2026, 3, 10), monthly=False) == "NIFTY26310"

    def test_a_monthly_month_is_spelled(self):
        from datetime import date
        assert _nse_contract_prefix("NIFTY", date(2026, 10, 27), monthly=True) == "NIFTY26OCT"


class TestResolvingExchangeSymbols:
    def test_a_weekly_call_resolves_to_its_security_id(self, instruments):
        found = instruments.resolve("NIFTY26O0623450CE", exchange="NSE")
        assert found.broker_token == "40757"
        assert found.exchange == "NFO", "an option does not trade on the cash segment"
        assert found.instrument_type is InstrumentType.CALL
        assert found.lot_size == 65

    def test_a_weekly_put_resolves(self, instruments):
        found = instruments.resolve("NIFTY26O0623800PE", exchange="NSE")
        assert found.broker_token == "40782"
        assert found.instrument_type is InstrumentType.PUT

    def test_a_monthly_contract_resolves(self, instruments):
        found = instruments.resolve("NIFTY26OCT23000CE", exchange="NSE")
        assert found.exchange == "NFO"
        assert found.instrument_type is InstrumentType.CALL

    def test_cash_equity_still_resolves(self, instruments):
        """The option branch must not swallow the symbols that already worked."""
        found = instruments.resolve("RELIANCE-EQ", exchange="NSE")
        assert found.exchange == "NSE"
        assert found.instrument_type is InstrumentType.EQUITY

    def test_an_unlisted_contract_is_an_error_not_a_guess(self, instruments):
        """A wrong securityId would place a real order in the wrong instrument."""
        with pytest.raises(InstrumentNotFound):
            instruments.resolve("NIFTY26O0699999CE", exchange="NSE")


class TestFlattradeSegment:
    """Flattrade looked every option up on the cash exchange.

    Its configured exchange stays NSE for a cash account regardless of what
    it is asked to price, so every contract came back "not found".
    """

    def test_an_option_goes_to_the_derivatives_segment(self):
        assert _split_symbol("NIFTY26O0623450CE", "NSE") == ("NFO", "NIFTY26O0623450CE")

    def test_a_sensex_option_goes_to_bfo(self):
        assert _split_symbol("SENSEX2620583200PE", "NSE") == ("BFO", "SENSEX2620583200PE")

    def test_a_future_goes_to_the_derivatives_segment(self):
        assert _split_symbol("NIFTY26OCTFUT", "NSE") == ("NFO", "NIFTY26OCTFUT")

    def test_an_equity_that_ends_in_ce_is_not_an_option(self):
        """RELIANCE ends in "CE"; the strike digits are what distinguish one."""
        assert _split_symbol("RELIANCE", "NSE") == ("NSE", "RELIANCE")
        assert _split_symbol("RELIANCE-EQ", "NSE") == ("NSE", "RELIANCE-EQ")

    def test_an_explicit_qualifier_always_wins(self):
        assert _split_symbol("NSE:RELIANCE-EQ", "NFO") == ("NSE", "RELIANCE-EQ")
