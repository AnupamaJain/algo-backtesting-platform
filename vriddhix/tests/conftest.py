"""Shared fixtures.

The schema is built by running Alembic, not ``metadata.create_all``. That is
deliberate: it means every test run exercises the migration, so a migration
that has drifted from the models fails here rather than in a deployment.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def migrated_db(tmp_path_factory) -> str:
    """A SQLite database with the full schema applied via Alembic."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    db_path = tmp_path_factory.mktemp("vriddhix-db") / "test.db"
    url = f"sqlite:///{db_path}"
    os.environ["VRIDDHIX_DATABASE_URL"] = url

    alembic_cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    command.upgrade(alembic_cfg, "head")
    return url


@pytest.fixture
def session(migrated_db):
    """A session in a transaction that is always rolled back.

    Tests therefore share one migrated schema but never each other's rows,
    which keeps them order-independent without paying for a migration each.
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session

    engine = create_engine(migrated_db, future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection, expire_on_commit=False, future=True)
    try:
        yield db
    finally:
        db.close()
        # A test that provoked an IntegrityError has already had its
        # transaction deassociated; rolling back again only warns.
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        engine.dispose()


@pytest.fixture
def cfg():
    from vriddhix.config import load_config

    return load_config()


# ---------------------------------------------------------------------------
# Synthetic price data
# ---------------------------------------------------------------------------


def make_ohlcv(
    bars: int = 300,
    *,
    start: date = date(2024, 1, 1),
    start_price: float = 100.0,
    pattern: str = "uptrend",
    seed: int = 42,
    base_volume: float = 1_000_000.0,
) -> pd.DataFrame:
    """Deterministic synthetic OHLCV obeying the price-frame contract.

    Seeded so that a failing assertion is reproducible -- an intermittently
    failing numeric test teaches nothing.
    """
    rng = np.random.default_rng(seed)

    drift = {"uptrend": 0.0012, "downtrend": -0.0012, "chop": 0.0, "flat": 0.0}.get(pattern, 0.0)
    vol = 0.004 if pattern == "flat" else 0.012

    returns = rng.normal(drift, vol, bars)
    close = start_price * np.exp(np.cumsum(returns))

    spread = np.abs(rng.normal(0, 0.006, bars)) * close
    open_ = close * (1 + rng.normal(0, 0.003, bars))
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    low = np.maximum(low, 0.01)

    volume = np.abs(rng.normal(base_volume, base_volume * 0.25, bars))

    # Business days only: weekend bars would be a data-quality finding in
    # every test that touched them.
    index = pd.bdate_range(start=pd.Timestamp(start), periods=bars, freq="B")

    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    ).rename_axis("date")


@pytest.fixture
def price_frame() -> pd.DataFrame:
    return make_ohlcv(300)


@pytest.fixture
def short_frame() -> pd.DataFrame:
    return make_ohlcv(40)


# ---------------------------------------------------------------------------
# Seeded reference data
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded(session):
    """A minimal but realistic reference graph.

    Includes DELISTED, a stock that was in the index and left it -- the case
    that separates a point-in-time universe from today's list.
    """
    from vriddhix.db.models import Industry, MarketIndex, Sector, Stock, UniverseMember

    energy = Sector(code="ENERGY", name="Energy")
    tech = Sector(code="IT", name="Information Technology")
    session.add_all([energy, tech])
    session.flush()

    refining = Industry(sector_id=energy.id, code="REFINING", name="Refineries")
    software = Industry(sector_id=tech.id, code="SOFTWARE", name="Software Services")
    session.add_all([refining, software])
    session.flush()

    stocks = {
        "RELIANCE": Stock(symbol="RELIANCE", name="Reliance Industries",
                          exchange="NSE", industry_id=refining.id),
        "TCS": Stock(symbol="TCS", name="Tata Consultancy Services",
                     exchange="NSE", industry_id=software.id),
        "INFY": Stock(symbol="INFY", name="Infosys",
                      exchange="NSE", industry_id=software.id),
        "DELISTED": Stock(symbol="DELISTED", name="Gone Co",
                          exchange="NSE", industry_id=software.id,
                          is_active=False, delisted_on=date(2023, 6, 30)),
    }
    session.add_all(stocks.values())
    session.flush()

    nifty500 = MarketIndex(code="NIFTY500", name="Nifty 500", is_benchmark=True)
    session.add(nifty500)
    session.flush()

    session.add_all([
        UniverseMember(index_id=nifty500.id, stock_id=stocks["RELIANCE"].id,
                       effective_from=date(2020, 1, 1)),
        UniverseMember(index_id=nifty500.id, stock_id=stocks["TCS"].id,
                       effective_from=date(2020, 1, 1)),
        UniverseMember(index_id=nifty500.id, stock_id=stocks["INFY"].id,
                       effective_from=date(2022, 6, 1)),
        # In the index 2020-2023, then removed. Must still appear for 2021.
        UniverseMember(index_id=nifty500.id, stock_id=stocks["DELISTED"].id,
                       effective_from=date(2020, 1, 1), effective_to=date(2023, 7, 1)),
    ])
    session.flush()

    return {"sectors": {"ENERGY": energy, "IT": tech}, "stocks": stocks, "index": nifty500}


# ---------------------------------------------------------------------------
# Pattern construction -- explicit waypoints, so the expected answer is
# verifiable by reading the test rather than by trusting a snapshot.
# ---------------------------------------------------------------------------


def build_path(waypoints: list[tuple[float, int]]) -> np.ndarray:
    """Piecewise-linear close series through (price, bars_to_reach) waypoints."""
    prices: list[float] = [waypoints[0][0]]
    current = waypoints[0][0]
    for price, bars in waypoints[1:]:
        prices.extend(np.linspace(current, price, bars + 1)[1:])
        current = price
    return np.asarray(prices, dtype=float)


def frame_from_closes(
    closes: np.ndarray,
    *,
    start: date = date(2022, 1, 3),
    volumes: np.ndarray | None = None,
    wiggle: float = 0.002,
) -> pd.DataFrame:
    """Wrap a close path in a valid OHLC frame.

    The wiggle is proportional and symmetric, so it never reorders highs and
    lows relative to the underlying path -- a swing at a waypoint stays a
    swing.
    """
    n = len(closes)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) * (1 + wiggle)
    lows = np.minimum(opens, closes) * (1 - wiggle)
    if volumes is None:
        volumes = np.full(n, 1_000_000.0)

    index = pd.bdate_range(start=pd.Timestamp(start), periods=n, freq="B")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=index,
    ).rename_axis("date")


def make_vcp(
    *,
    prior_bars: int = 260,
    prior_advance_pct: float = 100.0,
    base_waypoints: list[tuple[float, int]] | None = None,
    base_price: float = 200.0,
    prior_volume: float = 2_000_000.0,
    base_volume: float = 900_000.0,
    start: date = date(2021, 1, 4),
) -> pd.DataFrame:
    """A frame containing a prior uptrend followed by a contracting base.

    Default base contracts 9.1% -> 4.1% -> 2.6%, which is a textbook VCP.
    """
    if base_waypoints is None:
        base_waypoints = [
            (base_price, 0),
            (186.0, 10),   # initial drop off the base high
            (198.0, 10),
            (180.0, 8),    # 9.1% contraction
            (196.0, 8),
            (188.0, 6),    # 4.1%
            (193.0, 6),
            (188.0, 5),    # 2.6%
            (192.0, 5),
        ]

    prior_start = base_price / (1 + prior_advance_pct / 100.0)
    prior = build_path([(prior_start, 0), (base_price, prior_bars)])
    base = build_path(base_waypoints)[1:]  # waypoint 0 is the prior's last bar

    closes = np.concatenate([prior, base])
    volumes = np.concatenate([
        np.full(len(prior), prior_volume),
        np.full(len(base), base_volume),
    ])
    return frame_from_closes(closes, start=start, volumes=volumes)
