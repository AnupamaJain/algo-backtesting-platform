"""The legacy shim must be broker-agnostic, and honest about capabilities.

These tests deliberately drive the SAME shim over two different adapters. If
anything vendor-specific leaks back in, the parametrised cases below start
disagreeing with each other.
"""

from datetime import datetime

import pytest

from quant_backtester.src.broker.adapter import BrokerCapabilities
from quant_backtester.src.broker.legacy import LegacyBrokerShim, UnsupportedOperation
from quant_backtester.src.broker.models import (
    AccountSnapshot,
    Fill,
    OrderStatus,
    OrderType,
    ProductType,
    Side,
    UnifiedInstrument,
    UnifiedOrder,
    UnifiedPosition,
    UnifiedQuote,
)


class FakeAdapter:
    """A minimal BrokerAdapter stand-in, configurable per test."""

    name = "fake"
    # Real broker by default: tests of the live-trading gate must exercise
    # the gate, not accidentally bypass it the way a paper adapter would.
    is_simulated = False

    def __init__(self, capabilities: BrokerCapabilities | None = None, **data):
        self.capabilities = capabilities or BrokerCapabilities()
        self.data = data
        self.placed: list[UnifiedOrder] = []
        self.modified: list[tuple] = []
        self.cancelled: list[str] = []
        self.session = type("S", (), {"access_token": "tok", "user_id": "U1"})()

    def authenticate(self):
        return self.session

    def get_quote(self, symbol):
        if symbol in self.data.get("bad_symbols", []):
            raise RuntimeError("no such instrument")
        return UnifiedQuote(
            symbol=symbol, last_price=100.5, timestamp=datetime(2026, 9, 2),
            bid=100.4, ask=100.6, volume=1200, previous_close=99.5,
        )

    def get_orders(self):
        return self.data.get("orders", [])

    def get_order_status(self, order_id):
        return self.data.get("order_status")

    def get_fills(self):
        return self.data.get("fills", [])

    def get_positions(self):
        return self.data.get("positions", [])

    def get_instruments(self):
        return self.data.get("instruments", [])

    def get_account(self):
        return AccountSnapshot(
            broker="fake", cash=75000.0, equity=100000.0,
            realized_pnl=0.0, unrealized_pnl=0.0,
        )

    def place_order(self, order):
        self.placed.append(order)
        return order.copy_with(broker_order_id="OID-1", status=OrderStatus.OPEN)

    def modify_order(self, order_id, **kwargs):
        self.modified.append((order_id, kwargs))
        return self.data.get("order_status")

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return self.data.get("order_status")


def _shim(**kw):
    return LegacyBrokerShim(FakeAdapter(**kw))


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    """Arm live trading for the translation tests.

    Those assert how an order is MAPPED, not whether it is allowed; the gate
    itself is covered by its own tests, which switch this off explicitly.
    """
    from quant_backtester.src.broker import legacy

    monkeypatch.setattr(legacy, "_live_trading_enabled", lambda: True)


# -- the same shim over different brokers -------------------------------


@pytest.mark.parametrize("broker_name", ["flattrade", "paper", "some_future_broker"])
def test_the_shim_works_over_any_adapter(broker_name):
    """Nothing here may depend on which broker is underneath."""
    adapter = FakeAdapter()
    adapter.name = broker_name
    shim = LegacyBrokerShim(adapter)

    assert shim.quote("NSE:SBIN")["NSE:SBIN"]["last_price"] == 100.5
    assert shim.profile()["user_id"] == "U1"
    oid = shim.place_order(tradingsymbol="SBIN-EQ", transaction_type="BUY", quantity=5)
    assert oid == "OID-1"


def test_place_order_translates_into_the_neutral_model():
    shim = _shim()
    shim.place_order(
        tradingsymbol="SBIN-EQ", transaction_type="SELL", quantity=10,
        order_type="LIMIT", price=101.5, product="CNC", tag="mystrat",
    )
    order = shim.adapter.placed[0]

    assert isinstance(order, UnifiedOrder)
    assert order.side is Side.SELL
    assert order.order_type is OrderType.LIMIT
    assert order.product is ProductType.DELIVERY
    assert order.limit_price == 101.5
    assert order.tags["legacy_tag"] == "mystrat"


def test_legacy_order_type_spellings_all_map():
    shim = _shim()
    for spelling, expected in [
        ("MARKET", OrderType.MARKET), ("LIMIT", OrderType.LIMIT),
        ("SL", OrderType.STOP_LIMIT), ("SL-M", OrderType.STOP),
    ]:
        shim.place_order(
            tradingsymbol="X", quantity=1, order_type=spelling,
            # LIMIT/SL orders are validated to require their price, so supply
            # both and let the unused one be ignored.
            price=100.0, trigger_price=99.0,
        )
        assert shim.adapter.placed[-1].order_type is expected


def test_quote_reports_only_the_prices_that_exist():
    """The unified quote has no intraday OHLC; zeros would look like real
    prices to a strategy reading `ohlc.high`."""
    q = _shim().quote("NSE:SBIN")["NSE:SBIN"]

    assert q["ohlc"]["close"] == 99.5
    assert "high" not in q["ohlc"] and "open" not in q["ohlc"]


def test_one_unquotable_symbol_does_not_kill_the_batch():
    shim = _shim(bad_symbols=["BAD"])
    q = shim.quote(["NSE:SBIN", "NSE:BAD"])
    assert "NSE:SBIN" in q and "NSE:BAD" not in q


def test_positions_are_shaped_the_way_legacy_callers_read_them():
    positions = [
        UnifiedPosition(symbol="SBIN-EQ", quantity=10, average_price=100.0,
                        broker="fake", last_price=105.0, realized_pnl=0.0)
    ]
    p = _shim(positions=positions).positions()

    assert set(p) == {"net", "day"}
    assert p["net"][0]["quantity"] == 10
    assert p["net"][0]["unrealised"] == pytest.approx(50.0)


def test_positions_always_carry_an_instrument_token():
    """common_lib.get_position_for_symbol and five siblings read
    position['instrument_token'] unconditionally -- a holdover from Kite,
    where every position always has one. UnifiedPosition has no broker_token
    on most adapters, so a bare KeyError crashed Wave Extractor's first
    tick the moment it had ANY open position. The key must always be
    present, falling back to the symbol when no native token is resolvable.
    """
    positions = [
        UnifiedPosition(symbol="SBIN-EQ", quantity=10, average_price=100.0,
                        broker="fake", last_price=105.0, realized_pnl=0.0)
    ]
    p = _shim(positions=positions).positions()
    assert p["net"][0]["instrument_token"] == "SBIN-EQ"

    positions_with_token = [
        UnifiedPosition(symbol="SBIN-EQ", quantity=10, average_price=100.0,
                        broker="fake", last_price=105.0, realized_pnl=0.0,
                        broker_token="12345")
    ]
    p2 = _shim(positions=positions_with_token).positions()
    assert p2["net"][0]["instrument_token"] == "12345"


def test_orders_and_trades_are_translated():
    order = UnifiedOrder(symbol="SBIN-EQ", side=Side.BUY, quantity=10,
                         order_type=OrderType.LIMIT, limit_price=100.0,
                         broker_order_id="OID-9", status=OrderStatus.COMPLETE,
                         filled_quantity=10, average_price=100.0)
    fills = [Fill(order_id="OID-9", symbol="SBIN-EQ", side=Side.BUY,
                  quantity=10, price=100.0, timestamp=datetime(2026, 9, 2))]
    shim = _shim(orders=[order], fills=fills)

    assert shim.orders()[0]["order_id"] == "OID-9"
    assert shim.orders()[0]["transaction_type"] == "BUY"
    assert shim.orders()[0]["pending_quantity"] == 0
    assert shim.trades()[0]["average_price"] == 100.0


def test_modify_and_cancel_reach_the_adapter():
    shim = _shim()
    shim.modify_order(order_id="OID-1", quantity=5, price=99.0)
    shim.cancel_order(order_id="OID-1")

    assert shim.adapter.modified[0][0] == "OID-1"
    assert shim.adapter.modified[0][1]["limit_price"] == 99.0
    assert shim.adapter.cancelled == ["OID-1"]


def test_margins_come_from_the_account_snapshot():
    assert _shim().margins("equity")["available"]["cash"] == 75000.0


def test_set_access_token_is_a_harmless_noop():
    _shim().set_access_token("anything")   # legacy modules call this at startup


# -- capability gating, not vendor gating -------------------------------


def test_a_broker_without_native_gtt_gets_a_local_trigger(tmp_path, monkeypatch):
    """A stop that exists beats a stop that was refused.

    Previously the shim raised UnsupportedOperation here. That was correct
    while there was no alternative; now there is one (PRD FR-MG4), so the
    caller gets a working trigger and is told what kind it is.
    """
    from quant_backtester.src.broker import legacy
    import quant_backtester.src.config as config_module

    monkeypatch.setattr(config_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(legacy, "_live_trading_enabled", lambda: True)
    shim = _shim()  # capabilities default to supports_gtt=False

    trigger_id = shim.place_gtt(
        symbol="SBIN-EQ", side="SELL", quantity=10, trigger_values=[90.0]
    )
    assert trigger_id.startswith("SGTT-")

    listed = shim.get_gtts()
    assert listed[0]["condition"]["tradingsymbol"] == "SBIN-EQ"
    # The weaker guarantee is declared, not hidden.
    assert listed[0]["exchange_resident"] is False


def test_a_local_trigger_is_still_gated_by_live_trading(tmp_path, monkeypatch):
    from quant_backtester.src.broker import legacy
    import quant_backtester.src.config as config_module

    monkeypatch.setattr(config_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(legacy, "_live_trading_enabled", lambda: False)
    with pytest.raises(legacy.DryRunBlocked):
        _shim().place_gtt(symbol="X", side="SELL", quantity=1, trigger_values=[1.0])


def test_a_native_gtt_broker_is_used_directly(monkeypatch):
    """When the broker has real GTT, no local watcher is involved."""
    from quant_backtester.src.broker import legacy
    from quant_backtester.src.broker.adapter import BrokerCapabilities

    monkeypatch.setattr(legacy, "_live_trading_enabled", lambda: True)
    adapter = FakeAdapter(capabilities=BrokerCapabilities(supports_gtt=True))
    adapter.get_gtts = lambda: [{"id": "NATIVE-1"}]
    adapter.place_gtt = lambda **kw: "NATIVE-1"
    shim = legacy.LegacyBrokerShim(adapter)

    assert shim.place_gtt(symbol="X", side="SELL", quantity=1, trigger_values=[1.0]) == "NATIVE-1"
    assert shim.get_gtts() == [{"id": "NATIVE-1"}]
    # Nothing to poll: the exchange is doing the watching.
    assert shim.check_gtts() == []


def test_mutual_fund_orders_are_out_of_scope():
    with pytest.raises(UnsupportedOperation, match="mutual-fund"):
        _shim().place_mf_order()


def test_historical_data_is_refused_when_the_adapter_cannot_supply_it():
    with pytest.raises(UnsupportedOperation, match="historical"):
        _shim().historical_data("NSE:SBIN", "2026-08-01", "2026-08-02")


def test_instruments_come_from_the_adapter_and_can_be_filtered():
    rows = [
        UnifiedInstrument(symbol="SBIN-EQ", exchange="NSE", broker_token="1"),
        UnifiedInstrument(symbol="X", exchange="BSE", broker_token="2"),
    ]
    shim = _shim(instruments=rows)
    assert len(shim.instruments()) == 2
    assert len(shim.instruments("NSE")) == 1


# -- the dry-run gate ---------------------------------------------------


def test_orders_are_refused_while_live_trading_is_off(monkeypatch):
    """Several legacy modules — early_exit_lib among them — call
    place_order() with no gate of their own. The shim is the last line
    between that code and a real broker."""
    from quant_backtester.src.broker import legacy

    monkeypatch.setattr(legacy, "_live_trading_enabled", lambda: False)
    shim = _shim()
    with pytest.raises(legacy.DryRunBlocked, match="live trading is disabled"):
        shim.place_order(tradingsymbol="SBIN-EQ", transaction_type="BUY", quantity=1)
    assert shim.adapter.placed == [], "nothing may reach the adapter"


def test_orders_go_through_once_live_trading_is_armed(monkeypatch):
    from quant_backtester.src.broker import legacy

    monkeypatch.setattr(legacy, "_live_trading_enabled", lambda: True)
    shim = _shim()
    assert shim.place_order(tradingsymbol="SBIN-EQ", transaction_type="BUY", quantity=1)
    assert len(shim.adapter.placed) == 1


def test_a_missing_config_means_no_live_trading(tmp_path):
    """Fail-safe: absence of a decision is not permission."""
    from quant_backtester.src.broker.legacy import read_live_trading_flag

    assert read_live_trading_flag(tmp_path / "nowhere.ini") is False


def test_an_unreadable_config_means_no_live_trading(tmp_path):
    from quant_backtester.src.broker.legacy import read_live_trading_flag

    broken = tmp_path / "configfile.ini"
    broken.write_text("this is not ini [[[")
    assert read_live_trading_flag(broken) is False


def test_a_config_without_the_safety_key_means_no_live_trading(tmp_path):
    from quant_backtester.src.broker.legacy import read_live_trading_flag

    path = tmp_path / "configfile.ini"
    path.write_text("[other]\nkey = value\n")
    assert read_live_trading_flag(path) is False


def test_the_gate_reads_the_safety_section(tmp_path):
    from quant_backtester.src.broker.legacy import read_live_trading_flag

    path = tmp_path / "configfile.ini"
    path.write_text("[safety]\nlive_trading = true\n")
    assert read_live_trading_flag(path) is True


def test_gtt_placement_is_gated_too(monkeypatch):
    from quant_backtester.src.broker import legacy
    from quant_backtester.src.broker.adapter import BrokerCapabilities

    monkeypatch.setattr(legacy, "_live_trading_enabled", lambda: False)
    adapter = FakeAdapter(capabilities=BrokerCapabilities(supports_gtt=True))
    adapter.place_gtt = lambda *a, **k: "GTT-1"
    with pytest.raises(legacy.DryRunBlocked):
        legacy.LegacyBrokerShim(adapter).place_gtt(symbol="X")


def test_access_token_reflects_the_adapters_real_session():
    """Kite-shaped code (cas_tracker, expiry_trade_lib) reads
    kite.access_token directly to test whether a session exists. The shim
    authenticates at construction rather than via set_access_token(), so
    without this property such a check always reads None and the calling
    code silently concludes "not logged in" forever."""
    adapter = FakeAdapter()
    shim = LegacyBrokerShim(adapter)
    assert shim.access_token == adapter.session.access_token
    assert shim.access_token == "tok"


def test_access_token_is_none_before_authentication():
    adapter = FakeAdapter()
    adapter.session = None
    shim = LegacyBrokerShim(adapter)
    assert shim.access_token is None


def test_a_simulated_broker_is_not_gated_by_live_trading(monkeypatch):
    """Paper mode has no real money to protect. Blocking simulated fills
    there would defeat the entire point of running a strategy in paper
    mode -- observing realistic behaviour, not a wall of DryRunBlocked."""
    from quant_backtester.src.broker import legacy

    monkeypatch.setattr(legacy, "_live_trading_enabled", lambda: False)
    adapter = FakeAdapter()
    adapter.is_simulated = True
    shim = legacy.LegacyBrokerShim(adapter)

    placed = shim.place_order(tradingsymbol="RELIANCE-EQ", transaction_type="BUY", quantity=1)
    assert placed
    assert len(adapter.placed) == 1


def test_a_real_broker_still_stays_gated_regardless_of_simulation_check(monkeypatch):
    """Guard against the fix above accidentally weakening the default case:
    a REAL broker with live_trading off must still refuse."""
    from quant_backtester.src.broker import legacy

    monkeypatch.setattr(legacy, "_live_trading_enabled", lambda: False)
    adapter = FakeAdapter()
    assert adapter.is_simulated is False
    with pytest.raises(legacy.DryRunBlocked):
        legacy.LegacyBrokerShim(adapter).place_order(
            tradingsymbol="RELIANCE-EQ", transaction_type="BUY", quantity=1
        )
