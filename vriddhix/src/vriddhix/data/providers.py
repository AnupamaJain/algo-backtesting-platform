"""OHLCV providers.

One interface, several sources, chained with failover. Nothing above this
module knows where a bar came from -- except that every bar records its
``provider``, because "which source said this?" is the first question asked
when two numbers disagree.

Phase 1 ships two implementations:

* ``CsvCacheProvider``  -- the existing on-disk NSE daily cache
* ``YFinanceProvider``  -- NSE via the ``.NS`` suffix

A broker-backed provider (Flattrade/Dhan, already present in this repository
under ``quant_backtester/src/broker/``) slots in behind the same ABC when live
intraday data is needed in Phase 4; no caller changes.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path

import pandas as pd

from ..domain.types import DataIssue, QualityFinding, Severity
from ..features.technical import normalise_price_frame

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """A provider could not answer. Carries the symbol so the chain can record
    which source failed for which name, rather than a bare traceback."""

    def __init__(self, provider: str, symbol: str, message: str) -> None:
        super().__init__(f"[{provider}] {symbol}: {message}")
        self.provider = provider
        self.symbol = symbol


class OHLCVProvider(ABC):
    """Fetch daily bars for one symbol.

    Implementations return a frame obeying the price-frame contract
    (docs/04-data-model.md §3). Normalisation is the provider's job -- callers
    must not have to know that one source spells it ``Adj Close`` and another
    ``adj_close``.
    """

    name: str = "abstract"

    @abstractmethod
    def fetch(self, symbol: str, start: date | None = None, end: date | None = None) -> pd.DataFrame:
        ...

    def available(self) -> bool:
        """Whether this provider can be used at all right now."""
        return True

    def _slice(self, df: pd.DataFrame, start: date | None, end: date | None) -> pd.DataFrame:
        if start is not None:
            df = df[df.index >= pd.Timestamp(start)]
        if end is not None:
            df = df[df.index <= pd.Timestamp(end)]
        return df


class CsvCacheProvider(OHLCVProvider):
    """Daily bars from the on-disk CSV cache.

    The cache carries an ``-EQ`` suffix on NSE cash symbols (Flattrade's
    convention). Both spellings are accepted so that callers can use the plain
    exchange symbol throughout.
    """

    name = "csv_cache"

    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)

    def available(self) -> bool:
        return self.cache_dir.is_dir()

    def _path_for(self, symbol: str) -> Path | None:
        for candidate in (f"{symbol}.csv", f"{symbol}-EQ.csv", f"{symbol.upper()}-EQ.csv"):
            path = self.cache_dir / candidate
            if path.exists():
                return path
        return None

    def fetch(self, symbol: str, start: date | None = None, end: date | None = None) -> pd.DataFrame:
        path = self._path_for(symbol)
        if path is None:
            raise ProviderError(self.name, symbol, f"no cached file in {self.cache_dir}")

        try:
            raw = pd.read_csv(path)
        except Exception as exc:  # noqa: BLE001 - surfaced with provider context
            raise ProviderError(self.name, symbol, f"unreadable cache file: {exc}") from exc

        if raw.empty:
            raise ProviderError(self.name, symbol, "cache file is empty")

        frame = normalise_price_frame(raw)
        return self._slice(frame, start, end)


class YFinanceProvider(OHLCVProvider):
    """Daily NSE bars via yfinance.

    NSE symbols carry a ``.NS`` suffix there; ``-EQ`` (the broker convention)
    is stripped first so that one symbol spelling works across providers.
    """

    name = "yfinance"

    def __init__(self, suffix: str = ".NS") -> None:
        self.suffix = suffix

    def available(self) -> bool:
        try:
            import yfinance  # noqa: F401
        except ImportError:
            return False
        return True

    def _ticker_for(self, symbol: str) -> str:
        base = symbol.upper().removesuffix("-EQ")
        return base if base.endswith(self.suffix) else f"{base}{self.suffix}"

    def fetch(self, symbol: str, start: date | None = None, end: date | None = None) -> pd.DataFrame:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise ProviderError(self.name, symbol, "yfinance is not installed") from exc

        try:
            raw = yf.download(
                self._ticker_for(symbol),
                start=start,
                end=end,
                progress=False,
                auto_adjust=False,
                # Per-symbol calls: grouped downloads silently drop names that
                # fail, and a quietly missing symbol is worse than a slow loop.
                threads=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(self.name, symbol, f"download failed: {exc}") from exc

        if raw is None or raw.empty:
            raise ProviderError(self.name, symbol, "no rows returned")

        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)

        frame = normalise_price_frame(raw)
        return self._slice(frame, start, end)


class ChainedProvider(OHLCVProvider):
    """Try each provider in order; the first that answers wins.

    Failures are collected rather than swallowed: if every source fails, the
    error names each one and why, which is the difference between a five
    minute diagnosis and an hour of guessing.
    """

    name = "chained"

    def __init__(self, providers: list[OHLCVProvider]) -> None:
        if not providers:
            raise ValueError("ChainedProvider requires at least one provider")
        self.providers = providers
        self.last_source: str | None = None
        self.findings: list[QualityFinding] = []

    def available(self) -> bool:
        return any(p.available() for p in self.providers)

    def fetch(self, symbol: str, start: date | None = None, end: date | None = None) -> pd.DataFrame:
        errors: list[str] = []
        for provider in self.providers:
            if not provider.available():
                errors.append(f"{provider.name}: unavailable")
                continue
            try:
                frame = provider.fetch(symbol, start, end)
            except ProviderError as exc:
                errors.append(str(exc))
                self.findings.append(
                    QualityFinding(
                        issue=DataIssue.PROVIDER_ERROR,
                        severity=Severity.WARN,
                        symbol=symbol,
                        detail={"provider": provider.name, "error": str(exc)},
                    )
                )
                continue

            if self.last_source != provider.name:
                logger.info("bars for %s served by %s", symbol, provider.name)
            self.last_source = provider.name
            return frame

        raise ProviderError(self.name, symbol, "no provider could supply bars: " + "; ".join(errors))


def build_provider(cfg) -> ChainedProvider:
    """Construct the configured provider chain."""
    registry = {
        "csv_cache": lambda: CsvCacheProvider(cfg.cache_dir),
        "yfinance": YFinanceProvider,
    }
    chain: list[OHLCVProvider] = []
    for key in cfg.get("data.providers"):
        factory = registry.get(key)
        if factory is None:
            raise ValueError(f"unknown data provider: {key!r}")
        chain.append(factory())
    return ChainedProvider(chain)
