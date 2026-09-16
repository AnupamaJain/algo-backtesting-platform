"""Engine, session factory and declarative base.

The deployment target is PostgreSQL (with a TimescaleDB hypertable on
``ohlcv_daily``); local development and the test suite run on SQLite. The
schema is written to the portable subset -- explicit ``Numeric``, timezone
aware ``DateTime``, named constraints -- so that moving between them is a URL
change rather than a migration rewrite. See docs/01-system-architecture.md §9.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..config import get_config

#: Explicit naming convention so that Alembic autogenerate produces stable,
#: reversible names. Without it SQLite invents anonymous constraints that
#: cannot be dropped by name in a downgrade.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_connection, _record) -> None:
    """SQLite defaults are wrong for this workload in two ways.

    Foreign keys are off by default, which would let the ORM write an orphaned
    feature row and only discover it in production on PostgreSQL. WAL improves
    concurrent read behaviour while a nightly ingest is writing.

    busy_timeout matters more than it looks. WAL lets readers run during a
    write, but two writers still collide, and SQLite's default is to fail the
    statement immediately rather than wait. With a nightly backfill holding
    the write lock in bursts, that turned an ordinary login into a 500 the
    moment it tried to stamp last_login_at. Five seconds is far longer than
    any single scan's commit.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def get_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    global _engine, _SessionFactory
    if _engine is not None and url is None:
        return _engine

    resolved = url or get_config().database_url
    engine = create_engine(resolved, echo=echo, future=True)

    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _configure_sqlite)

    if url is None:
        _engine = engine
        _SessionFactory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def get_session_factory() -> sessionmaker[Session]:
    if _SessionFactory is None:
        get_engine()
    assert _SessionFactory is not None
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on any exception.

    Ingestion writes thousands of rows per symbol; a half-applied symbol is
    worse than a failed one, because the gap is invisible afterwards.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Drop cached engine/session factory. Tests only."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
