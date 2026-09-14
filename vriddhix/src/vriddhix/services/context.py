"""Persist market context: RS, sector metrics, breadth, regime.

Phase 3's engines compute these; this writes them down. The API reads the
stored rows and never calls an engine, which is the rule in docs/09 section 1:
an endpoint that computes inline is an endpoint that can time out, disagree
with yesterday's answer, and give two users different numbers for the same
date.

Writes are idempotent per date -- re-running a scan for 2026-09-14 replaces
that date's context rows rather than accumulating duplicates. This is
delete-then-insert for the same reason ingestion uses it: an upsert that
differs by dialect is a portability bug waiting to happen.

**This is not history rewriting.** The ``engine_version`` on every row records
which code produced it. Recomputing the same date under a NEW engine version
is a different operation, and ``allow_version_overwrite=False`` refuses it so
a version bump cannot silently erase what the previous version said.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db.models import (
    MarketIndex,
    MarketMetric,
    MarketRegimeRow,
    RelativeStrength,
    Sector,
    SectorMetric,
    Stock,
)
from ..versioning import REGIME_ENGINE_VERSION, RS_ENGINE_VERSION, SECTOR_ENGINE_VERSION

logger = logging.getLogger(__name__)


class VersionConflict(RuntimeError):
    """Stored rows for this date came from a different engine version."""


@dataclass
class ContextReport:
    rs_rows: int = 0
    sector_rows: int = 0
    market_rows: int = 0
    regime_rows: int = 0
    skipped: list[str] = None

    def __post_init__(self) -> None:
        if self.skipped is None:
            self.skipped = []


def _existing_version(session: Session, model, on: date, column) -> str | None:
    return session.scalar(select(model.engine_version).where(column == on).limit(1))


def _guard_version(
    session: Session, model, on: date, column, version: str, allow: bool
) -> None:
    stored = _existing_version(session, model, on, column)
    if stored is None or stored == version or allow:
        return
    raise VersionConflict(
        f"{model.__tablename__} for {on} was written by {stored}, now writing "
        f"{version}. Pass allow_version_overwrite=True to replace it, or "
        f"recompute into a fresh date range to keep both."
    )


def _f(value) -> float | None:
    """Scalar float or None. NaN is not a number a row should carry."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return float(value)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def persist_relative_strength(
    session: Session,
    results: dict,
    as_of: date,
    *,
    benchmark_index: str | None = None,
    allow_version_overwrite: bool = False,
) -> int:
    """Write one RS row per ranked symbol."""
    if not results:
        return 0

    _guard_version(
        session, RelativeStrength, as_of, RelativeStrength.date,
        RS_ENGINE_VERSION, allow_version_overwrite,
    )

    stock_ids = dict(
        session.execute(
            select(Stock.symbol, Stock.id).where(Stock.symbol.in_(list(results)))
        ).all()
    )
    index_id = None
    if benchmark_index:
        index_id = session.scalar(
            select(MarketIndex.id).where(MarketIndex.code == benchmark_index)
        )

    session.execute(delete(RelativeStrength).where(RelativeStrength.date == as_of))

    universe_size = len(results)
    written = 0
    for symbol, rs in results.items():
        stock_id = stock_ids.get(symbol)
        if stock_id is None:
            continue
        returns = rs.returns or {}
        session.add(
            RelativeStrength(
                stock_id=stock_id,
                date=as_of,
                ret_1m=_f(returns.get("ret_1m")),
                ret_3m=_f(returns.get("ret_3m")),
                ret_6m=_f(returns.get("ret_6m")),
                ret_12m=_f(returns.get("ret_12m")),
                rs_raw=_f(rs.rs_raw),
                rs_score=_f(rs.rs_score),
                rs_rank=rs.rs_rank,
                rs_vs_sector=_f(rs.rs_vs_sector),
                rs_trend=rs.rs_trend.value if rs.rs_trend else None,
                coverage=",".join(rs.coverage) if rs.coverage else None,
                universe_size=universe_size,
                benchmark_index_id=index_id,
                engine_version=rs.engine_version or RS_ENGINE_VERSION,
            )
        )
        written += 1

    session.flush()
    return written


def persist_sector_metrics(
    session: Session,
    results: dict,
    as_of: date,
    *,
    allow_version_overwrite: bool = False,
) -> int:
    """Write one row per sector that was aggregated."""
    if not results:
        return 0

    _guard_version(
        session, SectorMetric, as_of, SectorMetric.date,
        SECTOR_ENGINE_VERSION, allow_version_overwrite,
    )

    sector_ids = dict(
        session.execute(
            select(Sector.code, Sector.id).where(Sector.code.in_(list(results)))
        ).all()
    )
    session.execute(delete(SectorMetric).where(SectorMetric.date == as_of))

    written = 0
    for code, result in results.items():
        sector_id = sector_ids.get(code)
        if sector_id is None:
            # An unclassified bucket is a real scan output but not a sector;
            # it has no row rather than a row under a fabricated id.
            logger.debug("sector %s has no reference row; not persisted", code)
            continue
        returns = result.returns or {}
        session.add(
            SectorMetric(
                sector_id=sector_id,
                date=as_of,
                ret_1m=_f(returns.get("ret_1m")),
                ret_3m=_f(returns.get("ret_3m")),
                ret_6m=_f(returns.get("ret_6m")),
                rs_score=_f(result.rs_score),
                rs_rank=result.rs_rank,
                momentum_score=_f(result.momentum),
                quadrant=result.quadrant.value if result.quadrant else None,
                pct_above_ema_20=_f(result.pct_above_ema_20),
                pct_above_ema_50=_f(result.pct_above_ema_50),
                pct_above_ema_200=_f(result.pct_above_ema_200),
                new_highs=result.new_highs,
                new_lows=result.new_lows,
                breakouts=result.breakouts,
                failed_breakouts=result.failed_breakouts,
                avg_pattern_score=_f(result.avg_pattern_score),
                constituent_count=result.constituent_count,
                low_sample=result.low_sample,
                engine_version=result.engine_version or SECTOR_ENGINE_VERSION,
            )
        )
        written += 1

    session.flush()
    return written


def persist_market(
    session: Session,
    regime_result,
    as_of: date,
    *,
    benchmark: pd.Series | None = None,
    breakouts: int = 0,
    failed_breakouts: int = 0,
    volatility_20d: float | None = None,
    allow_version_overwrite: bool = False,
) -> tuple[int, int]:
    """Write the day's breadth row and regime row.

    Returns ``(market_rows, regime_rows)``. A refused regime -- coverage below
    the floor -- still writes breadth if a snapshot was produced: the breadth
    numbers are real even when the classification built on them is withheld.
    """
    if regime_result is None:
        return 0, 0

    _guard_version(
        session, MarketRegimeRow, as_of, MarketRegimeRow.date,
        REGIME_ENGINE_VERSION, allow_version_overwrite,
    )

    breadth = regime_result.breadth
    components = {c.name: c.raw for c in regime_result.components}

    session.execute(delete(MarketMetric).where(MarketMetric.date == as_of))
    session.execute(delete(MarketRegimeRow).where(MarketRegimeRow.date == as_of))

    total_resolved = breakouts + failed_breakouts
    session.add(
        MarketMetric(
            date=as_of,
            advancers=breadth.advancers,
            decliners=breadth.decliners,
            unchanged=breadth.unchanged,
            ad_ratio=_f(breadth.ad_ratio),
            pct_above_ema_20=_f(breadth.pct_above_ema_20),
            pct_above_ema_50=_f(breadth.pct_above_ema_50),
            pct_above_ema_100=_f(breadth.pct_above_ema_100),
            pct_above_ema_200=_f(breadth.pct_above_ema_200),
            new_highs_52w=breadth.new_highs_52w,
            new_lows_52w=breadth.new_lows_52w,
            breakouts=breakouts,
            failed_breakouts=failed_breakouts,
            # None, not 0.0, when nothing resolved: a success ratio over zero
            # breakouts is undefined, and 0.0 would read as "all failed".
            breakout_success_ratio=(
                breakouts / total_resolved if total_resolved else None
            ),
            index_close=_f(benchmark.get("close")) if benchmark is not None else None,
            index_ret_1m=_f(benchmark.get("ret_1m")) if benchmark is not None else None,
            index_ret_3m=_f(benchmark.get("ret_3m")) if benchmark is not None else None,
            volatility_20d=_f(volatility_20d),
            universe_size=breadth.universe_size,
            excluded=breadth.excluded,
            engine_version=REGIME_ENGINE_VERSION,
        )
    )
    session.add(
        MarketRegimeRow(
            date=as_of,
            regime=regime_result.regime.value,
            regime_score=_f(regime_result.regime_score),
            trend_score=_f(components.get("trend")),
            breadth_score=_f(components.get("breadth")),
            momentum_score=_f(components.get("momentum")),
            participation_score=_f(components.get("participation")),
            volatility_score=_f(components.get("volatility")),
            confidence=_f(regime_result.confidence),
            pending_regime=(
                regime_result.pending_regime.value
                if regime_result.pending_regime else None
            ),
            engine_version=regime_result.engine_version or REGIME_ENGINE_VERSION,
        )
    )
    session.flush()
    return 1, 1


def realised_volatility(closes: pd.Series, window: int = 20) -> float | None:
    """Annualised 20-day realised volatility of the benchmark.

    Uses only the trailing window, like every other feature: a full-series
    standard deviation would make today's number depend on tomorrow's bars.
    """
    if closes is None or len(closes) < window + 1:
        return None
    returns = np.log(closes.astype(float)).diff().dropna().tail(window)
    if len(returns) < window:
        return None
    return float(returns.std(ddof=1) * np.sqrt(252))


def persist_scan_context(
    session: Session,
    result,
    *,
    benchmark_index: str | None = None,
    breakouts: int = 0,
    failed_breakouts: int = 0,
    volatility_20d: float | None = None,
    allow_version_overwrite: bool = False,
) -> ContextReport:
    """Persist every context row a ``ScanResult`` produced."""
    report = ContextReport()
    as_of = result.as_of

    report.rs_rows = persist_relative_strength(
        session, getattr(result, "rs", {}) or {}, as_of,
        benchmark_index=benchmark_index,
        allow_version_overwrite=allow_version_overwrite,
    )
    report.sector_rows = persist_sector_metrics(
        session, result.sectors or {}, as_of,
        allow_version_overwrite=allow_version_overwrite,
    )
    if result.regime is None:
        report.skipped.append(
            "regime not computed (coverage below floor or no universe)"
        )
    else:
        market_rows, regime_rows = persist_market(
            session, result.regime, as_of,
            benchmark=getattr(result, "benchmark", None),
            breakouts=breakouts,
            failed_breakouts=failed_breakouts,
            volatility_20d=volatility_20d,
            allow_version_overwrite=allow_version_overwrite,
        )
        report.market_rows = market_rows
        report.regime_rows = regime_rows

    return report
