"""ORM models: reference data, universe, prices, features, patterns,
market context, backtests, user-facing tables and ops.

Tables are added in the phase that first writes to them, never stubbed ahead
of time -- an empty table invites code that reads from it and silently gets
nothing. The full design is docs/03-database-erd.md.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

# Prices: 18,4 comfortably holds MRF at ~150,000 with paise precision.
PRICE = Numeric(18, 4)
# Volume: Indian large-cap daily volume exceeds int32; Numeric avoids the
# float64 precision loss that shows up as an off-by-one in a volume ratio.
VOLUME = Numeric(20, 2)
RATIO = Numeric(18, 6)
PCT = Numeric(12, 6)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------


class Sector(Base, TimestampMixin):
    __tablename__ = "sectors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    industries: Mapped[list[Industry]] = relationship(back_populates="sector")

    def __repr__(self) -> str:
        return f"<Sector {self.code}>"


class Industry(Base, TimestampMixin):
    __tablename__ = "industries"
    __table_args__ = (UniqueConstraint("sector_id", "code", name="uq_industries_sector_id_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sector_id: Mapped[int] = mapped_column(ForeignKey("sectors.id"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(48), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    sector: Mapped[Sector] = relationship(back_populates="industries")
    stocks: Mapped[list[Stock]] = relationship(back_populates="industry")


class Stock(Base, TimestampMixin):
    """One tradable instrument.

    Delisted names are retained with ``is_active=False`` rather than deleted:
    removing them is exactly how survivorship bias enters a research database,
    and it is unrecoverable once the rows are gone.
    """

    __tablename__ = "stocks"
    __table_args__ = (
        Index("ix_stocks_exchange_is_active", "exchange", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    isin: Mapped[str | None] = mapped_column(String(12), nullable=True, unique=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, default="NSE")
    industry_id: Mapped[int | None] = mapped_column(
        ForeignKey("industries.id"), nullable=True, index=True
    )
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    listed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    delisted_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    industry: Mapped[Industry | None] = relationship(back_populates="stocks")
    bars: Mapped[list[OhlcvDaily]] = relationship(
        back_populates="stock", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Stock {self.symbol}>"


class MarketIndex(Base, TimestampMixin):
    """A market index (Nifty 500, ...).

    Named ``MarketIndex`` rather than ``Index`` because ``sqlalchemy.Index``
    is used for database indexes in ``__table_args__`` throughout this module;
    a class named ``Index`` silently shadows it and the declaration fails with
    an unhelpful arity error.
    """

    __tablename__ = "indices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_benchmark: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    members: Mapped[list[UniverseMember]] = relationship(back_populates="index")


# ---------------------------------------------------------------------------
# Universe -- point-in-time membership
# ---------------------------------------------------------------------------


class UniverseMember(Base):
    """Index membership over an interval. The survivorship fix.

    ``effective_to IS NULL`` means still a member. A backtest resolves
    membership per date against this table instead of against today's list,
    which is the difference between measuring a strategy and measuring the
    fact that today's index members are the ones that survived.
    """

    __tablename__ = "universe_members"
    __table_args__ = (
        UniqueConstraint(
            "index_id", "stock_id", "effective_from",
            name="uq_universe_members_index_id_stock_id_effective_from",
        ),
        Index("ix_universe_members_index_id_effective_from_effective_to",
              "index_id", "effective_from", "effective_to"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="effective_range",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    index_id: Mapped[int] = mapped_column(ForeignKey("indices.id"), nullable=False)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), nullable=False, index=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: declared = published index history; inferred = reconstructed;
    #: current_only = today's list backfilled because no history exists.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="declared")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    index: Mapped[MarketIndex] = relationship(back_populates="members")
    stock: Mapped[Stock] = relationship()


class UniverseSnapshot(Base):
    """A named, frozen resolution of an index, for reproducible backtests.

    ``is_complete=False`` marks a snapshot built without real membership
    history; any run against it must surface a survivorship warning.
    """

    __tablename__ = "universe_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    index_id: Mapped[int] = mapped_column(ForeignKey("indices.id"), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    index: Mapped[MarketIndex] = relationship()


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


class OhlcvDaily(Base):
    """Daily bars. On PostgreSQL this becomes a TimescaleDB hypertable on ``date``.

    Both raw ``close`` and ``adj_close`` are kept. Storing only one makes a
    whole class of question unanswerable later: raw is what printed and what a
    pivot must be compared against, adjusted is what a continuous return
    series needs.
    """

    __tablename__ = "ohlcv_daily"
    __table_args__ = (
        Index("ix_ohlcv_daily_date", "date"),
        CheckConstraint("high >= low", name="high_ge_low"),
        CheckConstraint("volume >= 0", name="volume_non_negative"),
    )

    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)

    open: Mapped[float] = mapped_column(PRICE, nullable=False)
    high: Mapped[float] = mapped_column(PRICE, nullable=False)
    low: Mapped[float] = mapped_column(PRICE, nullable=False)
    close: Mapped[float] = mapped_column(PRICE, nullable=False)
    volume: Mapped[float] = mapped_column(VOLUME, nullable=False, default=0)
    adj_close: Mapped[float | None] = mapped_column(PRICE, nullable=True)
    adj_factor: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False, default=1)

    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    stock: Mapped[Stock] = relationship(back_populates="bars")

    def __repr__(self) -> str:
        return f"<Bar stock={self.stock_id} {self.date} c={self.close}>"


# ---------------------------------------------------------------------------
# Derived features
# ---------------------------------------------------------------------------


class TechnicalFeature(Base):
    """Causal features for one symbol-date. ``feature_version`` is not optional:
    recomputing under a new formula must not silently overwrite the meaning of
    rows that older results were scored against."""

    __tablename__ = "technical_features"
    __table_args__ = (
        Index("ix_technical_features_date", "date"),
        Index("ix_technical_features_date_rel_volume", "date", "rel_volume"),
    )

    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)

    ema_20: Mapped[float | None] = mapped_column(PRICE, nullable=True)
    ema_50: Mapped[float | None] = mapped_column(PRICE, nullable=True)
    ema_100: Mapped[float | None] = mapped_column(PRICE, nullable=True)
    ema_200: Mapped[float | None] = mapped_column(PRICE, nullable=True)

    atr_14: Mapped[float | None] = mapped_column(RATIO, nullable=True)
    atr_pct: Mapped[float | None] = mapped_column(PCT, nullable=True)

    avg_volume_20: Mapped[float | None] = mapped_column(VOLUME, nullable=True)
    avg_volume_50: Mapped[float | None] = mapped_column(VOLUME, nullable=True)
    rel_volume: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)

    ret_1m: Mapped[float | None] = mapped_column(PCT, nullable=True)
    ret_3m: Mapped[float | None] = mapped_column(PCT, nullable=True)
    ret_6m: Mapped[float | None] = mapped_column(PCT, nullable=True)
    ret_12m: Mapped[float | None] = mapped_column(PCT, nullable=True)

    high_52w: Mapped[float | None] = mapped_column(PRICE, nullable=True)
    low_52w: Mapped[float | None] = mapped_column(PRICE, nullable=True)
    pct_from_52w_high: Mapped[float | None] = mapped_column(PCT, nullable=True)
    pct_from_52w_low: Mapped[float | None] = mapped_column(PCT, nullable=True)

    above_ema_20: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    above_ema_50: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    above_ema_100: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    above_ema_200: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    feature_version: Mapped[str] = mapped_column(String(32), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    stock: Mapped[Stock] = relationship()


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


class ScanRun(Base):
    __tablename__ = "scan_runs"
    __table_args__ = (Index("ix_scan_runs_scan_date", "scan_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_date: Mapped[date] = mapped_column(Date, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")

    symbols_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    symbols_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    patterns_detected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    breakouts_detected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    engine_version: Mapped[str] = mapped_column(String(48), nullable=False)
    universe_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("universe_snapshots.id"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class DataQualityEvent(Base):
    """One recorded data problem.

    Recorded rather than raised wherever the bar remains usable: a provider
    hiccup on twelve symbols should degrade those twelve, not abort a nightly
    scan for five hundred. Nothing is ever silently discarded -- that is the
    distinction this table exists to maintain.
    """

    __tablename__ = "data_quality_events"
    __table_args__ = (
        Index("ix_data_quality_events_severity_detected_at", "severity", "detected_at"),
        Index("ix_data_quality_events_stock_id_date", "stock_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[int | None] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), nullable=True
    )
    date: Mapped[date | None] = mapped_column(Date, nullable=True)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(24), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    stock: Mapped[Stock | None] = relationship()


# ---------------------------------------------------------------------------
# Patterns and lifecycle (Phase 2/7)
# ---------------------------------------------------------------------------


class Pattern(Base):
    """A detected base. ``status`` is a denormalised convenience -- the
    authoritative history is the append-only ``pattern_events`` log."""

    __tablename__ = "patterns"
    __table_args__ = (
        Index("ix_patterns_stock_id_detected_on", "stock_id", "detected_on"),
        Index("ix_patterns_status_score", "status", "score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    pattern_type: Mapped[str] = mapped_column(String(24), nullable=False, default="VCP")
    detected_on: Mapped[date] = mapped_column(Date, nullable=False)
    base_start: Mapped[date] = mapped_column(Date, nullable=False)
    base_end: Mapped[date] = mapped_column(Date, nullable=False)
    base_depth_pct: Mapped[float | None] = mapped_column(PCT, nullable=True)
    base_duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contraction_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pivot_price: Mapped[float | None] = mapped_column(PRICE, nullable=True)
    pivot_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="FORMING")
    score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    score_breakdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    engine_version: Mapped[str] = mapped_column(String(48), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(48), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    stock: Mapped[Stock] = relationship()
    events: Mapped[list[PatternEvent]] = relationship(
        back_populates="pattern", cascade="all, delete-orphan", order_by="PatternEvent.event_date"
    )


class PatternEvent(Base):
    """Append-only lifecycle log.

    'How did this become CONFIRMED?' must be answerable from rows rather than
    inferred from a mutated status column, so these are never updated.
    """

    __tablename__ = "pattern_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pattern_id: Mapped[int] = mapped_column(
        ForeignKey("patterns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    pattern: Mapped[Pattern] = relationship(back_populates="events")


class Breakout(Base):
    """The breakout ledger. Failures are never deleted -- they are the most
    valuable rows here for research."""

    __tablename__ = "breakouts"
    __table_args__ = (Index("ix_breakouts_breakout_date", "breakout_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pattern_id: Mapped[int | None] = mapped_column(
        ForeignKey("patterns.id", ondelete="SET NULL"), nullable=True
    )
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    breakout_date: Mapped[date] = mapped_column(Date, nullable=False)
    breakout_price: Mapped[float] = mapped_column(PRICE, nullable=False)
    pivot_price: Mapped[float] = mapped_column(PRICE, nullable=False)
    volume: Mapped[float | None] = mapped_column(VOLUME, nullable=True)
    rel_volume: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    # Snapshotted, not joined later: "what did this look like when it broke
    # out?" must survive later recomputation of RS and regime.
    rs_at_breakout: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    sector_rank_at_breakout: Mapped[int | None] = mapped_column(Integer, nullable=True)
    regime_at_breakout: Mapped[str | None] = mapped_column(String(16), nullable=True)
    setup_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    engine_version: Mapped[str] = mapped_column(String(48), nullable=False)

    stock: Mapped[Stock] = relationship()
    outcome: Mapped[BreakoutOutcome | None] = relationship(
        back_populates="breakout", cascade="all, delete-orphan", uselist=False
    )


class BreakoutOutcome(Base):
    __tablename__ = "breakout_outcomes"

    breakout_id: Mapped[int] = mapped_column(
        ForeignKey("breakouts.id", ondelete="CASCADE"), primary_key=True
    )
    mfe_pct: Mapped[float | None] = mapped_column(PCT, nullable=True)
    mae_pct: Mapped[float | None] = mapped_column(PCT, nullable=True)
    days_held: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(PRICE, nullable=True)
    return_pct: Mapped[float | None] = mapped_column(PCT, nullable=True)
    failed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failure_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    days_to_failure: Mapped[int | None] = mapped_column(Integer, nullable=True)
    drawdown_pct: Mapped[float | None] = mapped_column(PCT, nullable=True)

    breakout: Mapped[Breakout] = relationship(back_populates="outcome")


# ---------------------------------------------------------------------------
# Market structure: SMC and FVG (ERD sections 4 and 5)
# ---------------------------------------------------------------------------


class SwingPointRow(Base):
    """A confirmed swing high or low.

    ``right_bars`` is stored, not assumed. A swing is only confirmed once
    that many bars have printed after it, so a reader needs the value to know
    how late the confirmation was -- the single most common way an SMC
    backtest quietly becomes fiction.
    """

    __tablename__ = "swing_points"
    __table_args__ = (
        UniqueConstraint(
            "stock_id", "timeframe", "date", "swing_type",
            name="uq_swing_points_stock_tf_date_type",
        ),
        Index("ix_swing_points_stock_id_date", "stock_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, default="D1")
    date: Mapped[date] = mapped_column(Date, nullable=False)
    price: Mapped[float] = mapped_column(PRICE, nullable=False)
    swing_type: Mapped[str] = mapped_column(String(8), nullable=False)  # HIGH | LOW
    strength: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    left_bars: Mapped[int] = mapped_column(Integer, nullable=False)
    right_bars: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The date the swing could first be KNOWN, which is `right_bars` bars
    #: after it occurred. Anything reading swings as of a date must filter on
    #: this, not on `date`.
    confirmed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    engine_version: Mapped[str] = mapped_column(String(48), nullable=False)

    stock: Mapped[Stock] = relationship()


class StructureBreakRow(Base):
    """BOS and CHoCH in one table.

    They are the same event shape and differ only in whether the break
    continued the prior structure or reversed it; two tables would mean two
    query paths for one question.
    """

    __tablename__ = "structure_breaks"
    __table_args__ = (
        UniqueConstraint(
            "stock_id", "timeframe", "date", "kind", "direction",
            name="uq_structure_breaks_stock_tf_date_kind_dir",
        ),
        Index("ix_structure_breaks_stock_id_date", "stock_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, default="D1")
    date: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)      # BOS | CHOCH
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # BULLISH | BEARISH
    broken_level: Mapped[float] = mapped_column(PRICE, nullable=False)
    broken_swing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    close_price: Mapped[float | None] = mapped_column(PRICE, nullable=True)
    volume: Mapped[float | None] = mapped_column(VOLUME, nullable=True)
    prior_structure: Mapped[str | None] = mapped_column(String(16), nullable=True)
    engine_version: Mapped[str] = mapped_column(String(48), nullable=False)

    stock: Mapped[Stock] = relationship()


class OrderBlockRow(Base):
    __tablename__ = "order_blocks"
    __table_args__ = (
        UniqueConstraint(
            "stock_id", "timeframe", "date", "direction",
            name="uq_order_blocks_stock_tf_date_dir",
        ),
        Index("ix_order_blocks_stock_id_status", "stock_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, default="D1")
    date: Mapped[date] = mapped_column(Date, nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    upper: Mapped[float] = mapped_column(PRICE, nullable=False)
    lower: Mapped[float] = mapped_column(PRICE, nullable=False)
    origin_break_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    mitigated_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    engine_version: Mapped[str] = mapped_column(String(48), nullable=False)

    stock: Mapped[Stock] = relationship()


class LiquidityEventRow(Base):
    __tablename__ = "liquidity_events"
    __table_args__ = (
        UniqueConstraint(
            "stock_id", "timeframe", "date", "kind", "level",
            name="uq_liquidity_events_stock_tf_date_kind_level",
        ),
        Index("ix_liquidity_events_stock_id_date", "stock_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, default="D1")
    date: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # SWEEP | EQH | EQL
    level: Mapped[float] = mapped_column(PRICE, nullable=False)
    direction: Mapped[str | None] = mapped_column(String(8), nullable=True)
    reclaimed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    linked_break_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    engine_version: Mapped[str] = mapped_column(String(48), nullable=False)

    stock: Mapped[Stock] = relationship()


class FvgZone(Base):
    """A fair value gap and how much of it has been filled.

    ``mitigation_pct`` is a RUNNING MAXIMUM, never a current reading: a gap
    that filled 80% and then price retreated is 80% mitigated forever. Storing
    the current overlap instead would let a zone "un-fill" itself.
    """

    __tablename__ = "fvg_zones"
    __table_args__ = (
        UniqueConstraint(
            "stock_id", "timeframe", "created_at_bar", "direction",
            name="uq_fvg_zones_stock_tf_bar_dir",
        ),
        Index("ix_fvg_zones_stock_id_timeframe_status", "stock_id", "timeframe", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, default="D1")
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at_bar: Mapped[date] = mapped_column(Date, nullable=False)
    upper_bound: Mapped[float] = mapped_column(PRICE, nullable=False)
    lower_bound: Mapped[float] = mapped_column(PRICE, nullable=False)
    size_pct: Mapped[float | None] = mapped_column(PCT, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    mitigation_pct: Mapped[float | None] = mapped_column(PCT, nullable=True)
    mitigated_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    engine_version: Mapped[str] = mapped_column(String(48), nullable=False)

    stock: Mapped[Stock] = relationship()


# ---------------------------------------------------------------------------
# Market context: RS, sector, regime (ERD section 7)
# ---------------------------------------------------------------------------


class RelativeStrength(Base):
    """Per-stock RS for one date.

    ``rs_score`` is a percentile within the ranked universe, so a row is only
    meaningful alongside the ``universe_size`` it was ranked against -- a
    95th-percentile score out of 40 names is not the same claim as out of 500.
    """

    __tablename__ = "relative_strength"
    __table_args__ = (
        Index("ix_relative_strength_date_rs_score", "date", "rs_score"),
    )

    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)

    ret_1m: Mapped[float | None] = mapped_column(PCT, nullable=True)
    ret_3m: Mapped[float | None] = mapped_column(PCT, nullable=True)
    ret_6m: Mapped[float | None] = mapped_column(PCT, nullable=True)
    ret_12m: Mapped[float | None] = mapped_column(PCT, nullable=True)

    rs_raw: Mapped[float | None] = mapped_column(RATIO, nullable=True)
    rs_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    rs_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rs_vs_sector: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    rs_trend: Mapped[str | None] = mapped_column(String(16), nullable=True)

    #: Which return horizons actually had history, comma-separated. A
    #: two-month-old listing ranked on ret_1m alone must be identifiable as
    #: such rather than looking like a full-coverage rank.
    coverage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    universe_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    benchmark_index_id: Mapped[int | None] = mapped_column(
        ForeignKey("indices.id"), nullable=True
    )
    engine_version: Mapped[str] = mapped_column(String(48), nullable=False)

    stock: Mapped[Stock] = relationship()


class SectorMetric(Base):
    """Aggregated sector standing for one date."""

    __tablename__ = "sector_metrics"
    __table_args__ = (
        Index("ix_sector_metrics_date_rs_rank", "date", "rs_rank"),
    )

    sector_id: Mapped[int] = mapped_column(
        ForeignKey("sectors.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)

    ret_1m: Mapped[float | None] = mapped_column(PCT, nullable=True)
    ret_3m: Mapped[float | None] = mapped_column(PCT, nullable=True)
    ret_6m: Mapped[float | None] = mapped_column(PCT, nullable=True)

    #: Nullable because an unranked sector has no standing, which is a
    #: different statement from standing last.
    rs_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    rs_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    momentum_score: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    quadrant: Mapped[str | None] = mapped_column(String(16), nullable=True)

    pct_above_ema_20: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    pct_above_ema_50: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    pct_above_ema_200: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)

    new_highs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_lows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    breakouts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_breakouts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_pattern_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)

    constituent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Too few constituents for the rank to carry meaning. Stored rather than
    #: recomputed so the UI never has to re-derive the threshold.
    low_sample: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    engine_version: Mapped[str] = mapped_column(String(48), nullable=False)

    sector: Mapped[Sector] = relationship()


class MarketMetric(Base):
    """Breadth for one date, stored so a breadth history can be charted
    without replaying every scan."""

    __tablename__ = "market_metrics"

    date: Mapped[date] = mapped_column(Date, primary_key=True)

    advancers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    decliners: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ad_ratio: Mapped[float | None] = mapped_column(RATIO, nullable=True)

    pct_above_ema_20: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    pct_above_ema_50: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    pct_above_ema_100: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    pct_above_ema_200: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)

    new_highs_52w: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_lows_52w: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    breakouts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_breakouts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    breakout_success_ratio: Mapped[float | None] = mapped_column(RATIO, nullable=True)

    index_close: Mapped[float | None] = mapped_column(PRICE, nullable=True)
    index_ret_1m: Mapped[float | None] = mapped_column(PCT, nullable=True)
    index_ret_3m: Mapped[float | None] = mapped_column(PCT, nullable=True)
    volatility_20d: Mapped[float | None] = mapped_column(PCT, nullable=True)

    universe_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Symbols in the universe with no bar for this date. Kept because a
    #: breadth reading from 300 of 500 names is a different number from one
    #: taken across the whole universe, and the row should say which it is.
    excluded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    engine_version: Mapped[str] = mapped_column(String(48), nullable=False)


class MarketRegimeRow(Base):
    """One day's regime classification and its component scores.

    ``pending_regime`` is persisted, not derived on read: it is the record
    that a threshold was crossed and hysteresis withheld the change. Without
    it, a later reader cannot distinguish "stable" from "about to turn".
    """

    __tablename__ = "market_regimes"

    date: Mapped[date] = mapped_column(Date, primary_key=True)

    regime: Mapped[str] = mapped_column(String(16), nullable=False)
    regime_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)

    trend_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    breadth_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    momentum_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    participation_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    volatility_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)

    confidence: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    pending_regime: Mapped[str | None] = mapped_column(String(16), nullable=True)

    engine_version: Mapped[str] = mapped_column(String(48), nullable=False)


# ---------------------------------------------------------------------------
# Users and product (Phase 10)
# ---------------------------------------------------------------------------


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False, default="FREE")
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: scrypt digest as ``scrypt$n$r$p$salt$hash``, never a plaintext or a
    #: bare unsalted digest. Nullable because the dev identity and any
    #: externally-authenticated user have no local password -- and a NULL
    #: here must never be treated as "any password matches".
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Watchlist(Base, TimestampMixin):
    __tablename__ = "watchlists"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_watchlists_user_id_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)

    items: Mapped[list[WatchlistItem]] = relationship(
        back_populates="watchlist", cascade="all, delete-orphan"
    )


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (
        UniqueConstraint("watchlist_id", "stock_id", name="uq_watchlist_items_watchlist_id_stock_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watchlist_id: Mapped[int] = mapped_column(
        ForeignKey("watchlists.id", ondelete="CASCADE"), nullable=False
    )
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    watchlist: Mapped[Watchlist] = relationship(back_populates="items")
    stock: Mapped[Stock] = relationship()


class SavedScreen(Base, TimestampMixin):
    __tablename__ = "saved_screens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    conditions: Mapped[str] = mapped_column(Text, nullable=False)  # JSON condition tree


class Alert(Base, TimestampMixin):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stock_id: Mapped[int | None] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    conditions: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="IN_APP")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    triggers: Mapped[list[AlertTrigger]] = relationship(
        back_populates="alert", cascade="all, delete-orphan"
    )


class AlertTrigger(Base):
    __tablename__ = "alert_triggers"
    __table_args__ = (Index("ix_alert_triggers_triggered_at", "triggered_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[int] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    trigger_date: Mapped[date] = mapped_column(Date, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    alert: Mapped[Alert] = relationship(back_populates="triggers")


# ---------------------------------------------------------------------------
# Backtesting (Phase 8)
# ---------------------------------------------------------------------------


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    initial_capital: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    config: Mapped[str | None] = mapped_column(Text, nullable=True)
    engine_version: Mapped[str] = mapped_column(String(48), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(48), nullable=False)
    #: Not optional. A backtest that cannot name how its universe was resolved
    #: is not reproducible, and may be silently survivorship-biased.
    survivorship_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    survivorship_warning: Mapped[str | None] = mapped_column(Text, nullable=True)
    universe_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("universe_snapshots.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="QUEUED")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Everything compute_metrics() produced, as JSON. Recomputing from the
    #: trade ledger can only give a realised curve -- equity stepping on
    #: exits, open losing positions never marked -- which understates
    #: drawdown against any daily-marked benchmark.
    metrics: Mapped[str | None] = mapped_column(Text, nullable=True)

    equity: Mapped[list[BacktestEquityPoint]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    trades: Mapped[list[BacktestTrade]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class BacktestEquityPoint(Base):
    """One daily mark of a backtest's equity, including open positions."""

    __tablename__ = "backtest_equity"
    __table_args__ = (
        UniqueConstraint("run_id", "date", name="uq_backtest_equity_run_date"),
        Index("ix_backtest_equity_run", "run_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    equity: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)

    run: Mapped[BacktestRun] = relationship(back_populates="equity")


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"
    __table_args__ = (Index("ix_backtest_trades_run_id_entry_date", "run_id", "entry_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False
    )
    stock_id: Mapped[int | None] = mapped_column(
        ForeignKey("stocks.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    entry_price: Mapped[float] = mapped_column(PRICE, nullable=False)
    exit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(PRICE, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    gross_pnl: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    costs: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    net_pnl: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    return_pct: Mapped[float | None] = mapped_column(PCT, nullable=True)
    mfe_pct: Mapped[float | None] = mapped_column(PCT, nullable=True)
    mae_pct: Mapped[float | None] = mapped_column(PCT, nullable=True)
    days_held: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entry_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[BacktestRun] = relationship(back_populates="trades")


class AiExplanation(Base):
    """Stored explanation plus the exact payload the model was shown.

    An explanation that cannot be audited against its inputs is not evidence
    of anything, so ``input_payload`` is required.
    """

    __tablename__ = "ai_explanations"
    __table_args__ = (Index("ix_ai_explanations_subject", "subject_type", "subject_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    input_payload: Mapped[str] = mapped_column(Text, nullable=False)
    output_text: Mapped[str] = mapped_column(Text, nullable=False)
    engine_version_of_inputs: Mapped[str] = mapped_column(String(48), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
