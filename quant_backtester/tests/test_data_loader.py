

def test_a_dead_broker_session_does_not_stop_cached_data_being_read(tmp_path):
    """The cache exists so an outage is survivable.

    Building the downloader authenticates; doing that eagerly in __init__
    made the whole class unusable when a token lapsed, even though every
    symbol needed was already on disk.
    """
    import pandas as pd
    from quant_backtester.src.config import DataConfig
    from quant_backtester.src.data_loader import HistoricalDataManager

    cache = tmp_path / "cache"
    cache.mkdir()
    index = pd.date_range("2024-01-01", periods=50, freq="B")
    frame = pd.DataFrame(
        {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0,
         "Adj Close": 1.0, "Volume": 100},
        index=index,
    )
    frame.to_csv(cache / "AAA.csv")
    (cache / "AAA.meta.json").write_text(
        '{"requested_start": "2024-01-01", "requested_end": "2024-03-08"}'
    )

    config = DataConfig(
        cache_dir=cache, source="flattrade", lookback_years=1,
        start_date=index[0].date(), end_date=index[-1].date(),
        price_field="Close", max_forward_fill_days=3,
    )

    def exploding_downloader(*args, **kwargs):
        raise RuntimeError("broker session invalid")

    manager = HistoricalDataManager(config, downloader=exploding_downloader)
    data = manager.get_history("AAA")
    assert not data.empty, "cached data must still be readable"


def test_the_downloader_is_not_built_until_a_download_is_needed(tmp_path, monkeypatch):
    from quant_backtester.src.config import DataConfig
    from quant_backtester.src.data_loader import HistoricalDataManager
    import quant_backtester.src.data_loader as loader

    built = []

    def spy(config):
        built.append(True)
        raise RuntimeError("must not be constructed at __init__ time")

    monkeypatch.setattr(loader, "_downloader_for", spy)
    config = DataConfig(
        cache_dir=tmp_path, source="flattrade", lookback_years=1,
        start_date=None, end_date=None, price_field="Close",
        max_forward_fill_days=3,
    )
    HistoricalDataManager(config)          # must not raise
    assert built == [], "downloader was constructed eagerly"


# -- ingestion fallback -------------------------------------------------


def _frame(n=5):
    import pandas as pd

    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5,
         "Adj Close": 1.5, "Volume": 100},
        index=idx,
    )


def test_a_dead_primary_source_falls_through_to_the_backup():
    """Ingestion is the one place an outage cannot be covered by a cache:
    missing bars are missing. A second broker on the same market keeps the
    run alive."""
    from datetime import date

    from quant_backtester.src.data_loader import ChainedDownloader

    def dead():
        raise RuntimeError("session invalid")

    chain = ChainedDownloader([("flattrade", dead), ("dhan", lambda: (lambda s, a, b: _frame()))])
    out = chain("RELIANCE-EQ", date(2026, 1, 1), date(2026, 1, 8))

    assert len(out) == 5
    assert chain.last_source == "dhan"


def test_the_primary_is_used_when_it_works():
    from datetime import date

    from quant_backtester.src.data_loader import ChainedDownloader

    touched = []
    chain = ChainedDownloader([
        ("flattrade", lambda: (lambda s, a, b: _frame())),
        ("dhan", lambda: touched.append(1) or (lambda s, a, b: _frame())),
    ])
    chain("X", date(2026, 1, 1), date(2026, 1, 8))

    assert chain.last_source == "flattrade"
    assert touched == [], "the fallback was built despite the primary working"


def test_an_empty_response_counts_as_a_failure_and_moves_on():
    """A source that returns zero rows has not supplied the history; treating
    that as success would cache an empty file and poison later runs."""
    import pandas as pd
    from datetime import date

    from quant_backtester.src.data_loader import ChainedDownloader

    chain = ChainedDownloader([
        ("flattrade", lambda: (lambda s, a, b: pd.DataFrame())),
        ("dhan", lambda: (lambda s, a, b: _frame())),
    ])
    assert len(chain("X", date(2026, 1, 1), date(2026, 1, 8))) == 5
    assert chain.last_source == "dhan"


def test_every_source_failing_raises_rather_than_returning_empty():
    from datetime import date

    import pytest

    from quant_backtester.src.data_loader import ChainedDownloader, DataDownloadError

    def dead():
        raise RuntimeError("no session")

    chain = ChainedDownloader([("flattrade", dead), ("dhan", dead)])
    with pytest.raises(DataDownloadError, match="no data source"):
        chain("X", date(2026, 1, 1), date(2026, 1, 8))


def test_a_broken_source_is_built_once_not_per_symbol():
    from datetime import date

    from quant_backtester.src.data_loader import ChainedDownloader

    attempts = []

    def dead():
        attempts.append(1)
        raise RuntimeError("session invalid")

    chain = ChainedDownloader([("bad", dead), ("good", lambda: (lambda s, a, b: _frame()))])
    for sym in ("A", "B", "C"):
        chain(sym, date(2026, 1, 1), date(2026, 1, 8))

    assert len(attempts) == 1, "dead source re-authenticated for every symbol"


def test_dhan_column_arrays_become_a_canonical_frame():
    from quant_backtester.src.broker.dhan import _arrays_to_frame
    from quant_backtester.src.data_loader import CANONICAL_COLUMNS

    frame = _arrays_to_frame({
        "open": [1, 2], "high": [2, 3], "low": [0.5, 1], "close": [1.5, 2.5],
        "volume": [10, 20], "timestamp": [1725321600, 1725408000],
    })
    assert list(frame.columns) == CANONICAL_COLUMNS
    assert len(frame) == 2
    assert frame.index.tz is None, "index must be tz-naive like every other source"


def test_ragged_dhan_arrays_are_discarded_not_zipped():
    """Unequal-length arrays would pair the wrong price with the wrong day."""
    from quant_backtester.src.broker.dhan import _arrays_to_frame

    assert _arrays_to_frame({
        "open": [1], "high": [1, 2], "low": [1], "close": [1], "timestamp": [1]
    }).empty
