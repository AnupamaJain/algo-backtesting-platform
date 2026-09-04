"""Dhan must be interchangeable with every other broker, and honest about tokens.

The two Dhan-specific hazards are numeric security ids (a wrong one is an
order in the wrong instrument) and non-renewable tokens (a dead one must fail
loudly, not mid-session).
"""

import json
from datetime import datetime, timedelta

import pytest

from quant_backtester.src.broker.adapter import BrokerAdapter
from quant_backtester.src.broker.dhan import (
    DhanAdapter,
    DhanAuth,
    DhanClient,
    _clean,
    decode_token_claims,
    token_expiry,
)
from quant_backtester.src.broker.exceptions import AuthError, InstrumentNotFound
from quant_backtester.src.broker.models import (
    OrderStatus,
    OrderType,
    ProductType,
    Side,
    UnifiedInstrument,
    UnifiedOrder,
)


def _jwt(exp: datetime, client_id: str = "1102901358") -> str:
    """A structurally real JWT with a chosen expiry. Not signed — nothing here
    verifies signatures, and the platform must never rely on that."""
    import base64

    def part(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{part({'alg':'HS256'})}.{part({'exp': int(exp.timestamp()), 'dhanClientId': client_id, 'tokenConsumerType':'APP'})}.sig"


# -- token handling -----------------------------------------------------


def test_expiry_is_read_from_the_token_itself():
    when = datetime.now() + timedelta(days=10)
    assert abs((token_expiry(_jwt(when)) - when).total_seconds()) < 2


def test_claims_survive_a_malformed_token_without_raising():
    assert decode_token_claims("not-a-jwt") == {}
    assert token_expiry("not-a-jwt") is None


def test_an_expired_token_is_refused_with_instructions(tmp_path, monkeypatch):
    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    path = tmp_path / "dhan_token.json"
    path.write_text(json.dumps({"token": _jwt(datetime.now() - timedelta(days=1)),
                                "client_id": "X"}))
    auth = DhanAuth("dhan", {"token_file": str(path)})

    with pytest.raises(AuthError) as excinfo:
        auth.authenticate()
    message = str(excinfo.value)
    assert "expired" in message
    assert "dhan_token.py" in message, "must name the tool that fixes it"


def test_a_valid_token_authenticates(tmp_path, monkeypatch):
    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    path = tmp_path / "dhan_token.json"
    token = _jwt(datetime.now() + timedelta(days=20))
    path.write_text(json.dumps({"token": token, "client_id": "1102901358"}))

    session = DhanAuth("dhan", {"token_file": str(path)}).authenticate()
    assert session.access_token == token
    assert session.user_id == "1102901358"
    assert session.is_valid()


def test_the_environment_wins_over_the_token_file(tmp_path, monkeypatch):
    path = tmp_path / "dhan_token.json"
    path.write_text(json.dumps({"token": _jwt(datetime.now() + timedelta(days=5)),
                                "client_id": "FILE"}))
    env_token = _jwt(datetime.now() + timedelta(days=9), client_id="ENV")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", env_token)
    monkeypatch.setenv("DHAN_CLIENT_ID", "ENV")

    session = DhanAuth("dhan", {"token_file": str(path)}).authenticate()
    assert session.access_token == env_token
    assert session.metadata["source"] == "environment"


def test_refresh_says_plainly_that_it_cannot_refresh():
    with pytest.raises(AuthError, match="cannot be refreshed"):
        DhanAuth("dhan", {}).refresh()


def test_quoted_ini_values_are_unwrapped():
    """A token with its quotes still attached fails auth in a way that looks
    exactly like expiry — the worst kind of bug to debug."""
    assert _clean('"abc"') == "abc"
    assert _clean("'abc'") == "abc"
    assert _clean("  abc  ") == "abc"
    assert _clean(None) == ""
    assert _clean('"') == '"'          # a lone quote is not a pair


# -- instrument resolution ----------------------------------------------


class FakeInstruments:
    def __init__(self, known):
        self.known = known

    def resolve(self, symbol, exchange="NSE"):
        if symbol not in self.known:
            raise InstrumentNotFound(f"{symbol} unknown", symbol=symbol)
        return UnifiedInstrument(
            symbol=symbol, exchange=exchange, broker_token=self.known[symbol], lot_size=1
        )


def _adapter(known=None, **client_responses):
    auth = DhanAuth("dhan", {})
    adapter = DhanAdapter.__new__(DhanAdapter)
    BrokerAdapter.__init__(adapter, auth, {"universe": ["RELIANCE", "MISSING"]})
    adapter._exchange = "NSE"
    adapter._instruments = FakeInstruments(known or {"RELIANCE": "2885"})
    adapter._client = FakeClient(**client_responses)
    return adapter


class FakeClient:
    def __init__(self, **responses):
        self.responses = responses
        self.sent = []
        self.client_id = "1102901358"

    def quote(self, payload):
        self.sent.append(("quote", payload))
        return self.responses.get("quote", {})

    def place_order(self, payload):
        self.sent.append(("place", payload))
        return self.responses.get("place", {"orderId": "112111182198"})

    def orders(self):
        return self.responses.get("orders", [])

    def order(self, oid):
        return self.responses.get("order", {})

    def positions(self):
        return self.responses.get("positions", [])

    def trades(self):
        return self.responses.get("trades", [])

    def fund_limit(self):
        return self.responses.get("funds", {"availabelBalance": 50000, "utilizedAmount": 10000})


def test_an_unknown_symbol_is_an_error_not_a_guessed_security_id():
    with pytest.raises(InstrumentNotFound):
        _adapter().resolve_instrument("NOSUCH")


def test_get_instruments_skips_unresolvable_configured_symbols():
    found = _adapter().get_instruments()
    assert [i.symbol for i in found] == ["RELIANCE"]


def test_place_order_sends_the_numeric_security_id():
    adapter = _adapter()
    order = UnifiedOrder(symbol="RELIANCE", side=Side.BUY, quantity=5,
                         order_type=OrderType.LIMIT, limit_price=1400.0)
    placed = adapter.place_order(order)

    _, payload = adapter._client.sent[-1]
    assert payload["securityId"] == "2885"
    assert payload["transactionType"] == "BUY"
    assert payload["exchangeSegment"] == "NSE_EQ"
    assert payload["orderType"] == "LIMIT"
    assert placed.broker_order_id == "112111182198"


def test_order_types_map_to_dhan_vocabulary():
    adapter = _adapter()
    for our_type, theirs in [
        (OrderType.MARKET, "MARKET"), (OrderType.LIMIT, "LIMIT"),
        (OrderType.STOP, "STOP_LOSS_MARKET"), (OrderType.STOP_LIMIT, "STOP_LOSS"),
    ]:
        adapter.place_order(UnifiedOrder(
            symbol="RELIANCE", side=Side.BUY, quantity=1, order_type=our_type,
            limit_price=100.0, stop_price=99.0))
        assert adapter._client.sent[-1][1]["orderType"] == theirs


def test_a_rejected_order_raises_rather_than_returning_a_blank():
    adapter = _adapter(place={"status": "failed"})
    with pytest.raises(Exception, match="did not return an order id"):
        adapter.place_order(UnifiedOrder(symbol="RELIANCE", side=Side.BUY,
                                         quantity=1, order_type=OrderType.MARKET))


def test_quote_is_unpacked_from_dhans_nested_response():
    adapter = _adapter(quote={"data": {"NSE_EQ": {"2885": {
        "last_price": 1402.5, "volume": 900, "ohlc": {"close": 1395.0}}}}})
    q = adapter.get_quote("RELIANCE")

    assert q.last_price == 1402.5
    assert q.previous_close == 1395.0


def test_positions_ignore_closed_rows():
    adapter = _adapter(positions=[
        {"tradingSymbol": "RELIANCE", "netQty": 10, "costPrice": 1400, "realizedProfit": 0},
        {"tradingSymbol": "TCS", "netQty": 0, "costPrice": 3000, "realizedProfit": 120},
    ])
    positions = adapter.get_positions()
    assert [p.symbol for p in positions] == ["RELIANCE"]


def test_dhan_order_status_maps_into_the_neutral_vocabulary():
    adapter = _adapter(orders=[
        {"orderId": "1", "orderStatus": "TRADED", "transactionType": "BUY",
         "quantity": 10, "filledQty": 10, "orderType": "MARKET", "tradingSymbol": "RELIANCE"},
        {"orderId": "2", "orderStatus": "REJECTED", "transactionType": "SELL",
         "quantity": 5, "orderType": "MARKET", "tradingSymbol": "TCS"},
    ])
    orders = adapter.get_orders()
    assert orders[0].status is OrderStatus.COMPLETE
    assert orders[1].status is OrderStatus.REJECTED
    assert orders[1].side is Side.SELL


def test_account_snapshot_comes_from_the_fund_limit():
    account = _adapter().get_account()
    assert account.cash == 50000.0
    assert account.equity == 60000.0


# -- interchangeability -------------------------------------------------


def test_dhan_declares_its_capabilities_honestly():
    """GTT is on: Dhan's Forever Orders (/forever/orders) are implemented.

    The flag and the implementation must agree — declaring support without
    the methods makes the legacy shim call something that does not exist,
    which is a worse failure than an honest refusal.
    """
    assert DhanAdapter.capabilities.supports_gtt is True
    for method in ("get_gtts", "place_gtt", "modify_gtt", "delete_gtt"):
        assert callable(getattr(DhanAdapter, method, None)), f"missing {method}"
    assert DhanAdapter.capabilities.supports_options is True


def test_gtt_records_are_reshaped_into_the_vocabulary_monitors_read():
    """The legacy GTT monitors read condition.tradingsymbol. A flat Dhan
    record passed through unchanged would leave them unable to find the
    symbol, which reads as "this position has no protection"."""
    adapter = _adapter()
    adapter._client.responses["forever"] = None
    adapter._client.forever_orders = lambda: [{
        "orderId": "FO-1", "orderStatus": "PENDING", "tradingSymbol": "RELIANCE",
        "transactionType": "SELL", "quantity": 5, "triggerPrice": 1200.0,
        "price": 1195.0, "productType": "CNC", "exchangeSegment": "NSE_EQ",
    }]
    gtts = adapter.get_gtts()

    assert gtts[0]["condition"]["tradingsymbol"] == "RELIANCE"
    assert gtts[0]["condition"]["trigger_values"] == [1200.0]
    assert gtts[0]["orders"][0]["transaction_type"] == "SELL"


def test_placing_a_gtt_sends_the_numeric_security_id():
    adapter = _adapter()
    sent = {}
    adapter._client.place_forever = lambda payload: sent.update(payload) or {"orderId": "FO-9"}

    gtt_id = adapter.place_gtt(
        symbol="RELIANCE", side="SELL", quantity=5, trigger_price=1200.0
    )
    assert gtt_id == "FO-9"
    assert sent["securityId"] == "2885"
    assert sent["triggerPrice"] == 1200.0
    assert sent["orderFlag"] == "SINGLE"


def test_a_rejected_gtt_raises_rather_than_returning_blank():
    adapter = _adapter()
    adapter._client.place_forever = lambda payload: {"status": "failed"}
    with pytest.raises(Exception, match="rejected the GTT"):
        adapter.place_gtt(symbol="RELIANCE", side="SELL", quantity=1, trigger_price=1.0)


def test_dhan_implements_the_full_adapter_contract():
    missing = [
        name for name in (
            "place_order", "cancel_order", "modify_order", "get_order_status",
            "get_orders", "get_fills", "get_positions", "get_account",
            "get_quote", "get_instruments", "resolve_instrument",
        )
        if not callable(getattr(DhanAdapter, name, None))
    ]
    assert not missing, f"DhanAdapter is missing {missing}"


def test_the_factory_can_build_dhan_and_flattrade_from_one_config():
    """Broker choice is a config value; nothing else in the platform changes."""
    from pathlib import Path

    import yaml

    from quant_backtester.src.broker.factory import BrokerFactory

    config = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config" / "broker.yaml").read_text()
    )
    factory = BrokerFactory(config, Path("/tmp"), Path("/tmp"))

    assert "dhan" in factory.available()
    assert "flattrade" in factory.available()
    assert isinstance(factory.build_adapter("dhan"), DhanAdapter)


# -- credentials file layout --------------------------------------------


def _write_ini(tmp_path, monkeypatch, body: str):
    """Point the reader at a throwaway configfile.ini."""
    from quant_backtester.src.broker import dhan as dhan_module

    (tmp_path / "configfile.ini").write_text(body)
    fake_repo = tmp_path / "quant_backtester"
    fake_repo.mkdir(exist_ok=True)
    monkeypatch.setattr(dhan_module, "REPO_ROOT", fake_repo, raising=False)
    import quant_backtester.src.config as config_module

    monkeypatch.setattr(config_module, "REPO_ROOT", fake_repo)


def test_the_dhan_section_is_read(tmp_path, monkeypatch):
    from quant_backtester.src.broker.dhan import _from_credentials_file

    _write_ini(tmp_path, monkeypatch, """
[dhan]
client_id = 1102901358
access_token = header.payload.sig
""")
    token, client = _from_credentials_file()
    assert token == "header.payload.sig"
    assert client == "1102901358"


def test_legacy_prefixed_keys_in_another_section_still_work(tmp_path, monkeypatch):
    """A config that was never reorganised must not silently stop working."""
    from quant_backtester.src.broker.dhan import _from_credentials_file

    _write_ini(tmp_path, monkeypatch, """
[flattrade]
api_key = abc
dhan_client_id = 1102901358
dhan_access_token = header.payload.sig
""")
    token, client = _from_credentials_file()
    assert token == "header.payload.sig"
    assert client == "1102901358"


def test_the_dhan_section_wins_over_a_legacy_copy(tmp_path, monkeypatch):
    from quant_backtester.src.broker.dhan import _from_credentials_file

    _write_ini(tmp_path, monkeypatch, """
[flattrade]
dhan_access_token = stale.old.token
dhan_client_id = OLD

[dhan]
access_token = fresh.new.token
client_id = NEW
""")
    token, client = _from_credentials_file()
    assert token == "fresh.new.token"
    assert client == "NEW"


def test_quoted_values_in_the_dhan_section_are_unwrapped(tmp_path, monkeypatch):
    from quant_backtester.src.broker.dhan import _from_credentials_file

    _write_ini(tmp_path, monkeypatch, """
[dhan]
client_id = "1102901358"
access_token = "header.payload.sig"
""")
    token, client = _from_credentials_file()
    assert token == "header.payload.sig"
    assert client == "1102901358"


def test_a_missing_config_is_not_an_error(tmp_path, monkeypatch):
    from quant_backtester.src.broker import dhan as dhan_module
    from quant_backtester.src.broker.dhan import _from_credentials_file
    import quant_backtester.src.config as config_module

    monkeypatch.setattr(config_module, "REPO_ROOT", tmp_path / "nowhere" / "qb")
    assert _from_credentials_file() == ("", "")


def test_the_canonical_token_file_is_found_without_being_configured(tmp_path, monkeypatch):
    """A config that omits token_file must still see the refreshed token.

    Otherwise it falls through to configfile.ini, which holds a stale value
    by design — and the platform quietly trades on last-close fallbacks
    while a perfectly good token sits on disk.
    """
    import json

    from quant_backtester.src.broker.dhan import DhanAuth
    import quant_backtester.src.config as config_module

    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    state = tmp_path / "state"
    state.mkdir()
    token = _jwt(datetime.now() + timedelta(hours=12))
    (state / "dhan_token.json").write_text(
        json.dumps({"token": token, "client_id": "CANON"})
    )
    monkeypatch.setattr(config_module, "REPO_ROOT", tmp_path)

    session = DhanAuth("dhan", {}).authenticate()   # note: no token_file given
    assert session.access_token == token
    assert session.user_id == "CANON"
