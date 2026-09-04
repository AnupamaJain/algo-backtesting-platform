"""Typed exceptions every adapter raises.

Retry logic and strategy error handling must stay broker-agnostic. If one
adapter raised `kiteconnect.exceptions.TokenException` and another raised
`requests.HTTPError`, every caller would need per-broker branching — exactly
what this layer exists to prevent. Adapters translate their broker's failures
into these types.
"""

from __future__ import annotations


class BrokerError(Exception):
    """Base class for every broker-layer failure."""

    def __init__(self, message: str, *, broker: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.broker = broker
        self.retryable = retryable

    def __str__(self) -> str:
        return f"[{self.broker}] {self.message}" if self.broker else self.message


class AuthError(BrokerError):
    """Credentials are missing, invalid, or the session has expired.

    Not retryable: retrying with the same bad token only burns rate limit.
    The operator must re-authenticate.
    """

    def __init__(self, message: str, *, broker: str = "") -> None:
        super().__init__(message, broker=broker, retryable=False)


class OrderRejected(BrokerError):
    """The broker refused the order — margin, price bands, closed market.

    Not retryable: the same order will be rejected again.
    """

    def __init__(self, message: str, *, broker: str = "", order_id: str = "") -> None:
        super().__init__(message, broker=broker, retryable=False)
        self.order_id = order_id


class RateLimited(BrokerError):
    """Too many requests. Retryable after a pause.

    `retry_after` carries the broker's own guidance when it provides one, so
    backoff can respect it rather than guessing.
    """

    def __init__(
        self, message: str, *, broker: str = "", retry_after: float | None = None
    ) -> None:
        super().__init__(message, broker=broker, retryable=True)
        self.retry_after = retry_after


class InstrumentNotFound(BrokerError):
    """The instrument does not exist on this broker.

    Raised loudly rather than silently placing a malformed order: a symbol
    that resolves to nothing must never reach the exchange as a guess.
    """

    def __init__(self, message: str, *, broker: str = "", symbol: str = "") -> None:
        super().__init__(message, broker=broker, retryable=False)
        self.symbol = symbol


class BrokerUnavailable(BrokerError):
    """Network failure or broker-side outage. Retryable."""

    def __init__(self, message: str, *, broker: str = "") -> None:
        super().__init__(message, broker=broker, retryable=True)


class InsufficientFunds(OrderRejected):
    """Not enough buying power for the order."""


class SafetyGateBlocked(BrokerError):
    """The order was stopped by the platform's own safety gate, not by a broker.

    Distinct from `OrderRejected` on purpose: nothing reached a broker, and
    the cause is local configuration the operator controls.
    """

    def __init__(self, message: str, *, broker: str = "") -> None:
        super().__init__(message, broker=broker, retryable=False)
