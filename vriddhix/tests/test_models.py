"""Schema behaviour: constraints, cascades, and migration/model agreement."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from vriddhix.db.models import (
    DataQualityEvent,
    Industry,
    MarketIndex,
    OhlcvDaily,
    ScanRun,
    Sector,
    Stock,
    TechnicalFeature,
    UniverseMember,
    UniverseSnapshot,
)


EXPECTED_TABLES = {
    "sectors", "industries", "stocks", "indices",
    "universe_members", "universe_snapshots",
    "ohlcv_daily", "technical_features",
    "scan_runs", "data_quality_events",
}


def test_migration_creates_every_expected_table(session):
    """The schema under test came from Alembic (see conftest), so this also
    proves the migration and the models have not drifted apart."""
    names = set(inspect(session.get_bind()).get_table_names())
    missing = EXPECTED_TABLES - names
    assert not missing, f"migration did not create: {missing}"


def test_every_model_maps_to_a_migrated_table(session):
    names = set(inspect(session.get_bind()).get_table_names())
    for model in (Sector, Industry, Stock, MarketIndex, UniverseMember, UniverseSnapshot,
                  OhlcvDaily, TechnicalFeature, ScanRun, DataQualityEvent):
        assert model.__tablename__ in names


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


def test_symbol_is_unique(session, seeded):
    session.add(Stock(symbol="RELIANCE", name="Duplicate", exchange="NSE"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_index_code_is_unique(session, seeded):
    session.add(MarketIndex(code="NIFTY500", name="Duplicate"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_bar_primary_key_prevents_duplicate_dates(session, seeded):
    stock = seeded["stocks"]["RELIANCE"]
    bar = dict(stock_id=stock.id, date=date(2024, 1, 1), open=1, high=2, low=0.5,
               close=1.5, volume=10, provider="test")
    session.add(OhlcvDaily(**bar))
    session.flush()
    session.add(OhlcvDaily(**bar))
    with pytest.raises(IntegrityError):
        session.flush()


def test_high_below_low_is_rejected_by_the_database(session, seeded):
    """Defence in depth: the quality layer filters these, and the schema
    refuses them anyway."""
    session.add(
        OhlcvDaily(stock_id=seeded["stocks"]["TCS"].id, date=date(2024, 1, 2),
                   open=10, high=5, low=9, close=8, volume=1, provider="test")
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_negative_volume_is_rejected(session, seeded):
    session.add(
        OhlcvDaily(stock_id=seeded["stocks"]["TCS"].id, date=date(2024, 1, 3),
                   open=10, high=11, low=9, close=10, volume=-5, provider="test")
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_membership_interval_must_be_ordered(session, seeded):
    session.add(
        UniverseMember(
            index_id=seeded["index"].id,
            stock_id=seeded["stocks"]["TCS"].id,
            effective_from=date(2024, 1, 1),
            effective_to=date(2023, 1, 1),  # before it started
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_snapshot_version_is_unique(session, seeded):
    for _ in range(2):
        session.add(UniverseSnapshot(index_id=seeded["index"].id, version="V1",
                                     as_of=date(2024, 1, 1), member_count=3))
    with pytest.raises(IntegrityError):
        session.flush()


# ---------------------------------------------------------------------------
# Cascades and retention
# ---------------------------------------------------------------------------


def test_deleting_a_stock_cascades_to_its_bars_and_features(session, seeded):
    """Price and feature rows follow the stock. Uses a stock with no index
    membership, so that this exercises the cascade rather than the membership
    guard tested below."""
    orphan = Stock(symbol="ORPHAN", name="No Index Co", exchange="NSE")
    session.add(orphan)
    session.flush()

    session.add(OhlcvDaily(stock_id=orphan.id, date=date(2024, 1, 1), open=1, high=2,
                           low=0.5, close=1.5, volume=10, provider="test"))
    session.add(TechnicalFeature(stock_id=orphan.id, date=date(2024, 1, 1),
                                 ema_20=1.4, feature_version="FEATURES_V1.0"))
    session.flush()

    session.execute(text("DELETE FROM stocks WHERE id = :i"), {"i": orphan.id})
    session.flush()

    assert session.scalar(select(OhlcvDaily).where(OhlcvDaily.stock_id == orphan.id)) is None
    assert session.scalar(
        select(TechnicalFeature).where(TechnicalFeature.stock_id == orphan.id)
    ) is None


def test_deleting_a_stock_with_index_membership_is_refused(session, seeded):
    """``universe_members`` deliberately has no ON DELETE CASCADE.

    Membership history is the record that a stock *was* in the index, which is
    exactly what a point-in-time backtest depends on. Letting a stock deletion
    silently take it would reintroduce survivorship bias through the back
    door, so the database refuses instead.
    """
    stock = seeded["stocks"]["INFY"]
    with pytest.raises(IntegrityError):
        session.execute(text("DELETE FROM stocks WHERE id = :i"), {"i": stock.id})
        session.flush()


def test_delisted_stocks_are_retained_not_deleted(session, seeded):
    """Removing delisted names is how survivorship bias becomes unrecoverable."""
    delisted = session.scalar(select(Stock).where(Stock.symbol == "DELISTED"))
    assert delisted is not None
    assert delisted.is_active is False
    assert delisted.delisted_on == date(2023, 6, 30)


# ---------------------------------------------------------------------------
# Versioning and ops
# ---------------------------------------------------------------------------


def test_feature_rows_carry_their_version(session, seeded):
    session.add(
        TechnicalFeature(stock_id=seeded["stocks"]["TCS"].id, date=date(2024, 1, 1),
                         ema_20=100.0, feature_version="FEATURES_V1.0")
    )
    session.flush()
    row = session.scalar(select(TechnicalFeature))
    assert row.feature_version == "FEATURES_V1.0"


def test_feature_version_is_required(session, seeded):
    session.add(TechnicalFeature(stock_id=seeded["stocks"]["TCS"].id, date=date(2024, 2, 1)))
    with pytest.raises(IntegrityError):
        session.flush()


def test_quality_events_can_be_symbol_agnostic(session):
    """A provider-wide outage is not attributable to one stock."""
    session.add(DataQualityEvent(severity="ERROR", category="PROVIDER_ERROR",
                                 provider="yfinance", detail="rate limited"))
    session.flush()
    assert session.scalar(select(DataQualityEvent)).stock_id is None


def test_scan_run_records_its_engine_version(session):
    session.add(ScanRun(scan_date=date(2024, 5, 1), engine_version="VCP_ENGINE_V1.0"))
    session.flush()
    assert session.scalar(select(ScanRun)).engine_version == "VCP_ENGINE_V1.0"
