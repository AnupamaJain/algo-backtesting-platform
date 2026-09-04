"""The `BrokerAdapter` interface.

Every broker integration implements exactly this. Nothing above the adapter
layer may import a broker SDK, and no method here accepts or returns a
broker-native object — the unified models in `models.py` are the only
vocabulary that crosses this boundary.

Capability flags matter as much as the methods. Brokers genuinely differ in
what they support: GTT (good-till-triggered) is a Zerodha feature with no
universal equivalent, and streaming quotes are not offered everywhere.
Rather than have callers guess or discover by exception, an adapter declares
what it can do, and features like Early Exit branch on the declaration.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from .auth import AuthStrategy, Session
from .models import (
    AccountSnapshot,
    Fill,
    UnifiedInstrument,
    UnifiedOrder,
    UnifiedPosition,
    UnifiedQuote,
)


@dataclass(frozen=True)
class BrokerCapabilities:
    """What this broker can actually do.

    Declared rather than discovered, so a caller can degrade gracefully
    instead of failing on the first unsupported call.
    """

    supports_gtt: bool = False
    supports_streaming: bool = False
    supports_short_selling: bool = True
    supports_options: bool = False
    supports_modify: bool = True
    supports_bracket_orders: bool = False
    fractional_quantities: bool = False

    def describe(self) -> dict:
        return {
            "gtt": self.supports_gtt,
            "streaming": self.supports_streaming,
            "short_selling": self.supports_short_selling,
            "options": self.supports_options,
            "modify": self.supports_modify,
            "bracket_orders": self.supports_bracket_orders,
            "fractional": self.fractional_quantities,
        }


def account_key(adapter) -> str:
    """The per-account identity, for anything stored on disk.

    Falls back to the adapter kind when a caller constructed the adapter
    directly rather than through the factory.
    """
    return getattr(adapter, "config_name", None) or getattr(adapter, "name", "broker")


class BrokerAdapter(ABC):
    """One broker, behind a uniform interface."""

    name: str = "unknown"
    capabilities: BrokerCapabilities = BrokerCapabilities()

    #: True when this adapter cannot move real money.
    #:
    #: The dry-run gate exists to stop an accidental REAL order, not to stop
    #: simulation. Gating a paper broker would defeat its entire purpose —
    #: you could never practise. Real adapters leave this False and stay
    #: behind the gate; simulators set it True and always execute.
    is_simulated: bool = False

    def __init__(self, auth: AuthStrategy, config: dict | None = None) -> None:
        self._auth = auth
        self.config = config or {}

    # -- session ---------------------------------------------------------

    def authenticate(self) -> Session:
        """Establish or reuse a session."""
        return self._auth.ensure_valid()

    @property
    def session(self) -> Session | None:
        return self._auth.session

    def is_connected(self) -> bool:
        session = self._auth.session
        return session is not None and session.is_valid()

    # -- orders ----------------------------------------------------------

    @abstractmethod
    def place_order(self, order: UnifiedOrder) -> UnifiedOrder:
        """Submit an order and return it with broker identifiers and status.

        Implementations must NOT apply the dry-run gate themselves — that
        check happens once in `OrderService`, above every adapter, so it can
        never be forgotten in a new integration.
        """

    @abstractmethod
    def cancel_order(self, order_id: str) -> UnifiedOrder:
        """Cancel a working order."""

    @abstractmethod
    def modify_order(
        self,
        order_id: str,
        *,
        quantity: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> UnifiedOrder:
        """Amend a working order."""

    @abstractmethod
    def get_order_status(self, order_id: str) -> UnifiedOrder:
        """Authoritative current state of one order."""

    @abstractmethod
    def get_orders(self) -> list[UnifiedOrder]:
        """Every order known for the current session/day."""

    def get_fills(self) -> list[Fill]:
        """Executions. Optional: not every broker exposes a tradebook."""
        return []

    # -- positions and account -------------------------------------------

    @abstractmethod
    def get_positions(self) -> list[UnifiedPosition]:
        """Open positions, with quantity signed (negative == short)."""

    @abstractmethod
    def get_account(self) -> AccountSnapshot:
        """Cash, equity and P&L."""

    # -- market data ------------------------------------------------------

    @abstractmethod
    def get_quote(self, symbol: str) -> UnifiedQuote:
        """Latest price for one symbol."""

    def get_quotes(self, symbols: list[str]) -> dict[str, UnifiedQuote]:
        """Batch quotes. Overridden where a broker offers a bulk endpoint;
        the default loop is correct but chattier."""
        quotes: dict[str, UnifiedQuote] = {}
        for symbol in symbols:
            try:
                quotes[symbol] = self.get_quote(symbol)
            except Exception:  # noqa: BLE001 - one bad symbol must not blind the rest
                continue
        return quotes

    @abstractmethod
    def get_instruments(self) -> list[UnifiedInstrument]:
        """The tradable universe, in canonical form."""

    def resolve_instrument(self, symbol: str) -> UnifiedInstrument:
        """Canonical symbol to instrument. Raises `InstrumentNotFound` rather
        than returning None, so a bad symbol cannot silently become a
        malformed order."""
        from .exceptions import InstrumentNotFound

        for instrument in self.get_instruments():
            if instrument.symbol == symbol:
                return instrument
        raise InstrumentNotFound(
            f"{symbol} is not tradable on this broker", broker=self.name, symbol=symbol
        )

    # -- streaming --------------------------------------------------------

    def subscribe_ws(
        self,
        symbols: list[str],
        on_tick: Callable[[UnifiedQuote], None],
        on_order_update: Callable[[UnifiedOrder], None] | None = None,
    ) -> None:
        """Open a streaming subscription.

        One callback signature regardless of whether the broker streams
        binary ticks, Protobuf or JSON underneath. Adapters that cannot
        stream should leave this unimplemented and declare
        `supports_streaming=False`.
        """
        raise NotImplementedError(f"{self.name} does not support streaming")

    def unsubscribe_ws(self) -> None:
        """Close any streaming subscription. Safe to call when none is open."""

    # -- introspection ----------------------------------------------------

    def describe(self) -> dict:
        return {
            "name": self.name,
            "simulated": self.is_simulated,
            "connected": self.is_connected(),
            "capabilities": self.capabilities.describe(),
            "auth": self._auth.describe(),
        }
