"""Scanner results. docs/09 section 5.

These endpoints read the patterns the daily scan stored. They do not run the
scan. If the requested date has no completed scan, the answer is
``SCAN_NOT_RUN`` (503) -- not an empty list, which a UI cannot distinguish
from "nothing set up today".
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from ...db.models import (
    Breakout,
    FvgZone,
    Industry,
    MarketRegimeRow,
    OhlcvDaily,
    OrderBlockRow,
    Pattern,
    RelativeStrength,
    Sector,
    SectorMetric,
    Stock,
    StructureBreakRow,
    TechnicalFeature,
)
from ...domain.types import PatternStatus
from ...services.scanner import grade_for
from ...versioning import (
    FVG_ENGINE_VERSION,
    SCORING_RULE_VERSION,
    SMC_ENGINE_VERSION,
    VCP_ENGINE_VERSION,
)
from .. import errors
from ..deps import ConfigDep, PageDep, ProvenanceDep, SessionDep, parse_date, window
from ..serialise import breakout_payload, iso, loads, num

router = APIRouter(prefix="/api/v1/scanners", tags=["scanners"])

#: Sortable keys use the SAME names as ``SetupRow`` fields. The scanner
#: payload, the condition-tree vocabulary and the backtester's row shape are
#: one vocabulary: a screen saved against `vcp_score` has to mean the same
#: thing when it is applied to a scanner row.
SORTABLE = {"vcp_score", "rs_score", "distance_from_pivot_pct", "symbol"}


@router.get("/vcp")
def vcp_scanner(
    session: SessionDep,
    prov: ProvenanceDep,
    cfg: ConfigDep,
    page: PageDep,
    as_of: Annotated[str | None, Query()] = None,
    min_score: Annotated[float | None, Query(ge=0, le=100)] = None,
    stage: Annotated[str | None, Query()] = None,
    sector: Annotated[str | None, Query()] = None,
    min_rs: Annotated[float | None, Query(ge=0, le=100)] = None,
    max_pivot_distance_pct: Annotated[float | None, Query()] = None,
    sort: Annotated[str, Query()] = "vcp_score",
) -> dict:
    if sort not in SORTABLE:
        raise errors.invalid_params(f"Cannot sort by '{sort}'.", allowed=sorted(SORTABLE))

    target = parse_date(as_of, "as_of") or prov.as_of
    if target is None:
        raise errors.scan_not_run()

    stmt = (
        select(
            Pattern, Stock.symbol, Sector.code,
            OhlcvDaily.close, TechnicalFeature.rel_volume,
            RelativeStrength.rs_score, RelativeStrength.rs_trend,
            SectorMetric.rs_rank, SectorMetric.quadrant,
        )
        .join(Stock, Stock.id == Pattern.stock_id)
        .outerjoin(Industry, Industry.id == Stock.industry_id)
        .outerjoin(Sector, Sector.id == Industry.sector_id)
        .outerjoin(
            OhlcvDaily,
            (OhlcvDaily.stock_id == Stock.id) & (OhlcvDaily.date == target),
        )
        .outerjoin(
            TechnicalFeature,
            (TechnicalFeature.stock_id == Stock.id) & (TechnicalFeature.date == target),
        )
        .outerjoin(
            RelativeStrength,
            (RelativeStrength.stock_id == Stock.id) & (RelativeStrength.date == target),
        )
        .outerjoin(
            SectorMetric,
            (SectorMetric.sector_id == Sector.id) & (SectorMetric.date == target),
        )
        .where(Pattern.base_end == target)
    )

    if min_score is not None:
        stmt = stmt.where(Pattern.score >= min_score)
    if stage:
        wanted = [s.strip().upper() for s in stage.split(",") if s.strip()]
        valid = {s.value for s in PatternStatus}
        unknown = set(wanted) - valid
        if unknown:
            raise errors.invalid_params(
                f"Unknown stage: {', '.join(sorted(unknown))}", allowed=sorted(valid)
            )
        stmt = stmt.where(Pattern.status.in_(wanted))
    if sector:
        stmt = stmt.where(Sector.code == sector.upper())
    if min_rs is not None:
        stmt = stmt.where(RelativeStrength.rs_score >= min_rs)

    rows = session.execute(stmt).all()
    if not rows and not session.scalar(
        select(Pattern.id).where(Pattern.base_end == target).limit(1)
    ):
        # Distinguish "the scan never ran" from "the scan ran and nothing
        # matched the filters". Only the first is an operational problem.
        if session.get(MarketRegimeRow, target) is None:
            raise errors.scan_not_run(target)

    grades = cfg.get("scoring.grades", {})
    results = []
    for pattern, symbol, sector_code, close, rel_volume, rs, rs_trend, sec_rank, quadrant in rows:
        close_f = num(close)
        pivot = num(pattern.pivot_price)
        distance = None
        if close_f and pivot:
            distance = (pivot - close_f) / close_f * 100.0
        if max_pivot_distance_pct is not None and (
            distance is None or abs(distance) > max_pivot_distance_pct
        ):
            continue

        score = num(pattern.score)
        results.append(
            {
                "symbol": symbol,
                "close": close_f,
                "pattern_id": pattern.id,
                "pattern_type": pattern.pattern_type,
                "vcp_stage": pattern.status,
                "vcp_score": score,
                # Non-negotiable: the "why this setup?" panel projects these
                # stored components rather than recomputing the score.
                "score_components": loads(pattern.score_breakdown, []),
                "grade": grade_for(score, grades).value if score is not None else None,
                "rs_score": num(rs),
                "rs_trend": rs_trend,
                "sector": sector_code,
                "sector_rank": sec_rank,
                "sector_quadrant": quadrant,
                "pivot_price": pivot,
                "distance_from_pivot_pct": distance,
                "base_start": iso(pattern.base_start),
                "base_end": iso(pattern.base_end),
                "base_depth_pct": num(pattern.base_depth_pct),
                "contractions": pattern.contraction_count,
                "rel_volume": num(rel_volume),
                "engine_version": pattern.engine_version,
                "rule_version": pattern.rule_version,
            }
        )

    regime = session.get(MarketRegimeRow, target)
    for row in results:
        row["market_regime"] = regime.regime if regime else None

    present = [r for r in results if r.get(sort) is not None]
    absent = [r for r in results if r.get(sort) is None]
    present.sort(key=lambda r: r[sort], reverse=(sort != "symbol"))
    ordered = present + absent

    start = 0
    if page.cursor and "offset" in page.cursor:
        start = int(page.cursor["offset"])
    window_rows = ordered[start : start + page.limit]
    has_more = len(ordered) > start + page.limit

    return {
        "total": len(ordered),
        "results": window_rows,
        "next_cursor": page.encode(offset=start + page.limit) if has_more else None,
        **prov.envelope(
            engine_version=VCP_ENGINE_VERSION,
            rule_version=SCORING_RULE_VERSION,
            as_of=target.isoformat(),
        ),
    }


@router.get("/breakouts")
def breakouts_today(
    session: SessionDep,
    prov: ProvenanceDep,
    as_of: Annotated[str | None, Query()] = None,
    confirmed: Annotated[bool | None, Query()] = None,
) -> dict:
    target = parse_date(as_of, "as_of") or prov.as_of
    if target is None:
        raise errors.scan_not_run()

    stmt = (
        select(Breakout, Stock.symbol)
        .join(Stock, Stock.id == Breakout.stock_id)
        .where(Breakout.breakout_date == target)
    )
    if confirmed is True:
        stmt = stmt.where(Breakout.status == "CONFIRMED")
    elif confirmed is False:
        stmt = stmt.where(Breakout.status != "CONFIRMED")

    rows = session.execute(stmt.order_by(Breakout.id)).all()
    return {
        "breakouts": [breakout_payload(row, symbol) for row, symbol in rows],
        **prov.envelope(engine_version=VCP_ENGINE_VERSION, as_of=target.isoformat()),
    }


@router.get("/failed-breakouts")
def failed_breakouts(
    session: SessionDep,
    prov: ProvenanceDep,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
) -> dict:
    """Failures as a first-class result set.

    This endpoint exists because a platform that only surfaces what worked
    teaches its users nothing about what does not.
    """
    start, end = window(from_, to)
    rows = session.execute(
        select(Breakout, Stock.symbol)
        .join(Stock, Stock.id == Breakout.stock_id)
        .where(
            Breakout.status == "FAILED",
            Breakout.breakout_date >= start, Breakout.breakout_date <= end,
        )
        .order_by(Breakout.breakout_date.desc())
    ).all()
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "breakouts": [breakout_payload(row, symbol) for row, symbol in rows],
        **prov.envelope(engine_version=VCP_ENGINE_VERSION),
    }


@router.get("/smc")
def smc_scanner(
    session: SessionDep,
    prov: ProvenanceDep,
    page: PageDep,
    direction: Annotated[str | None, Query(pattern="^(BULLISH|BEARISH)$")] = None,
    timeframe: Annotated[str, Query()] = "D1",
    as_of: Annotated[str | None, Query()] = None,
    lookback_days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict:
    """Recent structure breaks, with the open order blocks around them."""
    target = parse_date(as_of, "as_of") or prov.as_of
    if target is None:
        raise errors.scan_not_run()
    since = target - timedelta(days=lookback_days)

    stmt = (
        select(StructureBreakRow, Stock.symbol)
        .join(Stock, Stock.id == StructureBreakRow.stock_id)
        .where(
            StructureBreakRow.timeframe == timeframe,
            StructureBreakRow.date >= since,
            StructureBreakRow.date <= target,
        )
    )
    if direction:
        stmt = stmt.where(StructureBreakRow.direction == direction)

    rows = session.execute(
        stmt.order_by(StructureBreakRow.date.desc()).limit(page.limit)
    ).all()

    blocks: dict[int, list] = {}
    if rows:
        stock_ids = {row.stock_id for row, _ in rows}
        for block in session.scalars(
            select(OrderBlockRow).where(
                OrderBlockRow.stock_id.in_(stock_ids),
                OrderBlockRow.timeframe == timeframe,
                OrderBlockRow.status == "OPEN",
            )
        ).all():
            blocks.setdefault(block.stock_id, []).append(
                {
                    "date": iso(block.date), "direction": block.direction,
                    "upper": num(block.upper), "lower": num(block.lower),
                    "status": block.status,
                }
            )

    return {
        "timeframe": timeframe,
        "results": [
            {
                "symbol": symbol,
                "date": iso(row.date),
                "kind": row.kind,
                "direction": row.direction,
                "broken_level": num(row.broken_level),
                "broken_swing_date": iso(row.broken_swing_date),
                "close_price": num(row.close_price),
                "prior_structure": row.prior_structure,
                "open_order_blocks": blocks.get(row.stock_id, []),
                "engine_version": row.engine_version,
            }
            for row, symbol in rows
        ],
        **prov.envelope(engine_version=SMC_ENGINE_VERSION, as_of=target.isoformat()),
    }


@router.get("/fvg")
def fvg_scanner(
    session: SessionDep,
    prov: ProvenanceDep,
    page: PageDep,
    status: Annotated[str, Query()] = "OPEN",
    timeframe: Annotated[str, Query()] = "D1",
    direction: Annotated[str | None, Query(pattern="^(BULLISH|BEARISH)$")] = None,
    min_size_pct: Annotated[float | None, Query(ge=0)] = None,
) -> dict:
    """Fair value gaps by state.

    ``status=OPEN`` includes PARTIALLY_FILLED: a gap that is 30% filled is
    still an unfilled zone, and excluding it would hide most of them.
    """
    wanted = [s.strip().upper() for s in status.split(",") if s.strip()]
    valid = {"OPEN", "PARTIALLY_FILLED", "MITIGATED", "INVALIDATED"}
    unknown = set(wanted) - valid
    if unknown:
        raise errors.invalid_params(
            f"Unknown FVG status: {', '.join(sorted(unknown))}", allowed=sorted(valid)
        )
    if wanted == ["OPEN"]:
        wanted = ["OPEN", "PARTIALLY_FILLED"]

    stmt = (
        select(FvgZone, Stock.symbol)
        .join(Stock, Stock.id == FvgZone.stock_id)
        .where(FvgZone.timeframe == timeframe, FvgZone.status.in_(wanted))
    )
    if direction:
        stmt = stmt.where(FvgZone.direction == direction)
    if min_size_pct is not None:
        stmt = stmt.where(FvgZone.size_pct >= min_size_pct)

    rows = session.execute(
        stmt.order_by(FvgZone.created_at_bar.desc()).limit(page.limit)
    ).all()
    return {
        "timeframe": timeframe,
        "status": wanted,
        "results": [
            {
                "symbol": symbol,
                "direction": row.direction,
                "created_at_bar": iso(row.created_at_bar),
                "upper_bound": num(row.upper_bound),
                "lower_bound": num(row.lower_bound),
                "size_pct": num(row.size_pct),
                "status": row.status,
                # A running maximum: a gap that filled 80% and then saw price
                # retreat is 80% mitigated, permanently.
                "mitigation_pct": num(row.mitigation_pct),
                "mitigated_at": iso(row.mitigated_at),
                "engine_version": row.engine_version,
            }
            for row, symbol in rows
        ],
        **prov.envelope(engine_version=FVG_ENGINE_VERSION),
    }
