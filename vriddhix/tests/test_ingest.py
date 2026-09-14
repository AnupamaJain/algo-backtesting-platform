"""Ingestion pipeline: idempotence, partial failure, quality recording."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from sqlalchemy import func, select

from vriddhix.data.ingest import ingest_symbol, ingest_universe
from vriddhix.data.providers import ChainedProvider
from vriddhix.db.models import DataQualityEvent, OhlcvDaily, TechnicalFeature
from conftest import make_ohlcv
from test_providers import FakeProvider


@pytest.fixture
def provider():
    return ChainedProvider([FakeProvider("fake", make_ohlcv(300, seed=77))])


def bars_for(session, stock) -> int:
    return session.scalar(
        select(func.count()).select_from(OhlcvDaily).where(OhlcvDaily.stock_id == stock.id)
    )


def features_for(session, stock) -> int:
    return session.scalar(
        select(func.count()).select_from(TechnicalFeature)
        .where(TechnicalFeature.stock_id == stock.id)
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_ingest_writes_bars_and_features(session, seeded, cfg, provider):
    result = ingest_symbol(session, "RELIANCE", provider, cfg)

    assert result.ok
    assert result.bars_written == 300
    assert result.features_written == 300
    assert bars_for(session, seeded["stocks"]["RELIANCE"]) == 300
    assert features_for(session, seeded["stocks"]["RELIANCE"]) == 300


def test_bars_record_their_provider(session, seeded, cfg, provider):
    """'Which source said this?' is the first question when two numbers
    disagree."""
    ingest_symbol(session, "RELIANCE", provider, cfg)
    bar = session.scalar(select(OhlcvDaily))
    assert bar.provider == "fake"


def test_features_record_their_version(session, seeded, cfg, provider):
    from vriddhix.versioning import FEATURES_VERSION

    ingest_symbol(session, "RELIANCE", provider, cfg)
    row = session.scalar(select(TechnicalFeature))
    assert row.feature_version == FEATURES_VERSION


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_re_ingesting_converges_rather_than_duplicating(session, seeded, cfg, provider):
    """A nightly job re-run after a partial failure must not double-count."""
    ingest_symbol(session, "RELIANCE", provider, cfg)
    first = bars_for(session, seeded["stocks"]["RELIANCE"])

    ingest_symbol(session, "RELIANCE", provider, cfg)
    assert bars_for(session, seeded["stocks"]["RELIANCE"]) == first


def test_re_ingesting_replaces_stale_values(session, seeded, cfg):
    """Delete-then-insert over the window: a re-run cannot leave a mixture of
    two vintages, which is what makes a second run produce different numbers
    from the first."""
    original = make_ohlcv(200, seed=5)
    ingest_symbol(session, "TCS", ChainedProvider([FakeProvider("v1", original)]), cfg)

    revised = original.copy()
    revised["close"] = revised["close"] * 1.10
    revised["high"] = revised[["high", "close"]].max(axis=1)
    ingest_symbol(session, "TCS", ChainedProvider([FakeProvider("v2", revised)]), cfg)

    stock = seeded["stocks"]["TCS"]
    assert bars_for(session, stock) == 200
    newest = session.scalar(
        select(OhlcvDaily).where(OhlcvDaily.stock_id == stock.id)
        .order_by(OhlcvDaily.date.desc())
    )
    assert newest.provider == "v2"
    assert float(newest.close) == pytest.approx(float(revised["close"].iloc[-1]), rel=1e-6)


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_unknown_symbol_fails_without_raising(session, cfg, provider):
    result = ingest_symbol(session, "NOTINDB", provider, cfg)
    assert result.ok is False
    assert "not in stocks table" in result.error


def test_provider_failure_is_recorded_as_a_quality_event(session, seeded, cfg):
    chain = ChainedProvider([FakeProvider("broken", fail=True)])
    result = ingest_symbol(session, "RELIANCE", chain, cfg)

    assert result.ok is False
    events = session.scalars(select(DataQualityEvent)).all()
    assert any(e.category == "PROVIDER_ERROR" and e.severity == "ERROR" for e in events)


def test_too_little_history_is_refused_rather_than_computed(session, seeded, cfg):
    """Features over a dozen bars are arithmetically valid and analytically
    meaningless, and nothing downstream could tell the difference."""
    chain = ChainedProvider([FakeProvider("short", make_ohlcv(20))])
    result = ingest_symbol(session, "RELIANCE", chain, cfg)

    assert result.ok is False
    assert "minimum" in result.error
    assert bars_for(session, seeded["stocks"]["RELIANCE"]) == 0


def test_one_failure_does_not_stop_the_rest(session, seeded, cfg):
    class Selective(FakeProvider):
        def fetch(self, symbol, start=None, end=None):
            if symbol == "TCS":
                from vriddhix.data.providers import ProviderError
                raise ProviderError(self.name, symbol, "simulated outage")
            return super().fetch(symbol, start, end)

    chain = ChainedProvider([Selective("selective", make_ohlcv(300, seed=13))])
    report = ingest_universe(session, ["RELIANCE", "TCS", "INFY"], chain, cfg)

    assert len(report.succeeded) == 2
    assert len(report.failed) == 1
    assert report.failed[0].symbol == "TCS"


# ---------------------------------------------------------------------------
# Quality integration
# ---------------------------------------------------------------------------


def test_quality_findings_are_persisted(session, seeded, cfg):
    df = make_ohlcv(300, seed=21)
    df.iloc[100, df.columns.get_loc("volume")] = 0.0  # a halt

    chain = ChainedProvider([FakeProvider("fake", df)])
    result = ingest_symbol(session, "RELIANCE", chain, cfg)

    assert result.ok
    events = session.scalars(select(DataQualityEvent)).all()
    assert any(e.category == "ZERO_VOLUME" for e in events)
    # Flagged, not dropped -- the halt is a real session.
    assert bars_for(session, seeded["stocks"]["RELIANCE"]) == 300


def test_impossible_bars_are_dropped_before_persistence(session, seeded, cfg):
    df = make_ohlcv(300, seed=22)
    df.iloc[50, df.columns.get_loc("high")] = df.iloc[50]["low"] - 10

    chain = ChainedProvider([FakeProvider("fake", df)])
    result = ingest_symbol(session, "RELIANCE", chain, cfg)

    assert result.ok
    assert bars_for(session, seeded["stocks"]["RELIANCE"]) == 299
    events = session.scalars(select(DataQualityEvent)).all()
    assert any(e.category == "INVALID_OHLC" for e in events)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_report_summarises_the_run(session, seeded, cfg, provider):
    report = ingest_universe(session, ["RELIANCE", "INFY"], provider, cfg)
    assert report.bars_written == 600
    assert "2/2 symbols" in report.summary()
    assert report.finished_at is not None
