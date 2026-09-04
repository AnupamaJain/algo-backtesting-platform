"""Live market-data providers for the broker layer.

The paper broker fills against real prices, not invented ones. `LiveQuotes`
pulls actual last-traded prices from the market; `CachedQuotes` falls back to
the most recent close already on disk.

Both are real market data. The difference is freshness, and it is surfaced
rather than hidden: every quote carries the timestamp it was observed, and a
fallback quote is flagged stale so the console can say so instead of implying
a live tick.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from .exceptions import BrokerUnavailable, InstrumentNotFound
from .models import UnifiedQuote

logger = logging.getLogger(__name__)


class QuoteProvider(ABC):
    """Source of prices for order filling and position marking."""

    @abstractmethod
    def get_quote(self, symbol: str) -> UnifiedQuote:
        """Latest price for a symbol, or raise."""

    def get_quotes(self, symbols: list[str]) -> dict[str, UnifiedQuote]:
        result: dict[str, UnifiedQuote] = {}
        for symbol in symbols:
            try:
                result[symbol] = self.get_quote(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.debug("No quote for %s: %s", symbol, exc)
        return result


class LiveQuotes(QuoteProvider):
    """Real-time-ish quotes from the market feed.

    Results are cached for `ttl_seconds` because a dashboard refresh asks for
    every held symbol at once, and re-requesting the same price several times
    a second would get the session throttled without improving accuracy.
    """

    def __init__(self, ttl_seconds: float = 15.0, fallback: QuoteProvider | None = None) -> None:
        self._ttl = ttl_seconds
        self._fallback = fallback
        self._cache: dict[str, tuple[float, UnifiedQuote]] = {}

    def get_quote(self, symbol: str) -> UnifiedQuote:
        now = time.monotonic()
        cached = self._cache.get(symbol)
        if cached and (now - cached[0]) < self._ttl:
            return cached[1]

        try:
            quote = self._fetch(symbol)
            self._cache[symbol] = (now, quote)
            return quote
        except Exception as exc:  # noqa: BLE001
            if self._fallback is not None:
                logger.warning("Live quote failed for %s (%s); using last close", symbol, exc)
                return self._fallback.get_quote(symbol)
            raise BrokerUnavailable(f"no quote available for {symbol}: {exc}") from exc

    def _fetch(self, symbol: str) -> UnifiedQuote:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        last = info.get("lastPrice") or info.get("last_price")
        if last is None:
            raise BrokerUnavailable(f"feed returned no price for {symbol}")

        previous_close = info.get("previousClose") or info.get("previous_close")
        return UnifiedQuote(
            symbol=symbol,
            last_price=float(last),
            timestamp=datetime.now(),
            bid=_safe_float(info.get("bid")),
            ask=_safe_float(info.get("ask")),
            volume=_safe_float(info.get("lastVolume") or info.get("last_volume")),
            previous_close=_safe_float(previous_close),
        )


class CachedQuotes(QuoteProvider):
    """Last close from the locally cached history.

    Real prices, just not current ones. Used as a fallback when the live feed
    is unreachable (outside market hours, no network), so the console keeps
    marking positions instead of going blank.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._cache: dict[str, UnifiedQuote] = {}

    def get_quote(self, symbol: str) -> UnifiedQuote:
        if symbol in self._cache:
            return self._cache[symbol]

        import pandas as pd

        path = self._data_dir / f"{symbol.replace('/', '_')}.csv"
        if not path.exists():
            raise InstrumentNotFound(f"no cached history for {symbol}", symbol=symbol)

        frame = pd.read_csv(path, index_col=0, parse_dates=True)
        if frame.empty:
            raise InstrumentNotFound(f"cached history for {symbol} is empty", symbol=symbol)

        last_row = frame.iloc[-1]
        previous = frame["Close"].iloc[-2] if len(frame) > 1 else None
        quote = UnifiedQuote(
            symbol=symbol,
            last_price=float(last_row["Close"]),
            # The timestamp is the bar's own date, not "now" — the console
            # uses it to show how stale this price is.
            timestamp=frame.index[-1].to_pydatetime(),
            volume=_safe_float(last_row.get("Volume")),
            previous_close=_safe_float(previous),
        )
        self._cache[symbol] = quote
        return quote


class StaticQuotes(QuoteProvider):
    """Fixed prices, for deterministic tests only.

    Never used by the running application — the console and paper broker are
    wired to real market data. This exists so contract tests can assert fill
    behaviour without a network call.
    """

    def __init__(self, prices: dict[str, float]) -> None:
        self._prices = dict(prices)

    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    def get_quote(self, symbol: str) -> UnifiedQuote:
        if symbol not in self._prices:
            raise InstrumentNotFound(f"no price configured for {symbol}", symbol=symbol)
        price = self._prices[symbol]
        return UnifiedQuote(
            symbol=symbol,
            last_price=price,
            # Stamped now, not at a fixed past date. What these quotes must
            # be deterministic about is the PRICE; a frozen timestamp makes
            # every one of them look staler by the day and trips the paper
            # broker's stale-quote guard.
            timestamp=datetime.now(),
            bid=price * 0.9995,
            ask=price * 1.0005,
            previous_close=price,
        )


class ChainedQuotes(QuoteProvider):
    """Try each provider in turn; the first that answers wins.

    This is the payoff of the adapter abstraction: when one broker's session
    lapses mid-session, another can price the same instrument and the trading
    loop keeps running. Nothing above this class knows which broker served a
    given quote.

    Two properties that matter more than the failover itself:

    **Providers are built lazily.** Constructing a broker provider
    authenticates, and doing that for every link in the chain up front means
    one dead token takes down a chain that would otherwise have worked.

    **The source is recorded.** A quote from the backup broker, or worse from
    yesterday's cache, must not be indistinguishable from a live primary
    quote — `last_source` and the quote's own timestamp let callers tell.
    """

    def __init__(self, builders: list[tuple[str, callable]]) -> None:
        self._builders = list(builders)
        self._built: dict[str, QuoteProvider | None] = {}
        self.last_source: str | None = None

    def _provider(self, name: str, build) -> QuoteProvider | None:
        """Construct on first use; remember failures so we retry once, not
        on every symbol of every loop."""
        if name not in self._built:
            try:
                self._built[name] = build()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Quote source %s unavailable: %s", name, exc)
                self._built[name] = None
        return self._built[name]

    def get_quote(self, symbol: str) -> UnifiedQuote:
        errors = []
        for name, build in self._builders:
            provider = self._provider(name, build)
            if provider is None:
                errors.append(f"{name}: unavailable")
                continue
            try:
                quote = provider.get_quote(symbol)
            except Exception as exc:  # noqa: BLE001 - try the next source
                errors.append(f"{name}: {exc}")
                continue
            if self.last_source != name:
                logger.info("Quotes for %s served by %s", symbol, name)
            self.last_source = name
            return quote

        raise BrokerUnavailable(
            f"no quote source could price {symbol} ({'; '.join(errors)})",
            broker="chained",
        )

    def get_quotes(self, symbols: list[str]) -> dict[str, UnifiedQuote]:
        """Batch through the first provider that can serve the whole list.

        Falls back to per-symbol resolution so one unpriceable name does not
        cost the entire batch.
        """
        for name, build in self._builders:
            provider = self._provider(name, build)
            if provider is None:
                continue
            try:
                quotes = provider.get_quotes(symbols)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Batch quotes failed on %s: %s", name, exc)
                continue
            if len(quotes) == len(symbols):
                self.last_source = name
                return quotes
        return super().get_quotes(symbols)


def build_quote_provider(config: dict, data_dir: Path) -> QuoteProvider:
    """Construct the configured quote source, plus any declared fallbacks.

    `quote_source` names the primary. `quote_fallbacks` is an ordered list
    tried when it fails — so a paper account on Dhan can keep pricing NSE
    instruments through Flattrade when a Dhan token lapses.

    The provider must match the market: yfinance cannot price RELIANCE-EQ and
    Flattrade cannot price SPY, so a fallback chain must only list sources
    that cover the same instruments.

    The cached last close is always available as the final link, and is
    deliberately last: it is real data but not current, and the paper
    broker's staleness guard will refuse to *fill* against it.
    """
    cached = CachedQuotes(data_dir)

    def builder(name: str):
        if name == "cached":
            return lambda: cached
        if name == "flattrade":
            def build_flattrade():
                # Imported lazily: the US path must not require a token.
                from .flattrade import FlattradeAuth, FlattradeClient, FlattradeQuotes

                auth = FlattradeAuth(
                    "flattrade",
                    {
                        "token_file": config.get(
                            "flattrade_token_file", "state/flattrade_token.json"
                        ),
                        "auto_login": config.get("auto_login", True),
                    },
                )
                session = auth.authenticate()
                client = FlattradeClient(
                    token=session.access_token or "", client_id=session.user_id or ""
                )
                return FlattradeQuotes(
                    client,
                    exchange=config.get("exchange", "NSE"),
                    ttl_seconds=float(config.get("quote_ttl_seconds", 5)),
                )

            return build_flattrade
        if name == "dhan":
            def build_dhan():
                from .dhan import DhanAdapter, DhanAuth

                # `config` is shared with the flattrade source above, whose
                # `token_file` names FLATTRADE's token file. Passing that
                # straight through here points DhanAuth at the wrong
                # broker's token (it would "authenticate" with a Flattrade
                # session token and get a real, correct 401 from Dhan). Use
                # a namespaced `dhan_token_file` instead, defaulting to
                # None so DhanAuth falls back to its own canonical location.
                dhan_config = dict(config)
                dhan_config["token_file"] = config.get("dhan_token_file")
                return BrokerQuotes(DhanAdapter(DhanAuth("dhan", dhan_config), dhan_config))

            return build_dhan
        if name in ("live", "yfinance"):
            return lambda: LiveQuotes(
                ttl_seconds=float(config.get("quote_ttl_seconds", 15))
            )
        raise ValueError(f"unknown quote source {name!r}")

    primary = config.get("quote_source", "live")
    fallbacks = list(config.get("quote_fallbacks", []) or [])
    # The cache is the last resort unless the caller has placed it explicitly.
    chain = [primary, *fallbacks]
    if "cached" not in chain:
        chain.append("cached")

    if len(chain) == 1:
        return builder(chain[0])()

    return ChainedQuotes([(name, builder(name)) for name in chain])


class BrokerQuotes(QuoteProvider):
    """Quotes from any BrokerAdapter, with a cached-history fallback.

    Broker-agnostic by construction: it depends on the adapter interface, so
    a new broker needs no change here.
    """

    def __init__(self, adapter, fallback: QuoteProvider | None = None) -> None:
        self._adapter = adapter
        self._fallback = fallback

    def get_quote(self, symbol: str) -> UnifiedQuote:
        try:
            return self._adapter.get_quote(symbol)
        except Exception as exc:  # noqa: BLE001
            if self._fallback is not None:
                logger.warning(
                    "%s quote failed for %s (%s); using last close",
                    getattr(self._adapter, "name", "broker"), symbol, exc,
                )
                return self._fallback.get_quote(symbol)
            raise


def _safe_float(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result > 0 else None  # reject NaN/0
