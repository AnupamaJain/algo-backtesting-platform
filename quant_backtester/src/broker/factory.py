"""BrokerFactory — builds the configured adapter(s).

Switching brokers is a config change and a restart, never a code change.
Registration is by name, so adding a broker means writing an adapter and
registering it here; nothing else in the system needs to know it exists.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .adapter import BrokerAdapter
from .auth import AuthStrategy, NoAuth, OAuthRedirectAuth, StaticAPIKeyAuth, TOTPCredentialAuth
from .mock import MockAdapter
from .paper import PaperBroker
from .quotes import build_quote_provider
from .service import OrderService, SafetySettings
from .store import BrokerStore

logger = logging.getLogger(__name__)

def _dhan_auth(broker: str, config: dict) -> AuthStrategy:
    """Dhan's token is loaded, validated and reported on — never minted."""
    from .dhan import DhanAuth

    return DhanAuth(broker, config)


def _flattrade_auth(broker: str, config: dict) -> AuthStrategy:
    """Deferred import: the Flattrade module pulls in `requests`, which the
    research pipeline does not otherwise need."""
    from .flattrade import FlattradeAuth

    return FlattradeAuth(broker, config)


AUTH_STRATEGIES = {
    "none": NoAuth,
    "flattrade": _flattrade_auth,
    "oauth_redirect": OAuthRedirectAuth,
    "totp": TOTPCredentialAuth,
    "static_api_key": StaticAPIKeyAuth,
    "dhan": _dhan_auth,
}


class BrokerFactory:
    """Constructs adapters and the order service from configuration."""

    def __init__(self, config: dict, state_dir: Path, data_dir: Path) -> None:
        self._config = config
        self._state_dir = Path(state_dir)
        self._data_dir = Path(data_dir)

    @property
    def active_broker(self) -> str:
        return self._config.get("active", "paper")

    def available(self) -> list[str]:
        return list(self._config.get("brokers", {}).keys())

    def build_store(self, broker: str | None = None) -> BrokerStore:
        name = broker or self.active_broker
        return BrokerStore(self._state_dir / f"{name}_broker.db")

    def build_auth(self, broker: str) -> AuthStrategy:
        settings = self._config.get("brokers", {}).get(broker, {})
        kind = settings.get("auth", "none")
        strategy_class = AUTH_STRATEGIES.get(kind)
        if strategy_class is None:
            raise ValueError(
                f"unknown auth strategy {kind!r}; known: {', '.join(AUTH_STRATEGIES)}"
            )
        return strategy_class(broker, settings)

    def build_adapter(self, broker: str | None = None) -> BrokerAdapter:
        adapter = self._build_adapter(broker)
        # `adapter.name` is the ADAPTER kind ("paper"), which three different
        # paper accounts share. Anything keyed per-account — trigger stores,
        # ledgers, heartbeats — must use the CONFIGURED name instead, or
        # paper_india and paper_dhan silently share state.
        adapter.config_name = broker or self.active_broker
        return adapter

    def _build_adapter(self, broker: str | None = None) -> BrokerAdapter:
        name = broker or self.active_broker
        settings = dict(self._config.get("brokers", {}).get(name, {}))
        if not settings:
            raise ValueError(f"broker {name!r} is not configured")

        kind = settings.get("adapter", name)
        store = self.build_store(name)

        if kind == "paper":
            quotes = build_quote_provider(settings, self._data_dir)
            return PaperBroker(store, quotes, self.build_auth(name), settings)
        if kind == "mock":
            return MockAdapter(config=settings)
        if kind == "flattrade":
            from .flattrade import FlattradeAdapter, FlattradeAuth

            return FlattradeAdapter(FlattradeAuth(name, settings), settings)
        if kind == "dhan":
            from .dhan import DhanAdapter, DhanAuth

            return DhanAdapter(DhanAuth(name, settings), settings)

        raise ValueError(
            f"no adapter implementation registered for {kind!r}. "
            "Implement BrokerAdapter and register it in BrokerFactory."
        )

    def build_safety(self) -> SafetySettings:
        safety = self._config.get("safety", {})
        return SafetySettings(
            live_trading=bool(safety.get("live_trading", False)),
            max_order_value=float(safety.get("max_order_value", 100_000)),
            duplicate_window_seconds=int(safety.get("duplicate_window_seconds", 30)),
            max_retries=int(safety.get("max_retries", 3)),
            backoff_seconds=float(safety.get("backoff_seconds", 1.0)),
        )

    def build_service(self, broker: str | None = None) -> OrderService:
        name = broker or self.active_broker
        return OrderService(self.build_adapter(name), self.build_store(name), self.build_safety())
