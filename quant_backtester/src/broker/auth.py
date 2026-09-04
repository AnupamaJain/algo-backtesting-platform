"""Authentication strategies.

Brokers authenticate in three broad shapes, and the differences between them
are mechanical rather than conceptual. Rather than reimplement session
handling per broker, each shape is a base class:

  * `OAuthRedirectAuth`  — Zerodha, Fyers, Upstox: browser redirect, then a
    request token exchanged for an access token.
  * `TOTPCredentialAuth` — Shoonya, Flattrade (NorenApi), Alice Blue: user,
    password and a time-based one-time code.
  * `StaticAPIKeyAuth`   — Dhan: a long-lived key with no interactive step.

Every strategy hands its adapter the same `Session`, so the dashboard's
"reconnect" flow behaves identically no matter which broker is configured,
even though token lifetimes differ wildly (Zerodha invalidates daily at ~6am
IST; a static key may last months).

Credentials are never stored in these objects as literals — they are resolved
from environment variables named in config, so nothing secret is committed.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .exceptions import AuthError


@dataclass
class Session:
    """A broker session. The one thing every auth strategy produces."""

    broker: str
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: datetime | None = None
    user_id: str = ""
    metadata: dict = field(default_factory=dict)

    def is_valid(self, now: datetime | None = None) -> bool:
        """True when the session can be used for an API call right now."""
        if not self.access_token:
            return False
        if self.expires_at is None:
            return True  # no declared expiry (e.g. a static API key)
        return (now or datetime.now()) < self.expires_at

    @property
    def expires_in(self) -> timedelta | None:
        if self.expires_at is None:
            return None
        return self.expires_at - datetime.now()

    def to_dict(self) -> dict:
        """Serializable form. Deliberately omits the token itself — session
        state gets rendered in a dashboard, and a token that reaches a browser
        is a token that can be stolen."""
        return {
            "broker": self.broker,
            "user_id": self.user_id,
            "valid": self.is_valid(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "has_token": bool(self.access_token),
        }


def credential_from_env(env_var: str, *, broker: str, required: bool = True) -> str:
    """Read a credential from the environment.

    Config files name the *variable*, never the value, so a checked-in config
    can describe a live setup without containing anything secret. A missing
    variable fails loudly at startup rather than producing a confusing auth
    error on the first order.
    """
    value = os.environ.get(env_var, "")
    if not value and required:
        raise AuthError(
            f"environment variable {env_var} is not set; "
            f"export it before connecting to {broker}",
            broker=broker,
        )
    return value


class AuthStrategy(ABC):
    """Common contract: produce a valid Session, and refresh it on demand."""

    def __init__(self, broker: str, config: dict) -> None:
        self.broker = broker
        self.config = config
        self._session: Session | None = None

    @property
    def session(self) -> Session | None:
        return self._session

    @abstractmethod
    def authenticate(self) -> Session:
        """Establish a session. May require operator interaction."""

    def refresh(self) -> Session:
        """Renew the session. Defaults to a full re-authentication, which is
        correct for brokers that offer no refresh endpoint."""
        return self.authenticate()

    def ensure_valid(self) -> Session:
        """Return a usable session, refreshing if the current one has lapsed."""
        if self._session is None or not self._session.is_valid():
            self._session = self.refresh() if self._session else self.authenticate()
        return self._session

    def describe(self) -> dict:
        return {
            "broker": self.broker,
            "kind": type(self).__name__,
            "session": self._session.to_dict() if self._session else None,
        }


class OAuthRedirectAuth(AuthStrategy):
    """Redirect-based login (Zerodha, Fyers, Upstox).

    The operator visits `login_url()`, approves access, and is redirected back
    with a request token which is exchanged for an access token. The three
    brokers differ only in endpoint URLs, the exchange payload, and what the
    token field is called — all supplied by the subclass.
    """

    def login_url(self) -> str:
        base = self.config.get("login_url", "")
        api_key_env = self.config.get("api_key_env", "")
        if not base:
            raise AuthError("no login_url configured", broker=self.broker)
        api_key = credential_from_env(api_key_env, broker=self.broker) if api_key_env else ""
        return f"{base}?api_key={api_key}" if api_key else base

    def exchange_request_token(self, request_token: str) -> Session:  # pragma: no cover
        raise NotImplementedError("subclass must implement the token exchange")

    def authenticate(self) -> Session:
        token = os.environ.get(self.config.get("access_token_env", ""), "")
        if not token:
            raise AuthError(
                "no access token available; complete the redirect login first",
                broker=self.broker,
            )
        # Redirect-flow tokens are typically day-scoped; without a broker-
        # supplied expiry, assume end of day rather than "never expires".
        expiry = datetime.now().replace(hour=23, minute=59, second=0, microsecond=0)
        self._session = Session(broker=self.broker, access_token=token, expires_at=expiry)
        return self._session


class TOTPCredentialAuth(AuthStrategy):
    """Username/password plus a time-based one-time code (NorenApi brokers,
    and Alice Blue with a 2FA variant)."""

    def authenticate(self) -> Session:
        user = credential_from_env(self.config.get("user_env", ""), broker=self.broker)
        password = credential_from_env(self.config.get("password_env", ""), broker=self.broker)
        totp_secret = credential_from_env(
            self.config.get("totp_secret_env", ""), broker=self.broker
        )
        if not (user and password and totp_secret):
            raise AuthError("incomplete TOTP credentials", broker=self.broker)
        raise AuthError(
            "TOTP login requires a live broker connection; not available offline",
            broker=self.broker,
        )


class StaticAPIKeyAuth(AuthStrategy):
    """A long-lived API key with no interactive step (Dhan).

    The simplest model, and the reason Dhan is the natural first real broker
    to add: there is no redirect and no one-time code to automate.
    """

    def authenticate(self) -> Session:
        token = credential_from_env(
            self.config.get("access_token_env", ""), broker=self.broker
        )
        self._session = Session(
            broker=self.broker,
            access_token=token,
            expires_at=None,  # static keys carry no declared expiry
            user_id=os.environ.get(self.config.get("client_id_env", ""), ""),
        )
        return self._session


class NoAuth(AuthStrategy):
    """For adapters that need no credentials — the paper broker and mocks.

    Having this as a real strategy rather than a `None` special case keeps
    every adapter on the same code path, so paper trading exercises the same
    session handling that live trading will.
    """

    def authenticate(self) -> Session:
        self._session = Session(
            broker=self.broker, access_token="local", user_id="paper", expires_at=None
        )
        return self._session
