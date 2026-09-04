"""PaperBroker — a real broker adapter that trades against live prices with
simulated money.

This is not a mock. It fetches actual market quotes, fills against them with
configurable slippage and commission, keeps a durable ledger of every order
and fill, and derives positions and P&L from that ledger. The only thing that
is not real is the money.

That distinction matters for the whole system: because PaperBroker implements
the same `BrokerAdapter` interface as a live broker and passes the same
contract tests, code proven against it needs no changes to trade for real —
only a different adapter in config.

Fill model:
  * MARKET orders fill immediately at the current quote, crossing the spread
    (buys pay the ask, sells hit the bid) plus a slippage allowance. Getting
    the mid price would flatter every result.
  * LIMIT orders rest until the market trades through them, then fill at the
    limit — never better, since a paper book has no queue priority to claim.
  * STOP orders trigger on the stop being touched, then fill like a market
    order, including the slippage that makes stops expensive in fast markets.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from .adapter import BrokerAdapter, BrokerCapabilities
from .auth import AuthStrategy, NoAuth
from .exceptions import InstrumentNotFound, InsufficientFunds, OrderRejected
from .models import (
    AccountSnapshot,
    Fill,
    InstrumentType,
    OrderStatus,
    OrderType,
    Side,
    UnifiedInstrument,
    UnifiedOrder,
    UnifiedPosition,
    UnifiedQuote,
)
from .quotes import QuoteProvider
from .store import BrokerStore

logger = logging.getLogger(__name__)


class PaperBroker(BrokerAdapter):
    """Simulated execution against real market data."""

    name = "paper"
    is_simulated = True
    capabilities = BrokerCapabilities(
        supports_gtt=False,
        supports_streaming=False,
        supports_short_selling=True,
        supports_options=False,
        supports_modify=True,
        fractional_quantities=True,
    )

    def __init__(
        self,
        store: BrokerStore,
        quotes: QuoteProvider,
        auth: AuthStrategy | None = None,
        config: dict | None = None,
    ) -> None:
        super().__init__(auth or NoAuth("paper", {}), config)
        self._store = store
        self._quotes = quotes
        cfg = self.config
        self._starting_cash = float(cfg.get("starting_cash", 100_000.0))
        self._commission_pct = float(cfg.get("commission_pct", 0.0005))
        self._slippage_pct = float(cfg.get("slippage_pct", 0.0002))
        self._allow_short = bool(cfg.get("allow_short", True))
        # Refuse to fill against a quote older than this. A paper fill at a
        # two-day-old close during an open market is not a simulation of
        # anything — it books P&L against a price that was never available.
        # 0 disables the check (useful for replaying history deliberately).
        self._max_quote_age_seconds = float(cfg.get("max_quote_age_seconds", 900))
        self._store.init_account(self.name, self._starting_cash)

    # -- orders ----------------------------------------------------------

    def place_order(self, order: UnifiedOrder) -> UnifiedOrder:
        """Accept an order, then fill it immediately if it is marketable."""
        order.order_id = order.order_id or f"PAPER-{uuid.uuid4().hex[:12].upper()}"
        order.broker_order_id = order.order_id
        order.broker = self.name
        order.created_at = order.created_at or datetime.now()
        order.updated_at = datetime.now()

        quote = self._require_quote(order.symbol)
        self._validate(order, quote)
        self._reject_stale(order, quote)

        order.status = OrderStatus.OPEN
        self._store.save_order(order)
        self._store.log_event(
            "order_placed",
            f"{order.side.value} {order.quantity:g} {order.symbol} "
            f"({order.order_type.value})",
            detail={"order_id": order.order_id, "strategy": order.strategy},
        )

        self._try_fill(order, quote)
        return order

    def _reject_stale(self, order: UnifiedOrder, quote) -> None:
        """Refuse a fill priced off a quote that is too old to be real.

        The quote provider falls back to the last cached close when the live
        feed is unreachable, which is right for *marking* an existing book
        but wrong for *executing*: it invents a fill at a price nobody could
        have traded. Better to reject loudly than to record fictitious P&L.
        """
        if not self._max_quote_age_seconds:
            return
        age = getattr(quote, "age_seconds", None)
        if age is None:
            timestamp = getattr(quote, "timestamp", None)
            if timestamp is None:
                return
            age = (datetime.now() - timestamp).total_seconds()
        if age > self._max_quote_age_seconds:
            raise OrderRejected(
                f"quote for {order.symbol} is {age / 60:.0f} minutes old "
                f"(limit {self._max_quote_age_seconds / 60:.0f}); refusing to "
                "fill against a price that was not available. The market data "
                "feed is probably down — check the broker session.",
                broker=self.name,
            )

    def cancel_order(self, order_id: str) -> UnifiedOrder:
        order = self._store.get_order(order_id)
        if order is None:
            raise OrderRejected(f"unknown order {order_id}", broker=self.name)
        if order.status.is_terminal:
            raise OrderRejected(
                f"order {order_id} is already {order.status.value}", broker=self.name
            )
        order.status = OrderStatus.CANCELLED
        order.status_message = "cancelled by operator"
        order.updated_at = datetime.now()
        self._store.save_order(order)
        self._store.log_event("order_cancelled", f"cancelled {order_id}")
        return order

    def modify_order(
        self,
        order_id: str,
        *,
        quantity: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> UnifiedOrder:
        order = self._store.get_order(order_id)
        if order is None:
            raise OrderRejected(f"unknown order {order_id}", broker=self.name)
        if order.status.is_terminal:
            raise OrderRejected(
                f"cannot modify a {order.status.value} order", broker=self.name
            )
        if quantity is not None:
            if quantity <= order.filled_quantity:
                raise OrderRejected(
                    "new quantity must exceed the amount already filled", broker=self.name
                )
            order.quantity = quantity
        if limit_price is not None:
            order.limit_price = limit_price
        if stop_price is not None:
            order.stop_price = stop_price
        order.updated_at = datetime.now()
        self._store.save_order(order)

        # An amended resting order may now be marketable.
        self._try_fill(order, self._require_quote(order.symbol))
        return order

    def get_order_status(self, order_id: str) -> UnifiedOrder:
        order = self._store.get_order(order_id)
        if order is None:
            raise OrderRejected(f"unknown order {order_id}", broker=self.name)
        return order

    def get_orders(self) -> list[UnifiedOrder]:
        return self._store.list_orders()

    def get_fills(self) -> list[Fill]:
        return self._store.list_fills()

    def poll(self) -> list[UnifiedOrder]:
        """Re-check every resting order against the current market.

        This is the paper equivalent of a broker's order-update stream: limit
        and stop orders can only become fillable when the price moves, and
        nothing here is event-driven, so the console calls this before
        rendering. Returns the orders whose state changed.
        """
        changed: list[UnifiedOrder] = []
        for order in self._store.list_working_orders():
            try:
                quote = self._require_quote(order.symbol)
            except InstrumentNotFound:
                continue
            before = (order.status, order.filled_quantity)
            self._try_fill(order, quote)
            if (order.status, order.filled_quantity) != before:
                changed.append(order)
        return changed

    # -- positions and account -------------------------------------------

    def get_positions(self) -> list[UnifiedPosition]:
        positions = [p for p in self._store.compute_positions(self.name) if not p.is_flat]
        quotes = self._quotes.get_quotes([p.symbol for p in positions])
        for position in positions:
            quote = quotes.get(position.symbol)
            if quote is not None:
                position.last_price = quote.last_price
        return positions

    def get_account(self) -> AccountSnapshot:
        cash, realized = self._store.get_account(self.name)
        positions = self.get_positions()
        unrealized = sum(p.unrealized_pnl for p in positions)
        # Equity = cash + the mark-to-market value of what is held. For a
        # short, market_value is negative, which correctly reduces equity as
        # the position moves against the account.
        equity = cash + sum(p.market_value for p in positions)
        return AccountSnapshot(
            broker=self.name,
            cash=cash,
            equity=equity,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            positions=positions,
            timestamp=datetime.now(),
        )

    # -- market data ------------------------------------------------------

    def get_quote(self, symbol: str) -> UnifiedQuote:
        return self._quotes.get_quote(symbol)

    def get_instruments(self) -> list[UnifiedInstrument]:
        """Whatever the quote provider can price.

        The paper broker has no instrument master of its own; anything with a
        live quote is tradable. Configured symbols are listed so the console
        has a universe to offer.
        """
        symbols = self.config.get("universe", [])
        return [
            UnifiedInstrument(
                symbol=symbol,
                exchange=self.config.get("exchange", "SIM"),
                instrument_type=(
                    InstrumentType.CRYPTO if symbol.endswith("-USD") else InstrumentType.EQUITY
                ),
            )
            for symbol in symbols
        ]

    # -- internals --------------------------------------------------------

    def _require_quote(self, symbol: str) -> UnifiedQuote:
        try:
            return self._quotes.get_quote(symbol)
        except Exception as exc:  # noqa: BLE001
            raise InstrumentNotFound(
                f"cannot price {symbol}: {exc}", broker=self.name, symbol=symbol
            ) from exc

    def _validate(self, order: UnifiedOrder, quote: UnifiedQuote) -> None:
        if order.side is Side.SELL and not self._allow_short:
            existing = {p.symbol: p.quantity for p in self._store.compute_positions(self.name)}
            if existing.get(order.symbol, 0.0) < order.quantity:
                raise OrderRejected(
                    f"short selling is disabled and there is no long position in "
                    f"{order.symbol} to sell",
                    broker=self.name,
                )
        if order.side is Side.BUY:
            cash, _ = self._store.get_account(self.name)
            estimated = order.quantity * quote.last_price * (1 + self._slippage_pct)
            if estimated > cash:
                raise InsufficientFunds(
                    f"buying {order.quantity:g} {order.symbol} needs "
                    f"~{estimated:,.2f} but only {cash:,.2f} is available",
                    broker=self.name,
                )

    def _fillable_price(self, order: UnifiedOrder, quote: UnifiedQuote) -> float | None:
        """The price this order would fill at now, or None if it must wait."""
        if order.order_type is OrderType.MARKET:
            return self._crossed_price(order.side, quote)

        if order.order_type is OrderType.LIMIT:
            # A buy limit fills only when the market trades at or below it.
            if order.side is Side.BUY and quote.last_price <= order.limit_price:
                return order.limit_price
            if order.side is Side.SELL and quote.last_price >= order.limit_price:
                return order.limit_price
            return None

        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            triggered = (
                quote.last_price >= order.stop_price
                if order.side is Side.BUY
                else quote.last_price <= order.stop_price
            )
            if not triggered:
                return None
            if order.order_type is OrderType.STOP:
                return self._crossed_price(order.side, quote)
            return order.limit_price
        return None

    def _crossed_price(self, side: Side, quote: UnifiedQuote) -> float:
        """Marketable price: cross the spread, then pay slippage.

        Filling at the mid would understate cost on every single trade, which
        compounds into a materially optimistic equity curve.
        """
        if side is Side.BUY:
            base = quote.ask if quote.ask else quote.last_price
            return base * (1 + self._slippage_pct)
        base = quote.bid if quote.bid else quote.last_price
        return base * (1 - self._slippage_pct)

    def _try_fill(self, order: UnifiedOrder, quote: UnifiedQuote) -> None:
        price = self._fillable_price(order, quote)
        if price is None:
            return

        quantity = order.remaining_quantity
        if quantity <= 0:
            return

        commission = abs(price * quantity) * self._commission_pct
        slippage_cost = abs(price * quantity) * self._slippage_pct

        fill = Fill(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            price=price,
            timestamp=datetime.now(),
            commission=commission,
            slippage=slippage_cost,
        )
        self._store.save_fill(fill)

        previously_filled = order.filled_quantity
        order.filled_quantity += quantity
        order.average_price = (
            price
            if previously_filled == 0
            else (
                (order.average_price or price) * previously_filled + price * quantity
            )
            / order.filled_quantity
        )
        order.status = OrderStatus.COMPLETE
        order.status_message = "filled"
        order.updated_at = datetime.now()
        self._store.save_order(order)

        # Cash moves opposite to the trade direction, and commission always
        # comes out regardless of side.
        cash, realized = self._store.get_account(self.name)
        cash -= fill.signed_quantity * price
        cash -= commission
        realized_after = sum(p.realized_pnl for p in self._store.compute_positions(self.name))
        self._store.update_account(self.name, cash, realized_after)

        self._store.log_event(
            "order_filled",
            f"{order.side.value} {quantity:g} {order.symbol} @ {price:,.4f}",
            detail={
                "order_id": order.order_id,
                "commission": round(commission, 4),
                "strategy": order.strategy,
            },
        )
        logger.info(
            "Paper fill: %s %g %s @ %.4f (order %s)",
            order.side.value,
            quantity,
            order.symbol,
            price,
            order.order_id,
        )

    def reset(self) -> None:
        """Wipe the paper account back to its starting cash."""
        self._store.reset()
        self._store.init_account(self.name, self._starting_cash)
        self._store.log_event("account_reset", "paper account reset", severity="warning")
