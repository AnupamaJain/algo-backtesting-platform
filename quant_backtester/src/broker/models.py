"""Broker-agnostic data models.

Every value crossing the `BrokerAdapter` boundary uses these types — never a
broker-native dict. That is the whole point of the layer: strategy and
portfolio code must never branch on which broker is connected, so no Kite
`instrument_token`, no Dhan `security_id`, and no Upstox `instrument_key`
appears above the adapter.

Vocabulary differs sharply between brokers (Zerodha calls a carry-forward
position `NRML`, Upstox calls it `D`, NorenApi calls it `M`). Each adapter
translates its broker's spelling to and from the enums here.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """+1 for a buy, -1 for a sell — used for signed quantity maths."""
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class ProductType(str, Enum):
    """Platform-level product vocabulary.

    Adapters map these onto their broker's own codes (Zerodha MIS/CNC/NRML,
    Upstox I/D/CO, NorenApi I/M/C). Strategies only ever see these.
    """

    INTRADAY = "INTRADAY"
    CARRYFORWARD = "CARRYFORWARD"
    DELIVERY = "DELIVERY"
    COVER = "COVER"


class OrderStatus(str, Enum):
    """Normalized order lifecycle.

    Brokers disagree on both names and transition timing — some emit a
    COMPLETE callback before the corresponding OPEN. Consumers should treat
    these as a set of observed states, not a guaranteed ordering.
    """

    PENDING = "PENDING"      # accepted locally, not yet acknowledged
    OPEN = "OPEN"            # live at the exchange
    PARTIAL = "PARTIAL"      # partially filled
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.COMPLETE, OrderStatus.CANCELLED, OrderStatus.REJECTED)

    @property
    def is_working(self) -> bool:
        return self in (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIAL)


class InstrumentType(str, Enum):
    EQUITY = "EQUITY"
    FUTURE = "FUTURE"
    CALL = "CE"
    PUT = "PE"
    CRYPTO = "CRYPTO"


# --------------------------------------------------------------------------
# Instruments
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UnifiedInstrument:
    """An instrument identified canonically, not by any broker's token scheme.

    The canonical key is (underlying, exchange, expiry, strike, option type).
    Each adapter keeps its own mapping from this key to whatever native
    identifier its broker requires, so the same instrument resolves correctly
    no matter who is executing.
    """

    symbol: str                      # canonical trading symbol, e.g. "SPY"
    exchange: str                    # e.g. "NASDAQ", "NSE"
    instrument_type: InstrumentType = InstrumentType.EQUITY
    underlying: str | None = None
    expiry: date | None = None
    strike: float | None = None
    lot_size: int = 1
    tick_size: float = 0.01
    broker_token: str | None = None  # native id, set by the adapter

    @property
    def canonical_key(self) -> str:
        """Stable identity independent of any broker."""
        parts = [
            self.underlying or self.symbol,
            self.exchange,
            self.instrument_type.value,
            self.expiry.isoformat() if self.expiry else "-",
            f"{self.strike:g}" if self.strike is not None else "-",
        ]
        return "|".join(parts)

    @property
    def is_derivative(self) -> bool:
        return self.instrument_type in (
            InstrumentType.FUTURE,
            InstrumentType.CALL,
            InstrumentType.PUT,
        )


# --------------------------------------------------------------------------
# Quotes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UnifiedQuote:
    """A price snapshot. `last_price` is the only field guaranteed present —
    depth and volume are best-effort and vary by broker and feed."""

    symbol: str
    last_price: float
    timestamp: datetime
    bid: float | None = None
    ask: float | None = None
    volume: float | None = None
    previous_close: float | None = None
    # The broker's own native instrument id, when the quote source resolved
    # one (a broker API call, not a cached/synthetic source). Legacy code
    # subscribes a websocket ticker by numeric token, not by symbol string --
    # without this, that subscription silently keys on the symbol text
    # instead, which every broker's ticker protocol rejects or misreads.
    broker_token: str | None = None

    @property
    def mid(self) -> float:
        """Mid price when both sides are known, else the last traded price."""
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.last_price

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def change_pct(self) -> float | None:
        if not self.previous_close:
            return None
        return self.last_price / self.previous_close - 1.0


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------


@dataclass
class UnifiedOrder:
    """An order request and its lifecycle state.

    `order_id` is assigned by whoever accepted the order; `broker_order_id`
    is the broker's own reference, which differs in format across brokers
    (numeric string, UUID, alphanumeric) and is therefore kept as opaque text.
    """

    symbol: str
    side: Side
    quantity: float
    order_type: OrderType = OrderType.MARKET
    product: ProductType = ProductType.INTRADAY
    limit_price: float | None = None
    stop_price: float | None = None

    order_id: str = ""
    broker_order_id: str | None = None
    broker: str = ""
    strategy: str = ""

    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: float = 0.0
    average_price: float | None = None
    status_message: str = ""

    created_at: datetime | None = None
    updated_at: datetime | None = None

    # True when the order was simulated by the safety gate rather than sent.
    dry_run: bool = False
    tags: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("order quantity must be positive")
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and self.limit_price is None:
            raise ValueError(f"{self.order_type.value} order requires a limit_price")
        if self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and self.stop_price is None:
            raise ValueError(f"{self.order_type.value} order requires a stop_price")

    @property
    def remaining_quantity(self) -> float:
        return max(self.quantity - self.filled_quantity, 0.0)

    @property
    def signed_filled_quantity(self) -> float:
        return self.filled_quantity * self.side.sign

    @property
    def notional(self) -> float | None:
        if self.average_price is None:
            return None
        return self.average_price * self.filled_quantity

    def fingerprint(self) -> str:
        """Stable hash of the order's economic intent.

        Used by duplicate detection: two orders with the same fingerprint in
        quick succession are almost certainly the same intent fired twice,
        not two deliberate trades.
        """
        raw = "|".join(
            [
                self.broker,
                self.strategy,
                self.symbol,
                self.side.value,
                f"{self.quantity:g}",
                self.order_type.value,
                f"{self.limit_price:g}" if self.limit_price is not None else "-",
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("side", "order_type", "product", "status"):
            data[key] = getattr(self, key).value
        for key in ("created_at", "updated_at"):
            value = getattr(self, key)
            data[key] = value.isoformat() if value else None
        return data

    def copy_with(self, **changes) -> "UnifiedOrder":
        return replace(self, **changes)


@dataclass(frozen=True)
class Fill:
    """One execution against an order."""

    order_id: str
    symbol: str
    side: Side
    quantity: float
    price: float
    timestamp: datetime
    commission: float = 0.0
    slippage: float = 0.0

    @property
    def signed_quantity(self) -> float:
        return self.quantity * self.side.sign

    @property
    def notional(self) -> float:
        return self.price * self.quantity


# --------------------------------------------------------------------------
# Positions
# --------------------------------------------------------------------------


@dataclass
class UnifiedPosition:
    """A net position with a normalized sign convention.

    Brokers disagree here: some report short positions as a negative quantity,
    others as a positive quantity with a direction flag. Above the adapter,
    quantity is ALWAYS signed — negative means short.
    """

    symbol: str
    quantity: float                 # signed: negative == short
    average_price: float
    broker: str = ""
    strategy: str = ""
    product: ProductType = ProductType.INTRADAY
    last_price: float | None = None
    realized_pnl: float = 0.0
    # The broker's own native instrument id, when resolvable. Several legacy
    # callers read position['instrument_token'] unconditionally (a holdover
    # from Kite, where every position always carries one) -- without this,
    # the shim either crashes them (KeyError) or would have to invent a
    # value. None is the honest answer when the adapter has no cheap way to
    # resolve it; the shim falls back to the symbol string rather than a
    # missing key.
    broker_token: str | None = None

    @property
    def is_flat(self) -> bool:
        return abs(self.quantity) < 1e-9

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def direction(self) -> str:
        if self.is_flat:
            return "FLAT"
        return "LONG" if self.is_long else "SHORT"

    @property
    def market_value(self) -> float:
        price = self.last_price if self.last_price is not None else self.average_price
        return self.quantity * price

    @property
    def unrealized_pnl(self) -> float:
        """Signed P&L, correct for both long and short.

        Because quantity is signed, `(last - avg) * quantity` gives the right
        answer in both directions without a branch: a short position with a
        falling price yields a positive number.
        """
        if self.last_price is None or self.is_flat:
            return 0.0
        return (self.last_price - self.average_price) * self.quantity

    @property
    def unrealized_pnl_pct(self) -> float | None:
        cost = abs(self.average_price * self.quantity)
        if cost == 0:
            return None
        return self.unrealized_pnl / cost

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    def to_dict(self) -> dict:
        data = asdict(self)
        data["product"] = self.product.value
        data.update(
            direction=self.direction,
            market_value=self.market_value,
            unrealized_pnl=self.unrealized_pnl,
            total_pnl=self.total_pnl,
        )
        return data


@dataclass(frozen=True)
class AccountSnapshot:
    """Account-level state, normalized across brokers."""

    broker: str
    cash: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    positions: list[UnifiedPosition] = field(default_factory=list)
    timestamp: datetime | None = None

    @property
    def gross_exposure(self) -> float:
        return sum(abs(p.market_value) for p in self.positions)

    @property
    def net_exposure(self) -> float:
        return sum(p.market_value for p in self.positions)

    @property
    def leverage(self) -> float:
        return self.gross_exposure / self.equity if self.equity > 0 else 0.0
