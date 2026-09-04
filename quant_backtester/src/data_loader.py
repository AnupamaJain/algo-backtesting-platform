"""Layer 1 - historical market data acquisition, caching and slicing."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from .config import DataConfig

logger = logging.getLogger(__name__)


# ==========================================================================
# HistoricalDataManager
# ==========================================================================
# HistoricalDataManager: download, cache, clean, and slice OHLCV data.
#
# Single Responsibility: this module only gets price data into a clean,
# canonical DataFrame shape onto disk and back. It knows nothing about
# strategies or signals.


CANONICAL_COLUMNS = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


class DataDownloadError(RuntimeError):
    """Raised when fresh data can't be downloaded AND no usable cache exists."""


class HistoricalDataManager:
    """Fetches, caches, cleans, and slices daily OHLCV bars for a configured
    asset universe.

    Every path, date range, and price-field choice comes from the injected
    `DataConfig` (see config.py) — nothing here hardcodes an asset list, a
    date, or a directory.
    """

    def __init__(self, config: DataConfig, downloader=None) -> None:
        self.config = config
        # Dependency inversion: the download call is injected so tests can
        # supply a fake with no network access.
        #
        # Built LAZILY. Constructing it eagerly authenticates against the
        # broker, so a lapsed token made this class unusable even when every
        # symbol was already cached and no download was needed — the cache
        # exists precisely so a broker outage is survivable.
        self._injected = downloader
        self._downloader_cache = None
        self.config.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _downloader(self):
        """The download callable, constructed on first actual use."""
        if self._injected is not None:
            return self._injected
        if self._downloader_cache is None:
            self._downloader_cache = _downloader_for(self.config)
        return self._downloader_cache

    # -- public API ---------------------------------------------------

    def get_history(self, symbol: str, force_refresh: bool = False) -> pd.DataFrame:
        """Return cleaned OHLCV data for `symbol` covering the configured
        date range, using the local cache when possible and falling back to
        it gracefully if a fresh download fails."""
        start = self.config.resolved_start()
        end = self.config.resolved_end()
        cache_path = self._cache_path(symbol)

        cached = self._read_cache(cache_path)
        meta = self._read_meta(self._meta_path(symbol))
        if (
            not force_refresh
            and cached is not None
            and self._cache_covers_range(meta, start, end)
        ):
            logger.info("Using cached data for %s (%s rows)", symbol, len(cached))
            return self._slice_range(cached, start, end)

        try:
            # Touching self._downloader may itself authenticate and raise;
            # that belongs inside the try so the cache fallback below covers
            # a dead broker session as well as a failed request.
            fresh = self._downloader(symbol, start, end)
            fresh = self._clean(fresh)
            if fresh.empty:
                raise DataDownloadError(f"downloader returned no rows for {symbol}")
            self._write_cache(cache_path, fresh)
            self._write_meta(self._meta_path(symbol), start, end)
            logger.info("Downloaded fresh data for %s (%s rows)", symbol, len(fresh))
            return self._slice_range(fresh, start, end)
        except Exception as exc:  # noqa: BLE001 - broad on purpose, see fallback below
            if cached is not None and not cached.empty:
                logger.warning(
                    "Download failed for %s (%s); falling back to cache (%s rows)",
                    symbol,
                    exc,
                    len(cached),
                )
                return self._slice_range(cached, start, end)
            raise DataDownloadError(
                f"could not download {symbol} and no usable cache exists"
            ) from exc

    def get_universe_history(
        self, symbols: list[str], force_refresh: bool = False
    ) -> dict[str, pd.DataFrame]:
        """Fetch history for every symbol; a single symbol's failure doesn't
        abort the rest of the universe."""
        result: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            try:
                result[symbol] = self.get_history(symbol, force_refresh=force_refresh)
            except DataDownloadError as exc:
                logger.error("Skipping %s: %s", symbol, exc)
        return result

    def train_test_split(
        self,
        data: pd.DataFrame,
        split_ratio: float | None = None,
        split_date: date | None = None,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Slice `data` into an in-sample (train) and out-of-sample (test)
        window for walk-forward-style downstream use.

        Exactly one of `split_ratio` or `split_date` should be provided; if
        neither is, the configured default split_ratio is not used here
        (callers own that choice) — pass it explicitly to avoid surprise.
        """
        if split_date is not None and split_ratio is not None:
            raise ValueError("provide only one of split_ratio or split_date")
        if split_date is None and split_ratio is None:
            raise ValueError("must provide split_ratio or split_date")

        if split_date is not None:
            train = data.loc[data.index < pd.Timestamp(split_date)]
            test = data.loc[data.index >= pd.Timestamp(split_date)]
        else:
            if not 0 < split_ratio < 1:
                raise ValueError("split_ratio must be between 0 and 1")
            split_idx = int(len(data) * split_ratio)
            train = data.iloc[:split_idx]
            test = data.iloc[split_idx:]
        return train, test

    # -- internals ------------------------------------------------------

    def _cache_path(self, symbol: str) -> Path:
        safe_name = symbol.replace("/", "_")
        return self.config.cache_dir / f"{safe_name}.csv"

    def _meta_path(self, symbol: str) -> Path:
        safe_name = symbol.replace("/", "_")
        return self.config.cache_dir / f"{safe_name}.meta.json"

    def _read_cache(self, path: Path) -> pd.DataFrame | None:
        if not path.exists():
            return None
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            return df
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cache file %s unreadable (%s); ignoring cache", path, exc)
            return None

    def _write_cache(self, path: Path, data: pd.DataFrame) -> None:
        data.to_csv(path)

    def _read_meta(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Metadata file %s unreadable (%s); ignoring", path, exc)
            return None

    def _write_meta(self, path: Path, start: date, end: date) -> None:
        with path.open("w", encoding="utf-8") as fh:
            json.dump({"requested_start": start.isoformat(), "requested_end": end.isoformat()}, fh)

    def _cache_covers_range(self, meta: dict | None, start: date, end: date) -> bool:
        """Coverage is judged against the *requested* range recorded when the
        cache was written, not against the data's own min/max timestamps —
        a weekend/holiday start date will never appear as a row in the data,
        so inferring coverage from row bounds would force a redundant
        re-download on every call."""
        if meta is None:
            return False
        cached_start = date.fromisoformat(meta["requested_start"])
        cached_end = date.fromisoformat(meta["requested_end"])
        return cached_start <= start and cached_end >= end

    def _slice_range(self, data: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
        return data.loc[
            (data.index >= pd.Timestamp(start)) & (data.index <= pd.Timestamp(end))
        ].copy()

    def _clean(self, data: pd.DataFrame) -> pd.DataFrame:
        """Normalize columns, forward-fill small gaps, drop rows that are
        still broken afterward."""
        data = data.copy()
        data.index = pd.to_datetime(data.index)
        data = data.sort_index()
        data = data[~data.index.duplicated(keep="last")]

        missing_cols = [c for c in CANONICAL_COLUMNS if c not in data.columns]
        if missing_cols:
            raise DataDownloadError(f"downloaded data missing columns: {missing_cols}")
        data = data[CANONICAL_COLUMNS]

        data = data.ffill(limit=self.config.max_forward_fill_days)
        before = len(data)
        data = data.dropna(subset=CANONICAL_COLUMNS)
        dropped = before - len(data)
        if dropped:
            logger.warning("Dropped %s rows with unrecoverable missing data", dropped)

        # The canonical "Close" downstream code should read is the
        # configured price_field (defaults to Adj Close, which accounts for
        # splits/dividends) — expose it as `Close` without discarding the
        # raw unadjusted close.
        data = data.rename(columns={"Close": "Close_Raw"})
        price_field = self.config.price_field
        source_col = "Close_Raw" if price_field == "Close" else price_field
        data["Close"] = data[source_col]

        # Back-adjust Open/High/Low by the same factor applied to the close.
        #
        # This is not cosmetic. yfinance adjusts only the close for splits and
        # dividends, leaving OHL on the raw price scale. Publishing an
        # adjusted close alongside unadjusted highs and lows silently corrupts
        # every range-based indicator, because true range differences today's
        # high against yesterday's close — two different price scales. For a
        # dividend-paying ETF over 15 years the gap is large enough to inflate
        # ATR by more than an order of magnitude, which in turn distorts
        # Keltner channels, ATR position sizing and the regime features.
        adjustment = (data["Close"] / data["Close_Raw"]).replace(
            [np.inf, -np.inf], np.nan
        ).fillna(1.0)
        for column in ("Open", "High", "Low"):
            data[column] = data[column] * adjustment

        return data[["Open", "High", "Low", "Close", "Volume", "Close_Raw", "Adj Close"]]


def _downloader_for(config: DataConfig):
    """The download callable for a universe, including any fallbacks."""
    fallbacks = list(getattr(config, "source_fallbacks", []) or [])
    if fallbacks:
        chain = [config.source, *fallbacks]
        return ChainedDownloader([(n, (lambda n=n: _single_downloader(n))) for n in chain])
    return _single_downloader(config.source)


def _single_downloader(source: str):
    """Select one named market-data provider.

    Providers cover disjoint universes: yfinance for US instruments,
    Flattrade and Dhan for Indian ones. Nothing can price another's symbols,
    so the choice belongs to the universe rather than being a global setting
    — and a fallback chain must only list sources covering the same market.
    """
    source = (source or "yfinance").lower()
    if source == "yfinance":
        return _yfinance_downloader
    if source == "flattrade":
        return _build_flattrade_downloader()
    if source == "dhan":
        return _build_dhan_downloader()
    raise ValueError(
        f"unknown data source: {source!r} (expected yfinance, flattrade or dhan)"
    )


def _build_dhan_downloader():
    """Construct a Dhan downloader for daily bars."""
    from pathlib import Path as _Path

    from .broker.dhan import DhanAdapter, DhanAuth, build_dhan_downloader

    state = _Path(__file__).resolve().parent.parent / "state"
    settings = {"token_file": str(state / "dhan_token.json"), "exchange": "NSE"}
    adapter = DhanAdapter(DhanAuth("dhan", settings), settings)
    try:
        adapter.authenticate()
    except Exception as exc:  # noqa: BLE001
        raise DataDownloadError(f"Dhan authentication failed: {exc}") from exc
    return build_dhan_downloader(adapter)


class ChainedDownloader:
    """Try each configured source in turn for a symbol's history.

    Ingestion is the one place a broker outage cannot be papered over with a
    cache: if the bars are missing, they are missing. Falling through to a
    second broker covering the same market keeps a run alive when one
    session lapses.

    Sources are constructed lazily and a failure is remembered, for the same
    reason as in the quote chain: building one authenticates, and retrying a
    dead session once per symbol turns a 23-symbol universe into 23 doomed
    logins.
    """

    def __init__(self, builders: list[tuple[str, callable]]) -> None:
        self._builders = list(builders)
        self._built: dict[str, object] = {}
        self.last_source: str | None = None

    def _downloader(self, name: str, build):
        if name not in self._built:
            try:
                self._built[name] = build()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Data source %s unavailable: %s", name, exc)
                self._built[name] = None
        return self._built[name]

    def __call__(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        errors = []
        for name, build in self._builders:
            downloader = self._downloader(name, build)
            if downloader is None:
                errors.append(f"{name}: unavailable")
                continue
            try:
                frame = downloader(symbol, start, end)
            except Exception as exc:  # noqa: BLE001 - try the next source
                errors.append(f"{name}: {exc}")
                continue
            if frame is not None and not frame.empty:
                if self.last_source != name:
                    logger.info("History for %s served by %s", symbol, name)
                self.last_source = name
                return frame
            errors.append(f"{name}: returned no rows")

        raise DataDownloadError(
            f"no data source could supply {symbol} ({'; '.join(errors)})"
        )


def _build_flattrade_downloader():
    """Construct a Flattrade downloader, logging in automatically if needed.

    Auth goes through FlattradeAuth rather than reading the token file
    directly, so an expired or missing token triggers a silent re-login
    instead of failing an unattended run.
    """
    from .broker.flattrade import FlattradeAuth, FlattradeClient, build_flattrade_downloader

    token_path = Path(__file__).resolve().parent.parent / "state" / "flattrade_token.json"
    auth = FlattradeAuth("flattrade", {"token_file": str(token_path), "auto_login": True})
    try:
        session = auth.authenticate()
    except Exception as exc:  # noqa: BLE001
        raise DataDownloadError(f"Flattrade authentication failed: {exc}") from exc

    client = FlattradeClient(
        token=session.access_token or "", client_id=session.user_id or ""
    )
    return build_flattrade_downloader(client)


def _yfinance_downloader(symbol: str, start: date, end: date) -> pd.DataFrame:
    """Default downloader backed by yfinance. Kept as a free function (not a
    method) so HistoricalDataManager can be constructed with a mock
    downloader in tests without importing yfinance at all."""
    import yfinance as yf

    df = yf.download(
        symbol,
        start=start.isoformat(),
        end=end.isoformat(),
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df
