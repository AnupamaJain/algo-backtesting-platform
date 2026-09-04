"""Broker abstraction layer.

Strategy and portfolio code imports from here and never from a broker SDK.
"""

from .adapter import BrokerAdapter, BrokerCapabilities
from .auth import (
    AuthStrategy,
    NoAuth,
    OAuthRedirectAuth,
    Session,
    StaticAPIKeyAuth,
    TOTPCredentialAuth,
)
from .exceptions import (
    AuthError,
    BrokerError,
    BrokerUnavailable,
    InstrumentNotFound,
    InsufficientFunds,
    OrderRejected,
    RateLimited,
    SafetyGateBlocked,
)
from .factory import BrokerFactory
from .mock import MockAdapter
from .models import (
    AccountSnapshot,
    Fill,
    InstrumentType,
    OrderStatus,
    OrderType,
    ProductType,
    Side,
    UnifiedInstrument,
    UnifiedOrder,
    UnifiedPosition,
    UnifiedQuote,
)
from .paper import PaperBroker
from .quotes import CachedQuotes, LiveQuotes, QuoteProvider, StaticQuotes
from .service import OrderService, SafetySettings
from .store import BrokerStore

__all__ = [
    "AccountSnapshot", "AuthError", "AuthStrategy", "BrokerAdapter", "BrokerCapabilities",
    "BrokerError", "BrokerFactory", "BrokerStore", "BrokerUnavailable", "CachedQuotes",
    "Fill", "InstrumentNotFound", "InstrumentType", "InsufficientFunds", "LiveQuotes",
    "MockAdapter", "NoAuth", "OAuthRedirectAuth", "OrderRejected", "OrderService",
    "OrderStatus", "OrderType", "PaperBroker", "ProductType", "QuoteProvider",
    "RateLimited", "SafetyGateBlocked", "SafetySettings", "Session", "Side",
    "StaticAPIKeyAuth", "StaticQuotes", "TOTPCredentialAuth", "UnifiedInstrument",
    "UnifiedOrder", "UnifiedPosition", "UnifiedQuote",
]
