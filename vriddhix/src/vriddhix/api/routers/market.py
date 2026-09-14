"""Market regime and breadth. docs/09 section 2."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from ...db.models import MarketIndex, MarketMetric, MarketRegimeRow
from ...versioning import REGIME_ENGINE_VERSION
from .. import errors
from ..deps import ProvenanceDep, SessionDep, parse_date, window
from ..serialise import num, regime_payload

router = APIRouter(prefix="/api/v1/market", tags=["market"])


def _breadth_payload(row: MarketMetric | None) -> dict | None:
    if row is None:
        return None
    return {
        "advancers": row.advancers,
        "decliners": row.decliners,
        "unchanged": row.unchanged,
        "ad_ratio": num(row.ad_ratio),
        "pct_above_ema_20": num(row.pct_above_ema_20),
        "pct_above_ema_50": num(row.pct_above_ema_50),
        "pct_above_ema_100": num(row.pct_above_ema_100),
        "pct_above_ema_200": num(row.pct_above_ema_200),
        "new_highs_52w": row.new_highs_52w,
        "new_lows_52w": row.new_lows_52w,
        "breakouts": row.breakouts,
        "failed_breakouts": row.failed_breakouts,
        "breakout_success_ratio": num(row.breakout_success_ratio),
        "index_close": num(row.index_close),
        "index_ret_1m": num(row.index_ret_1m),
        "index_ret_3m": num(row.index_ret_3m),
        "volatility_20d": num(row.volatility_20d),
        "universe_size": row.universe_size,
        # Surfaced, not hidden: breadth across 300 of 500 names is a
        # different reading from breadth across the whole universe.
        "excluded": row.excluded,
    }


@router.get("")
def market_snapshot(
    session: SessionDep,
    prov: ProvenanceDep,
    as_of: Annotated[str | None, Query()] = None,
) -> dict:
    """Regime plus breadth for one date (default: the last completed scan)."""
    requested = parse_date(as_of, "as_of")
    target = requested or prov.as_of
    if target is None:
        raise errors.scan_not_run()

    regime = session.get(MarketRegimeRow, target)
    breadth = session.get(MarketMetric, target)
    if regime is None and breadth is None:
        raise errors.scan_not_run(target)

    return {
        "regime": regime_payload(regime),
        "breadth": _breadth_payload(breadth),
        **prov.envelope(
            engine_version=REGIME_ENGINE_VERSION, as_of=target.isoformat()
        ),
    }


@router.get("/regime")
def regime_series(
    session: SessionDep,
    prov: ProvenanceDep,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
) -> dict:
    """Historical regime series, oldest first."""
    start, end = window(from_, to)
    rows = session.scalars(
        select(MarketRegimeRow)
        .where(MarketRegimeRow.date >= start, MarketRegimeRow.date <= end)
        .order_by(MarketRegimeRow.date)
    ).all()
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "series": [regime_payload(row) for row in rows],
        **prov.envelope(engine_version=REGIME_ENGINE_VERSION),
    }


#: Columns a client may chart. An allow-list, not a passthrough: accepting an
#: arbitrary column name from a querystring is how a read API grows an
#: injection surface.
BREADTH_METRICS = {
    "advancers", "decliners", "unchanged", "ad_ratio",
    "pct_above_ema_20", "pct_above_ema_50", "pct_above_ema_100",
    "pct_above_ema_200", "new_highs_52w", "new_lows_52w",
    "breakouts", "failed_breakouts", "breakout_success_ratio",
    "index_close", "index_ret_1m", "index_ret_3m", "volatility_20d",
    "universe_size", "excluded",
}


@router.get("/breadth")
def breadth_series(
    session: SessionDep,
    prov: ProvenanceDep,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    metrics: Annotated[str | None, Query()] = None,
) -> dict:
    """Breadth history, optionally narrowed to named metrics."""
    start, end = window(from_, to)

    selected = BREADTH_METRICS
    if metrics:
        requested = {m.strip() for m in metrics.split(",") if m.strip()}
        unknown = requested - BREADTH_METRICS
        if unknown:
            raise errors.invalid_params(
                f"Unknown breadth metric(s): {', '.join(sorted(unknown))}",
                allowed=sorted(BREADTH_METRICS),
            )
        selected = requested

    rows = session.scalars(
        select(MarketMetric)
        .where(MarketMetric.date >= start, MarketMetric.date <= end)
        .order_by(MarketMetric.date)
    ).all()

    series = []
    for row in rows:
        full = _breadth_payload(row)
        series.append({"date": row.date.isoformat(),
                       **{k: v for k, v in full.items() if k in selected}})

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "metrics": sorted(selected),
        "series": series,
        **prov.envelope(engine_version=REGIME_ENGINE_VERSION),
    }


@router.get("/indices", tags=["reference"])
def indices(session: SessionDep) -> dict:
    rows = session.scalars(select(MarketIndex).order_by(MarketIndex.code)).all()
    return {
        "indices": [
            {"id": row.id, "code": row.code, "name": row.name,
             "is_benchmark": row.is_benchmark}
            for row in rows
        ]
    }
