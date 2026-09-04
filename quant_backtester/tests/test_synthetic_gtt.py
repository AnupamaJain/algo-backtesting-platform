"""Local trigger orders, for brokers with no native GTT.

The dangerous failure here is a trigger that looks armed but is not — so
these tests focus on persistence, on what happens when placement fails, and
on the direction defaulting that decides whether a stop fires immediately.
"""

from datetime import datetime

import pytest

from quant_backtester.src.broker.exceptions import BrokerError
from quant_backtester.src.broker.models import OrderStatus, Side, UnifiedQuote
from quant_backtester.src.broker.synthetic_gtt import SyntheticGTT, TriggerStore


class FakeAdapter:
    def __init__(self, prices=None, fail=False):
        self.prices = prices or {}
        self.fail = fail
        self.placed = []

    def get_quotes(self, symbols):
        return {
            s: UnifiedQuote(symbol=s, last_price=self.prices[s], timestamp=datetime.now())
            for s in symbols if s in self.prices
        }

    def place_order(self, order):
        if self.fail:
            raise BrokerError("broker rejected it", broker="fake")
        self.placed.append(order)
        return order.copy_with(broker_order_id="OID-1", status=OrderStatus.COMPLETE)


def _gtt(tmp_path, **kw):
    return SyntheticGTT(FakeAdapter(**kw), TriggerStore(tmp_path / "triggers.json"))


# -- arming -------------------------------------------------------------


def test_a_sell_stop_defaults_to_firing_below_the_market(tmp_path):
    """The direction decides whether a stop protects or fires instantly."""
    gtt = _gtt(tmp_path, prices={"X": 100.0})
    gtt.place(symbol="X", side="SELL", quantity=10, trigger_price=90.0)
    assert gtt.active()[0].direction == "BELOW"


def test_a_buy_stop_defaults_to_firing_above_the_market(tmp_path):
    gtt = _gtt(tmp_path, prices={"X": 100.0})
    gtt.place(symbol="X", side="BUY", quantity=10, trigger_price=110.0)
    assert gtt.active()[0].direction == "ABOVE"


def test_nonsense_is_rejected_at_arming_time(tmp_path):
    gtt = _gtt(tmp_path)
    with pytest.raises(ValueError):
        gtt.place(symbol="X", side="SELL", quantity=0, trigger_price=90.0)
    with pytest.raises(ValueError):
        gtt.place(symbol="X", side="SELL", quantity=1, trigger_price=-1)
    with pytest.raises(ValueError):
        gtt.place(symbol="X", side="SELL", quantity=1, trigger_price=90, direction="SIDEWAYS")


# -- firing -------------------------------------------------------------


def test_a_trigger_below_the_market_does_not_fire(tmp_path):
    gtt = _gtt(tmp_path, prices={"X": 100.0})
    gtt.place(symbol="X", side="SELL", quantity=10, trigger_price=90.0)
    assert gtt.check() == []
    assert len(gtt.active()) == 1


def test_a_reached_trigger_becomes_a_real_order(tmp_path):
    gtt = _gtt(tmp_path, prices={"X": 89.0})
    gtt.place(symbol="X", side="SELL", quantity=10, trigger_price=90.0)

    fired = gtt.check()
    assert len(fired) == 1
    assert fired[0]["order_id"] == "OID-1"
    assert gtt.adapter_placed()[0].side is Side.SELL if hasattr(gtt, "adapter_placed") \
        else gtt._adapter.placed[0].side is Side.SELL
    assert gtt.active() == [], "a fired trigger must not stay armed"


def test_a_trigger_fires_only_once(tmp_path):
    gtt = _gtt(tmp_path, prices={"X": 89.0})
    gtt.place(symbol="X", side="SELL", quantity=10, trigger_price=90.0)
    gtt.check()
    assert gtt.check() == [], "already-fired trigger fired again"


def test_a_failed_order_leaves_the_trigger_armed(tmp_path):
    """The condition still holds; disarming would silently drop protection."""
    gtt = _gtt(tmp_path, prices={"X": 89.0}, fail=True)
    gtt.place(symbol="X", side="SELL", quantity=10, trigger_price=90.0)

    fired = gtt.check()
    assert "error" in fired[0]
    assert len(gtt.active()) == 1, "protection was dropped after a failed placement"


def test_a_missing_quote_does_not_fire_or_disarm(tmp_path):
    gtt = _gtt(tmp_path, prices={})
    gtt.place(symbol="X", side="SELL", quantity=10, trigger_price=90.0)
    assert gtt.check() == []
    assert len(gtt.active()) == 1


def test_one_unquotable_symbol_does_not_block_the_others(tmp_path):
    gtt = _gtt(tmp_path, prices={"B": 50.0})
    gtt.place(symbol="A", side="SELL", quantity=1, trigger_price=90.0)
    gtt.place(symbol="B", side="SELL", quantity=1, trigger_price=60.0)

    fired = gtt.check()
    assert [f["symbol"] for f in fired] == ["B"]


# -- persistence --------------------------------------------------------


def test_triggers_survive_a_restart(tmp_path):
    """A restart that silently disarmed every stop would be catastrophic."""
    path = tmp_path / "triggers.json"
    first = SyntheticGTT(FakeAdapter(prices={"X": 100.0}), TriggerStore(path))
    first.place(symbol="X", side="SELL", quantity=10, trigger_price=90.0)

    second = SyntheticGTT(FakeAdapter(prices={"X": 100.0}), TriggerStore(path))
    assert len(second.active()) == 1


def test_an_unreadable_store_refuses_rather_than_reporting_no_stops(tmp_path):
    path = tmp_path / "triggers.json"
    path.write_text("{ not json")
    gtt = SyntheticGTT(FakeAdapter(), TriggerStore(path))
    with pytest.raises(BrokerError, match="unreadable"):
        gtt.active()


def test_cancel_and_modify(tmp_path):
    gtt = _gtt(tmp_path, prices={"X": 100.0})
    tid = gtt.place(symbol="X", side="SELL", quantity=10, trigger_price=90.0)

    gtt.modify(tid, trigger_price=85.0)
    assert gtt.active()[0].trigger_price == 85.0

    gtt.cancel(tid)
    assert gtt.active() == []
    with pytest.raises(BrokerError, match="unknown trigger"):
        gtt.cancel("SGTT-NOPE")


# -- honesty ------------------------------------------------------------


def test_it_declares_that_it_is_not_exchange_resident(tmp_path):
    """A local watcher is strictly weaker than a broker-held GTT, and must
    not be mistaken for one."""
    gtt = _gtt(tmp_path)
    assert SyntheticGTT.is_exchange_resident is False
    described = gtt.describe()
    assert described["exchange_resident"] is False
    assert "not held at the exchange" in described["warning"]


def test_paper_accounts_do_not_share_a_trigger_store():
    """Three paper accounts all report adapter.name == "paper".

    Keying a trigger store on that made paper_india and paper_dhan overwrite
    each other's stops — protection that silently belongs to another book.
    """
    from pathlib import Path

    import yaml

    from quant_backtester.src.broker.adapter import account_key
    from quant_backtester.src.broker.factory import BrokerFactory

    config = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config" / "broker.yaml").read_text()
    )
    factory = BrokerFactory(config, Path("/tmp"), Path("/tmp"))
    keys = {name: account_key(factory.build_adapter(name))
            for name in ("paper", "paper_india", "paper_dhan")}

    assert len(set(keys.values())) == 3, f"accounts share a key: {keys}"
    assert keys["paper_india"] == "paper_india"


def test_account_key_falls_back_when_built_outside_the_factory():
    from quant_backtester.src.broker.adapter import account_key

    class Bare:
        name = "somebroker"

    assert account_key(Bare()) == "somebroker"
