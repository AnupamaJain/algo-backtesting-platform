"""MockAdapter — a credential-free adapter for CI and offline development.

Distinct from `PaperBroker`, which is a real simulator trading live prices.
This one exists purely so the contract test suite can drive failure paths on
demand: rate limits, rejections, outages. It is never used by the running
application.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from .adapter import BrokerAdapter, BrokerCapabilities
from .auth import NoAuth
from .exceptions import BrokerUnavailable, InstrumentNotFound, OrderRejected, RateLimited
from .models import (
    AccountSnapshot,
    InstrumentType,
    OrderStatus,
    Side,
    UnifiedInstrument,
    UnifiedOrder,
    UnifiedPosition,
    UnifiedQuote,
)


class MockAdapter(BrokerAdapter):
    """In-memory adapter with programmable failures."""

    name = "mock"
    capabilities = BrokerCapabilities(
        supports_gtt=True,
        supports_streaming=False,
        supports_short_selling=True,
        supports_options=True,
        supports_modify=True,
    )

    def __init__(self, prices: dict[str, float] | None = None, config: dict | None = None) -> None:
        super().__init__(NoAuth("mock", {}), config)
        self._prices = dict(prices or {"TEST": 100.0})
        self._orders: dict[str, UnifiedOrder] = {}
        self._positions: dict[str, UnifiedPosition] = {}
        self._cash = float((config or {}).get("starting_cash", 100_000.0))
        # Failure injection for the contract tests.
        self.fail_next_with: Exception | None = None
        self.fail_times: int = 0

    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    def _maybe_fail(self) -> None:
        if self.fail_times > 0 and self.fail_next_with is not None:
            self.fail_times -= 1
            raise self.fail_next_with

    # -- orders ----------------------------------------------------------

    def place_order(self, order: UnifiedOrder) -> UnifiedOrder:
        self._maybe_fail()
        if order.symbol not in self._prices:
            raise InstrumentNotFound(
                f"unknown symbol {order.symbol}", broker=self.name, symbol=order.symbol
            )

        price = self._prices[order.symbol]
        order.order_id = order.order_id or f"MOCK-{uuid.uuid4().hex[:8].upper()}"
        order.broker_order_id = f"B-{order.order_id}"
        order.broker = self.name
        order.status = OrderStatus.COMPLETE
        order.filled_quantity = order.quantity
        order.average_price = price
        order.created_at = order.created_at or datetime.now()
        order.updated_at = datetime.now()
        self._orders[order.order_id] = order

        existing = self._positions.get(order.symbol)
        signed = order.quantity * order.side.sign
        if existing is None:
            self._positions[order.symbol] = UnifiedPosition(
                symbol=order.symbol,
                quantity=signed,
                average_price=price,
                broker=self.name,
            )
        else:
            existing.quantity += signed
        self._cash -= signed * price
        return order

    def cancel_order(self, order_id: str) -> UnifiedOrder:
        order = self._orders.get(order_id)
        if order is None:
            raise OrderRejected(f"unknown order {order_id}", broker=self.name)
        order.status = OrderStatus.CANCELLED
        order.updated_at = datetime.now()
        return order

    def modify_order(
        self,
        order_id: str,
        *,
        quantity: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> UnifiedOrder:
        order = self._orders.get(order_id)
        if order is None:
            raise OrderRejected(f"unknown order {order_id}", broker=self.name)
        if quantity is not None:
            order.quantity = quantity
        if limit_price is not None:
            order.limit_price = limit_price
        if stop_price is not None:
            order.stop_price = stop_price
        order.updated_at = datetime.now()
        return order

    def get_order_status(self, order_id: str) -> UnifiedOrder:
        order = self._orders.get(order_id)
        if order is None:
            raise OrderRejected(f"unknown order {order_id}", broker=self.name)
        return order

    def get_orders(self) -> list[UnifiedOrder]:
        return list(self._orders.values())

    # -- positions and account -------------------------------------------

    def get_positions(self) -> list[UnifiedPosition]:
        for position in self._positions.values():
            position.last_price = self._prices.get(position.symbol)
        return [p for p in self._positions.values() if not p.is_flat]

    def get_account(self) -> AccountSnapshot:
        positions = self.get_positions()
        return AccountSnapshot(
            broker=self.name,
            cash=self._cash,
            equity=self._cash + sum(p.market_value for p in positions),
            realized_pnl=0.0,
            unrealized_pnl=sum(p.unrealized_pnl for p in positions),
            positions=positions,
            timestamp=datetime.now(),
        )

    # -- market data ------------------------------------------------------

    def get_quote(self, symbol: str) -> UnifiedQuote:
        self._maybe_fail()
        if symbol not in self._prices:
            raise InstrumentNotFound(f"unknown symbol {symbol}", broker=self.name, symbol=symbol)
        price = self._prices[symbol]
        return UnifiedQuote(
            symbol=symbol,
            last_price=price,
            timestamp=datetime.now(),
            bid=price * 0.999,
            ask=price * 1.001,
            previous_close=price,
        )

    def get_instruments(self) -> list[UnifiedInstrument]:
        return [
            UnifiedInstrument(symbol=s, exchange="MOCK", instrument_type=InstrumentType.EQUITY)
            for s in self._prices
        ]


# Convenience constructors used by the contract tests.
def failing_adapter(error: Exception, times: int = 1, **kwargs) -> MockAdapter:
    adapter = MockAdapter(**kwargs)
    adapter.fail_next_with = error
    adapter.fail_times = times
    return adapter


def rate_limited_adapter(times: int = 1) -> MockAdapter:
    return failing_adapter(RateLimited("slow down", broker="mock", retry_after=0.01), times)


def unavailable_adapter(times: int = 1) -> MockAdapter:
    return failing_adapter(BrokerUnavailable("broker down", broker="mock"), times)
