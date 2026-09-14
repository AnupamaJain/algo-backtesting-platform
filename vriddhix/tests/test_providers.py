"""Provider contract and the failover chain."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from vriddhix.data.providers import (
    ChainedProvider,
    CsvCacheProvider,
    OHLCVProvider,
    ProviderError,
    YFinanceProvider,
    build_provider,
)
from vriddhix.features.technical import validate_price_frame
from conftest import make_ohlcv


class FakeProvider(OHLCVProvider):
    """Returns a fixed frame. Tests never touch the network."""

    def __init__(self, name: str, frame: pd.DataFrame | None = None, *, fail: bool = False,
                 usable: bool = True):
        self.name = name
        self._frame = frame if frame is not None else make_ohlcv(120)
        self._fail = fail
        self._usable = usable
        self.calls = 0

    def available(self) -> bool:
        return self._usable

    def fetch(self, symbol, start=None, end=None):
        self.calls += 1
        if self._fail:
            raise ProviderError(self.name, symbol, "simulated failure")
        return self._slice(self._frame, start, end)


# ---------------------------------------------------------------------------
# CSV cache -- exercised against the real on-disk NSE data
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_dir(cfg):
    if not cfg.cache_dir.is_dir():
        pytest.skip(f"NSE cache not present at {cfg.cache_dir}")
    return cfg.cache_dir


def test_csv_provider_reads_real_nse_data(cache_dir):
    frame = CsvCacheProvider(cache_dir).fetch("RELIANCE")
    assert len(frame) > 500
    validate_price_frame(frame, symbol="RELIANCE")


def test_csv_provider_accepts_either_symbol_spelling(cache_dir):
    """The cache carries Flattrade's '-EQ' suffix; callers use the plain
    exchange symbol."""
    provider = CsvCacheProvider(cache_dir)
    plain = provider.fetch("RELIANCE")
    suffixed = provider.fetch("RELIANCE-EQ")
    pd.testing.assert_frame_equal(plain, suffixed)


def test_csv_provider_normalises_columns(cache_dir):
    frame = CsvCacheProvider(cache_dir).fetch("TCS")
    assert set(["open", "high", "low", "close", "volume"]).issubset(frame.columns)
    assert "close_raw" not in frame.columns  # provider-specific extras dropped


def test_csv_provider_respects_the_date_window(cache_dir):
    frame = CsvCacheProvider(cache_dir).fetch(
        "RELIANCE", start=date(2023, 1, 1), end=date(2023, 12, 31)
    )
    assert frame.index[0].year == 2023
    assert frame.index[-1].year == 2023


def test_csv_provider_reports_a_missing_symbol(cache_dir):
    with pytest.raises(ProviderError, match="no cached file"):
        CsvCacheProvider(cache_dir).fetch("NOSUCHSYMBOL")


def test_csv_provider_unavailable_when_directory_absent(tmp_path):
    assert CsvCacheProvider(tmp_path / "nope").available() is False


# ---------------------------------------------------------------------------
# yfinance symbol mapping (no network)
# ---------------------------------------------------------------------------


def test_yfinance_maps_nse_symbols():
    provider = YFinanceProvider()
    assert provider._ticker_for("RELIANCE") == "RELIANCE.NS"
    assert provider._ticker_for("RELIANCE-EQ") == "RELIANCE.NS"
    assert provider._ticker_for("TCS.NS") == "TCS.NS"


# ---------------------------------------------------------------------------
# Chain
# ---------------------------------------------------------------------------


def test_chain_uses_the_first_provider_that_answers():
    primary = FakeProvider("primary")
    secondary = FakeProvider("secondary")
    chain = ChainedProvider([primary, secondary])

    chain.fetch("X")
    assert primary.calls == 1
    assert secondary.calls == 0
    assert chain.last_source == "primary"


def test_chain_fails_over_and_records_why():
    primary = FakeProvider("primary", fail=True)
    secondary = FakeProvider("secondary")
    chain = ChainedProvider([primary, secondary])

    chain.fetch("X")
    assert chain.last_source == "secondary"
    # The failure is recorded rather than swallowed -- it explains why the
    # answer came from the fallback.
    assert any(f.detail.get("provider") == "primary" for f in chain.findings)


def test_chain_skips_unavailable_providers():
    unusable = FakeProvider("unusable", usable=False)
    working = FakeProvider("working")
    chain = ChainedProvider([unusable, working])

    chain.fetch("X")
    assert unusable.calls == 0
    assert chain.last_source == "working"


def test_chain_error_names_every_provider_that_failed():
    """A five-minute diagnosis instead of an hour of guessing."""
    chain = ChainedProvider([FakeProvider("a", fail=True), FakeProvider("b", fail=True)])
    with pytest.raises(ProviderError) as exc:
        chain.fetch("X")
    assert "a" in str(exc.value) and "b" in str(exc.value)


def test_chain_requires_at_least_one_provider():
    with pytest.raises(ValueError):
        ChainedProvider([])


def test_build_provider_follows_configuration(cfg):
    chain = build_provider(cfg)
    assert [p.name for p in chain.providers] == list(cfg.get("data.providers"))


def test_build_provider_rejects_unknown_names(cfg, monkeypatch, tmp_path):
    import textwrap

    from vriddhix.config import load_config

    path = tmp_path / "cfg.yaml"
    path.write_text(
        textwrap.dedent(
            """
            market: {primary_exchange: NSE}
            data: {providers: [wat], cache_dir: ./x}
            features:
              ema_periods: [20]
              atr_period: 14
              volume_avg_periods: [20]
              return_periods: {ret_1m: 21}
              high_low_lookback: 252
            universe: {default_index: NIFTY500}
            quality: {suspected_split_pct: 25, zero_volume_severity: WARN,
                      stale_after_days: 5, max_missing_bar_streak: 5}
            """
        )
    )
    with pytest.raises(ValueError, match="unknown data provider"):
        build_provider(load_config(path))


def test_an_empty_answer_falls_through_to_the_next_provider(tmp_path):
    """A stale cache must not shadow the live provider behind it.

    Asked for bars after its last row, a CSV cache returns zero rows rather
    than raising. If the chain accepted that, the platform would stop
    backfilling the moment the cache fell behind -- silently, and with no
    error to notice.
    """
    from vriddhix.data.providers import ChainedProvider

    stale = FakeProvider("stale", make_ohlcv(5, start=date(2020, 1, 1)))
    live = FakeProvider("live", make_ohlcv(5, start=date(2026, 9, 3)))

    chain = ChainedProvider([stale, live])
    frame = chain.fetch("RELIANCE", start=date(2026, 9, 3))

    assert not frame.empty
    assert chain.last_source == "live"


def test_every_provider_returning_nothing_is_an_error_naming_each(tmp_path):
    from vriddhix.data.providers import ChainedProvider, ProviderError

    a = FakeProvider("a", make_ohlcv(5, start=date(2020, 1, 1)))
    b = FakeProvider("b", make_ohlcv(5, start=date(2020, 1, 1)))

    with pytest.raises(ProviderError) as exc:
        ChainedProvider([a, b]).fetch("RELIANCE", start=date(2026, 9, 3))
    assert "a: no bars in range" in str(exc.value)
    assert "b: no bars in range" in str(exc.value)
