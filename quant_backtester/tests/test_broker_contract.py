"""The broker contract suite.

Every adapter must pass these before it is considered release-ready. They are
parameterized over adapters so a new integration inherits the whole suite by
adding one fixture — which is the point: the contract is executable, not a
document someone hopes was followed.

Nothing here touches the network or needs credentials.
"""

from __future__ import annotations

import pytest

from quant_backtester.src.broker import (
    BrokerAdapter,
    InstrumentNotFound,
    MockAdapter,
    OrderRejected,
    OrderStatus,
    OrderType,
    PaperBroker,
    Side,
    StaticQuotes,
    UnifiedOrder,
)
from quant_backtester.src.broker.auth import Session
from quant_backtester.src.broker.exceptions import BrokerError
from quant_backtester.src.broker.models import UnifiedInstrument
from quant_backtester.src.broker.store import BrokerStore

PRICES = {"SPY": 500.0, "AAPL": 200.0, "TLT": 90.0}


@pytest.fixture
def mock_adapter() -> MockAdapter:
    return MockAdapter(prices=dict(PRICES))


@pytest.fixture
def paper_adapter(tmp_path) -> PaperBroker:
    return PaperBroker(
        store=BrokerStore(tmp_path / "paper.db"),
        quotes=StaticQuotes(dict(PRICES)),
        config={"starting_cash": 1_000_000.0, "universe": list(PRICES)},
    )


class _FakeDhanBackend:
    """An in-memory stand-in for Dhan's REST API.

    It speaks Dhan's wire vocabulary — `netQty`, `orderStatus: TRADED`,
    the nested market-feed envelope — so the adapter's translation layer is
    genuinely exercised. Without this the contract suite could only test
    adapters that already speak the platform's own language, which is the
    half that never breaks.
    """

    def __init__(self, prices: dict, tokens: dict) -> None:
        # Keyed by security id, exactly as the real API is.
        self.prices = {tokens[s]: p for s, p in prices.items()}
        self.names = {tokens[s]: s for s in prices}
        self.client_id = "TEST1"
        self._orders: dict[str, dict] = {}
        self._book: dict[str, dict] = {}
        self._cash = 1_000_000.0
        self._next = 1

    def profile(self):
        return {"dhanClientId": self.client_id}

    def fund_limit(self):
        invested = sum(
            abs(p["netQty"]) * p["costPrice"] for p in self._book.values()
        )
        return {"availabelBalance": self._cash, "utilizedAmount": invested}

    def quote(self, payload):
        segment, tokens = next(iter(payload.items()))
        out = {}
        for token in tokens:
            symbol = str(token)
            if symbol not in self.prices:
                continue
            price = self.prices[symbol]
            out[symbol] = {"last_price": price, "volume": 1000,
                           "ohlc": {"close": price}}
        return {"data": {segment: out}} if out else {}

    def place_order(self, payload):
        symbol = str(payload["securityId"])
        if symbol not in self.prices:
            return {"omsErrorDescription": "unknown security"}
        quantity = int(payload["quantity"])
        buy = payload["transactionType"] == "BUY"
        price = self.prices[symbol]

        order_id = f"D{self._next:08d}"
        self._next += 1
        self._orders[order_id] = {
            "orderId": order_id,
            "tradingSymbol": self.names.get(symbol, symbol),
            "transactionType": payload["transactionType"],
            "quantity": quantity,
            "filledQty": quantity,
            "orderType": payload["orderType"],
            "orderStatus": "TRADED",
            "price": payload.get("price") or price,
            "averageTradedPrice": price,
            "productType": payload.get("productType", "INTRADAY"),
            "createTime": "2026-09-02 10:00:00",
        }

        signed = quantity if buy else -quantity
        position = self._book.setdefault(
            symbol, {"tradingSymbol": self.names.get(symbol, symbol),
                     "netQty": 0, "costPrice": price,
                     "realizedProfit": 0.0, "productType": "INTRADAY"}
        )
        position["netQty"] += signed
        position["costPrice"] = price
        position["lastTradedPrice"] = price
        self._cash -= signed * price
        return {"orderId": order_id, "orderStatus": "TRADED"}

    def modify_order(self, order_id, payload):
        if order_id not in self._orders:
            raise BrokerError("order not found", broker="dhan")
        self._orders[order_id].update(
            {"quantity": payload.get("quantity", self._orders[order_id]["quantity"])}
        )
        return {"orderId": order_id}

    def cancel_order(self, order_id):
        if order_id not in self._orders:
            raise BrokerError("order not found", broker="dhan")
        self._orders[order_id]["orderStatus"] = "CANCELLED"
        return {"orderId": order_id}

    def order(self, order_id):
        if order_id not in self._orders:
            raise BrokerError("order not found", broker="dhan")
        return self._orders[order_id]

    def orders(self):
        return list(self._orders.values())

    def trades(self):
        return [
            {"orderId": o["orderId"], "tradingSymbol": o["tradingSymbol"],
             "transactionType": o["transactionType"], "tradedQuantity": o["filledQty"],
             "tradedPrice": o["averageTradedPrice"], "exchangeTime": o["createTime"]}
            for o in self._orders.values() if o["orderStatus"] == "TRADED"
        ]

    def positions(self):
        return [p for p in self._book.values()]


class _FakeDhanInstruments:
    """Numeric security ids, as Dhan really issues them."""

    def __init__(self, known):
        self.tokens = {symbol: str(1000 + i) for i, symbol in enumerate(sorted(known))}

    def resolve(self, symbol, exchange="NSE"):
        if symbol not in self.tokens:
            raise InstrumentNotFound(f"{symbol} not on Dhan", symbol=symbol)
        return UnifiedInstrument(
            symbol=symbol, exchange=exchange,
            broker_token=self.tokens[symbol], lot_size=1,
        )


@pytest.fixture
def dhan_adapter() -> BrokerAdapter:
    from quant_backtester.src.broker.dhan import DhanAdapter, DhanAuth

    adapter = DhanAdapter.__new__(DhanAdapter)
    BrokerAdapter.__init__(
        adapter, DhanAuth("dhan", {}), {"universe": list(PRICES)}
    )
    adapter._exchange = "NSE"
    instruments = _FakeDhanInstruments(PRICES)
    adapter._instruments = instruments
    adapter._client = _FakeDhanBackend(PRICES, instruments.tokens)
    # The token path is covered in test_dhan.py; here the session is given.
    adapter._auth._session = Session(
        broker="dhan", access_token="test-token", user_id="TEST1"
    )
    return adapter


@pytest.fixture(params=["mock", "paper", "dhan"])
def adapter(request, mock_adapter, paper_adapter, dhan_adapter) -> BrokerAdapter:
    """Every contract test runs against every adapter.

    Adding a broker means adding a fixture and a param — the contract is
    executable, so a new integration cannot quietly skip it.
    """
    return {"mock": mock_adapter, "paper": paper_adapter, "dhan": dhan_adapter}[
        request.param
    ]


def _order(symbol="SPY", side=Side.BUY, quantity=10.0, **kwargs) -> UnifiedOrder:
    return UnifiedOrder(symbol=symbol, side=side, quantity=quantity, **kwargs)


# ==========================================================================
# Interface conformance
# ==========================================================================


def test_adapter_declares_identity_and_capabilities(adapter):
    assert adapter.name
    caps = adapter.capabilities.describe()
    assert set(caps) >= {"gtt", "streaming", "short_selling", "options", "modify"}
    assert all(isinstance(v, bool) for v in caps.values())


def test_authenticate_returns_a_usable_session(adapter):
    session = adapter.authenticate()
    assert session.is_valid()
    assert adapter.is_connected()


def test_session_dict_never_leaks_the_token(adapter):
    """Session state is rendered in a dashboard; the token must not travel."""
    payload = adapter.authenticate().to_dict()
    assert "access_token" not in payload
    assert payload["has_token"] is True


def test_describe_is_serializable(adapter):
    import json

    json.dumps(adapter.describe())


# ==========================================================================
# Quotes
# ==========================================================================


def test_get_quote_returns_unified_quote(adapter):
    quote = adapter.get_quote("SPY")
    assert quote.symbol == "SPY"
    assert quote.last_price > 0
    assert quote.mid > 0


def test_unknown_symbol_raises_instrument_not_found(adapter):
    with pytest.raises(InstrumentNotFound):
        adapter.get_quote("NOT_A_REAL_SYMBOL")


def test_get_quotes_skips_unknown_symbols(adapter):
    quotes = adapter.get_quotes(["SPY", "NOT_A_REAL_SYMBOL"])
    assert "SPY" in quotes
    assert "NOT_A_REAL_SYMBOL" not in quotes


# ==========================================================================
# Order placement
# ==========================================================================


def test_market_order_fills_and_returns_ids(adapter):
    placed = adapter.place_order(_order())
    assert placed.order_id
    assert placed.broker_order_id
    assert placed.broker == adapter.name
    assert placed.status is OrderStatus.COMPLETE
    assert placed.filled_quantity == 10.0
    assert placed.average_price and placed.average_price > 0


def test_order_appears_in_order_list(adapter):
    placed = adapter.place_order(_order())
    assert any(o.order_id == placed.order_id for o in adapter.get_orders())


def test_get_order_status_is_authoritative(adapter):
    placed = adapter.place_order(_order())
    fetched = adapter.get_order_status(placed.order_id)
    assert fetched.order_id == placed.order_id
    assert fetched.status is placed.status


def test_unknown_order_status_raises(adapter):
    with pytest.raises(OrderRejected):
        adapter.get_order_status("NOPE-123")


def test_cancelling_unknown_order_raises(adapter):
    with pytest.raises(OrderRejected):
        adapter.cancel_order("NOPE-123")


def test_order_for_unknown_symbol_raises(adapter):
    with pytest.raises(InstrumentNotFound):
        adapter.place_order(_order(symbol="NOT_A_REAL_SYMBOL"))


# ==========================================================================
# Positions — the sign convention is the contract
# ==========================================================================


def test_buy_creates_a_signed_long_position(adapter):
    adapter.place_order(_order(side=Side.BUY, quantity=10))
    position = next(p for p in adapter.get_positions() if p.symbol == "SPY")
    assert position.quantity == 10
    assert position.is_long
    assert position.direction == "LONG"


def test_sell_creates_a_negative_short_position(adapter):
    """Above the adapter, short is ALWAYS a negative quantity — no direction
    flags, no broker-specific conventions."""
    adapter.place_order(_order(side=Side.SELL, quantity=4))
    position = next(p for p in adapter.get_positions() if p.symbol == "SPY")
    assert position.quantity == -4
    assert not position.is_long
    assert position.direction == "SHORT"


def test_buy_then_equal_sell_flattens(adapter):
    adapter.place_order(_order(side=Side.BUY, quantity=5))
    adapter.place_order(_order(side=Side.SELL, quantity=5))
    assert all(p.symbol != "SPY" for p in adapter.get_positions())


def test_positions_are_marked_to_market(adapter):
    adapter.place_order(_order(quantity=10))
    position = next(p for p in adapter.get_positions() if p.symbol == "SPY")
    assert position.last_price is not None
    assert position.market_value != 0


# ==========================================================================
# Account
# ==========================================================================


def test_account_snapshot_shape(adapter):
    snapshot = adapter.get_account()
    assert snapshot.broker == adapter.name
    assert snapshot.equity > 0
    assert isinstance(snapshot.positions, list)


def test_buying_reduces_cash(adapter):
    before = adapter.get_account().cash
    adapter.place_order(_order(quantity=10))
    assert adapter.get_account().cash < before


def test_gross_exposure_counts_both_directions(adapter):
    adapter.place_order(_order(symbol="SPY", side=Side.BUY, quantity=10))
    adapter.place_order(_order(symbol="AAPL", side=Side.SELL, quantity=10))
    snapshot = adapter.get_account()
    # Net nearly cancels; gross must not.
    assert snapshot.gross_exposure > abs(snapshot.net_exposure)


# ==========================================================================
# Instruments
# ==========================================================================


def test_get_instruments_returns_canonical_records(adapter):
    instruments = adapter.get_instruments()
    assert instruments
    assert all(i.canonical_key for i in instruments)


def test_resolve_unknown_instrument_raises(adapter):
    with pytest.raises(InstrumentNotFound):
        adapter.resolve_instrument("NOT_A_REAL_SYMBOL")


# ==========================================================================
# Paper-broker-specific execution semantics
# ==========================================================================


def test_market_buy_crosses_the_spread(paper_adapter):
    """Filling at the mid would flatter every backtest. A buy pays the ask."""
    placed = paper_adapter.place_order(_order(side=Side.BUY, quantity=1))
    quote = paper_adapter.get_quote("SPY")
    assert placed.average_price > quote.mid


def test_market_sell_hits_the_bid(paper_adapter):
    placed = paper_adapter.place_order(_order(side=Side.SELL, quantity=1))
    quote = paper_adapter.get_quote("SPY")
    assert placed.average_price < quote.mid


def test_unmarketable_limit_order_rests(paper_adapter):
    """A buy limit far below the market must not fill."""
    placed = paper_adapter.place_order(
        _order(order_type=OrderType.LIMIT, limit_price=400.0)
    )
    assert placed.status is OrderStatus.OPEN
    assert placed.filled_quantity == 0


def test_resting_limit_fills_when_price_comes_to_it(paper_adapter):
    quotes: StaticQuotes = paper_adapter._quotes
    placed = paper_adapter.place_order(
        _order(order_type=OrderType.LIMIT, limit_price=490.0)
    )
    assert placed.status is OrderStatus.OPEN

    quotes.set_price("SPY", 485.0)  # market trades through the limit
    changed = paper_adapter.poll()

    assert any(o.order_id == placed.order_id for o in changed)
    filled = paper_adapter.get_order_status(placed.order_id)
    assert filled.status is OrderStatus.COMPLETE
    # Never better than the limit: a paper book has no queue priority.
    assert filled.average_price == pytest.approx(490.0)


def test_stop_order_triggers_only_after_the_stop_is_touched(paper_adapter):
    quotes: StaticQuotes = paper_adapter._quotes
    placed = paper_adapter.place_order(
        _order(side=Side.SELL, order_type=OrderType.STOP, stop_price=480.0)
    )
    assert placed.status is OrderStatus.OPEN

    quotes.set_price("SPY", 470.0)
    paper_adapter.poll()
    assert paper_adapter.get_order_status(placed.order_id).status is OrderStatus.COMPLETE


def test_cancelled_order_cannot_be_cancelled_twice(paper_adapter):
    placed = paper_adapter.place_order(
        _order(order_type=OrderType.LIMIT, limit_price=400.0)
    )
    paper_adapter.cancel_order(placed.order_id)
    with pytest.raises(OrderRejected):
        paper_adapter.cancel_order(placed.order_id)


def test_insufficient_cash_is_rejected(tmp_path):
    broke = PaperBroker(
        store=BrokerStore(tmp_path / "broke.db"),
        quotes=StaticQuotes(dict(PRICES)),
        config={"starting_cash": 100.0, "universe": list(PRICES)},
    )
    with pytest.raises(OrderRejected):
        broke.place_order(_order(quantity=100))


def test_short_selling_can_be_disabled(tmp_path):
    no_shorts = PaperBroker(
        store=BrokerStore(tmp_path / "noshort.db"),
        quotes=StaticQuotes(dict(PRICES)),
        config={"starting_cash": 100_000.0, "allow_short": False, "universe": list(PRICES)},
    )
    with pytest.raises(OrderRejected):
        no_shorts.place_order(_order(side=Side.SELL, quantity=5))


def test_state_survives_a_restart(tmp_path):
    """A broker that forgets its positions on restart can double up on risk."""
    db = tmp_path / "persist.db"
    first = PaperBroker(
        BrokerStore(db), StaticQuotes(dict(PRICES)), config={"universe": list(PRICES)}
    )
    first.place_order(_order(quantity=7))

    # A completely fresh process, same database.
    second = PaperBroker(
        BrokerStore(db), StaticQuotes(dict(PRICES)), config={"universe": list(PRICES)}
    )
    position = next(p for p in second.get_positions() if p.symbol == "SPY")
    assert position.quantity == 7
