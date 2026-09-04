from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from quant_backtester.src.config import DataConfig
from quant_backtester.src.data_loader import DataDownloadError, HistoricalDataManager


def _make_fake_frame(start: date, end: date) -> pd.DataFrame:
    dates = pd.bdate_range(start, end)
    return pd.DataFrame(
        {
            "Open": 100.0,
            "High": 101.0,
            "Low": 99.0,
            "Close": 100.5,
            "Adj Close": 100.0,
            "Volume": 1_000_000,
        },
        index=dates,
    )


@pytest.fixture
def data_config(tmp_path) -> DataConfig:
    return DataConfig(
        cache_dir=tmp_path / "cache",
        lookback_years=1,
        start_date=date(2023, 1, 1),
        end_date=date(2023, 6, 30),
        price_field="Adj Close",
        max_forward_fill_days=3,
    )


def test_get_history_downloads_and_caches(data_config):
    call_count = {"n": 0}

    def fake_downloader(symbol, start, end):
        call_count["n"] += 1
        return _make_fake_frame(start, end)

    manager = HistoricalDataManager(data_config, downloader=fake_downloader)
    df = manager.get_history("FAKE")

    assert not df.empty
    assert call_count["n"] == 1
    assert (data_config.cache_dir / "FAKE.csv").exists()

    # second call should hit the cache, not the downloader
    manager.get_history("FAKE")
    assert call_count["n"] == 1


def test_falls_back_to_cache_on_download_failure(data_config):
    def working_downloader(symbol, start, end):
        return _make_fake_frame(start, end)

    manager = HistoricalDataManager(data_config, downloader=working_downloader)
    manager.get_history("FAKE")  # populate cache

    def failing_downloader(symbol, start, end):
        raise ConnectionError("network is down")

    manager_2 = HistoricalDataManager(data_config, downloader=failing_downloader)
    df = manager_2.get_history("FAKE")
    assert not df.empty


def test_raises_when_no_cache_and_download_fails(data_config):
    def failing_downloader(symbol, start, end):
        raise ConnectionError("network is down")

    manager = HistoricalDataManager(data_config, downloader=failing_downloader)
    with pytest.raises(DataDownloadError):
        manager.get_history("NEVER_CACHED")


def test_get_universe_history_skips_bad_symbols(data_config):
    def selective_downloader(symbol, start, end):
        if symbol == "BAD":
            raise ConnectionError("boom")
        return _make_fake_frame(start, end)

    manager = HistoricalDataManager(data_config, downloader=selective_downloader)
    result = manager.get_universe_history(["GOOD", "BAD"])

    assert "GOOD" in result
    assert "BAD" not in result


def test_train_test_split_by_ratio(data_config):
    def fake_downloader(symbol, start, end):
        return _make_fake_frame(start, end)

    manager = HistoricalDataManager(data_config, downloader=fake_downloader)
    df = manager.get_history("FAKE")
    train, test = manager.train_test_split(df, split_ratio=0.7)

    assert len(train) + len(test) == len(df)
    assert train.index.max() < test.index.min()


def test_train_test_split_by_date(data_config):
    def fake_downloader(symbol, start, end):
        return _make_fake_frame(start, end)

    manager = HistoricalDataManager(data_config, downloader=fake_downloader)
    df = manager.get_history("FAKE")
    split_date = date(2023, 4, 1)
    train, test = manager.train_test_split(df, split_date=split_date)

    assert (train.index < pd.Timestamp(split_date)).all()
    assert (test.index >= pd.Timestamp(split_date)).all()


def test_train_test_split_rejects_ambiguous_args(data_config):
    def fake_downloader(symbol, start, end):
        return _make_fake_frame(start, end)

    manager = HistoricalDataManager(data_config, downloader=fake_downloader)
    df = manager.get_history("FAKE")
    with pytest.raises(ValueError):
        manager.train_test_split(df, split_ratio=0.5, split_date=date(2023, 4, 1))
    with pytest.raises(ValueError):
        manager.train_test_split(df)


def test_ohl_are_back_adjusted_with_close(data_config):
    """Regression: Open/High/Low must be adjusted by the same factor as Close.

    yfinance adjusts only the close for splits and dividends. Publishing an
    adjusted close alongside raw highs and lows makes true range difference
    two different price scales, which inflated ATR by more than 10x on real
    ETF data and corrupted Keltner channels, ATR position sizing and the
    Layer 4 regime features.
    """
    def fake_downloader(symbol, start, end):
        dates = pd.bdate_range(start, end)
        # Adj Close sits 20% below the raw close, as for a long dividend history.
        return pd.DataFrame(
            {
                "Open": 100.0,
                "High": 102.0,
                "Low": 98.0,
                "Close": 100.0,
                "Adj Close": 80.0,
                "Volume": 1_000_000,
            },
            index=dates,
        )

    manager = HistoricalDataManager(data_config, downloader=fake_downloader)
    df = manager.get_history("FAKE")

    # factor = 80/100 = 0.8
    assert df["Close"].iloc[0] == pytest.approx(80.0)
    assert df["High"].iloc[0] == pytest.approx(102.0 * 0.8)
    assert df["Low"].iloc[0] == pytest.approx(98.0 * 0.8)
    assert df["Open"].iloc[0] == pytest.approx(100.0 * 0.8)
    # ...and the bar must remain internally coherent: Low <= Close <= High
    assert (df["Low"] <= df["Close"]).all()
    assert (df["Close"] <= df["High"]).all()


def test_adjusted_bars_keep_atr_on_a_sane_scale(data_config):
    """ATR as a fraction of price must stay plausible (~1%), not ~20%."""
    from quant_backtester.src.strategies import atr

    def fake_downloader(symbol, start, end):
        dates = pd.bdate_range(start, end)
        return pd.DataFrame(
            {
                "Open": 100.0,
                "High": 101.0,
                "Low": 99.0,
                "Close": 100.0,
                "Adj Close": 80.0,
                "Volume": 1_000_000,
            },
            index=dates,
        )

    manager = HistoricalDataManager(data_config, downloader=fake_downloader)
    df = manager.get_history("FAKE")
    atr_fraction = (atr(df["High"], df["Low"], df["Close"], 14) / df["Close"]).dropna()
    assert (atr_fraction < 0.05).all(), "ATR/price should be a small percentage"
