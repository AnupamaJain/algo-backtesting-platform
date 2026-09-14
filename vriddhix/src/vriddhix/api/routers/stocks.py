"""Stock detail, chart, patterns, X-Ray. docs/09 section 4."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from ...db.models import (
    Industry,
    MarketRegimeRow,
    OhlcvDaily,
    Pattern,
    RelativeStrength,
    Sector,
    SectorMetric,
    Stock,
    TechnicalFeature,
)
from ...domain.types import PatternStatus
from ...versioning import FEATURES_VERSION, VCP_ENGINE_VERSION
from .. import errors
from ..deps import PageDep, ProvenanceDep, SessionDep, parse_date, window
from ..serialise import iso, num, pattern_payload, rs_payload

router = APIRouter(prefix="/api/v1/stocks", tags=["stocks"])

#: Overlays a chart may request. Allow-list, not a column passthrough.
OVERLAYS = {
    "ema": ("ema_20", "ema_50", "ema_100", "ema_200"),
    "atr": ("atr_14", "atr_pct"),
    "volume": ("avg_volume_20", "avg_volume_50", "rel_volume"),
    "returns": ("ret_1m", "ret_3m", "ret_6m", "ret_12m"),
    "extremes": ("high_52w", "low_52w", "pct_from_52w_high", "pct_from_52w_low"),
}


def _stock_or_404(session, symbol: str) -> Stock:
    stock = session.scalar(select(Stock).where(Stock.symbol == symbol.upper()))
    if stock is None:
        raise errors.not_found(f"Symbol '{symbol}'")
    return stock


def _sector_of(session, stock: Stock) -> tuple[str | None, str | None, int | None]:
    if stock.industry_id is None:
        return None, None, None
    row = session.execute(
        select(Sector.code, Sector.name, Sector.id, Industry.name)
        .join(Industry, Industry.sector_id == Sector.id)
        .where(Industry.id == stock.industry_id)
    ).first()
    if row is None:
        return None, None, None
    return row[0], row[3], row[2]


@router.get("")
def list_stocks(
    session: SessionDep,
    page: PageDep,
    index: Annotated[str | None, Query()] = None,
    sector: Annotated[str | None, Query()] = None,
    active_only: Annotated[bool, Query()] = True,
) -> dict:
    """Reference listing. Keyset-paged by symbol."""
    from ...db.models import MarketIndex, UniverseMember

    stmt = select(Stock)
    if active_only:
        stmt = stmt.where(Stock.is_active.is_(True))
    if sector:
        stmt = (
            stmt.join(Industry, Industry.id == Stock.industry_id)
            .join(Sector, Sector.id == Industry.sector_id)
            .where(Sector.code == sector.upper())
        )
    if index:
        stmt = (
            stmt.join(UniverseMember, UniverseMember.stock_id == Stock.id)
            .join(MarketIndex, MarketIndex.id == UniverseMember.index_id)
            .where(MarketIndex.code == index.upper())
        )
    if page.cursor and page.cursor.get("symbol"):
        stmt = stmt.where(Stock.symbol > page.cursor["symbol"])

    rows = session.scalars(
        stmt.order_by(Stock.symbol).limit(page.limit + 1)
    ).unique().all()

    has_more = len(rows) > page.limit
    rows = rows[: page.limit]
    return {
        "stocks": [
            {
                "symbol": row.symbol, "name": row.name, "exchange": row.exchange,
                "is_active": row.is_active,
                # Retained rather than deleted: removing delisted names is
                # exactly how survivorship bias enters a research database.
                "delisted_on": iso(row.delisted_on),
            }
            for row in rows
        ],
        "next_cursor": page.encode(symbol=rows[-1].symbol) if has_more and rows else None,
    }


@router.get("/{symbol}")
def stock_detail(
    symbol: str,
    session: SessionDep,
    prov: ProvenanceDep,
    as_of: Annotated[str | None, Query()] = None,
) -> dict:
    stock = _stock_or_404(session, symbol)
    target = parse_date(as_of, "as_of") or prov.as_of

    bar = session.scalar(
        select(OhlcvDaily)
        .where(
            OhlcvDaily.stock_id == stock.id,
            *( [OhlcvDaily.date <= target] if target else [] ),
        )
        .order_by(OhlcvDaily.date.desc())
        .limit(1)
    )
    if bar is None:
        raise errors.not_found(f"Price history for '{stock.symbol}'")

    previous = session.scalar(
        select(OhlcvDaily.close)
        .where(OhlcvDaily.stock_id == stock.id, OhlcvDaily.date < bar.date)
        .order_by(OhlcvDaily.date.desc())
        .limit(1)
    )
    features = session.get(TechnicalFeature, (stock.id, bar.date))
    rs = session.get(RelativeStrength, (stock.id, bar.date))

    sector_code, industry_name, sector_id = _sector_of(session, stock)
    sector_metric = (
        session.get(SectorMetric, (sector_id, bar.date)) if sector_id else None
    )
    regime = session.get(MarketRegimeRow, bar.date)

    active = session.scalar(
        select(Pattern)
        .where(
            Pattern.stock_id == stock.id,
            Pattern.status.notin_(
                [PatternStatus.FAILED.value, PatternStatus.COMPLETED.value]
            ),
        )
        .order_by(Pattern.detected_on.desc())
        .limit(1)
    )

    change_pct = None
    if previous is not None and float(previous) != 0:
        change_pct = (float(bar.close) - float(previous)) / float(previous) * 100.0

    pattern = pattern_payload(active, stock.symbol)
    if pattern and active and active.pivot_price:
        pattern["distance_from_pivot_pct"] = (
            (float(active.pivot_price) - float(bar.close)) / float(bar.close) * 100.0
        )

    return {
        "stock": {
            "symbol": stock.symbol, "name": stock.name, "exchange": stock.exchange,
            "sector": sector_code, "industry": industry_name,
            "is_active": stock.is_active, "delisted_on": iso(stock.delisted_on),
        },
        "quote": {
            "date": iso(bar.date), "open": num(bar.open), "high": num(bar.high),
            "low": num(bar.low), "close": num(bar.close), "volume": num(bar.volume),
            "change_pct": change_pct,
        },
        "features": None if features is None else {
            "ema_20": num(features.ema_20), "ema_50": num(features.ema_50),
            "ema_100": num(features.ema_100), "ema_200": num(features.ema_200),
            "atr_14": num(features.atr_14), "atr_pct": num(features.atr_pct),
            "rel_volume": num(features.rel_volume),
            "high_52w": num(features.high_52w), "low_52w": num(features.low_52w),
            "pct_from_52w_high": num(features.pct_from_52w_high),
            "pct_from_52w_low": num(features.pct_from_52w_low),
            "feature_version": features.feature_version,
        },
        "rs": rs_payload(rs),
        "active_pattern": pattern,
        "context": {
            "market_regime": regime.regime if regime else None,
            "sector_rank": sector_metric.rs_rank if sector_metric else None,
            "sector_quadrant": sector_metric.quadrant if sector_metric else None,
        },
        **prov.envelope(
            engine_version=VCP_ENGINE_VERSION,
            feature_version=FEATURES_VERSION,
            as_of=iso(bar.date),
        ),
    }


@router.get("/{symbol}/chart")
def chart(
    symbol: str,
    session: SessionDep,
    prov: ProvenanceDep,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    overlays: Annotated[str | None, Query()] = None,
) -> dict:
    """OHLCV plus overlay series and pattern annotation geometry.

    The annotations are the numbers the engine produced, not geometry the
    frontend re-derives: a second implementation in TypeScript is exactly what
    the architecture rule prohibits.
    """
    stock = _stock_or_404(session, symbol)
    start, end = window(from_, to, default_days=365)

    requested = (
        {o.strip() for o in overlays.split(",") if o.strip()} if overlays else {"ema"}
    )
    unknown = requested - set(OVERLAYS)
    if unknown:
        raise errors.invalid_params(
            f"Unknown overlay(s): {', '.join(sorted(unknown))}",
            allowed=sorted(OVERLAYS),
        )
    columns = [c for name in requested for c in OVERLAYS[name]]

    bars = session.scalars(
        select(OhlcvDaily)
        .where(
            OhlcvDaily.stock_id == stock.id,
            OhlcvDaily.date >= start, OhlcvDaily.date <= end,
        )
        .order_by(OhlcvDaily.date)
    ).all()
    features = {
        row.date: row
        for row in session.scalars(
            select(TechnicalFeature).where(
                TechnicalFeature.stock_id == stock.id,
                TechnicalFeature.date >= start, TechnicalFeature.date <= end,
            )
        ).all()
    }

    candles = [
        {
            "date": iso(bar.date), "open": num(bar.open), "high": num(bar.high),
            "low": num(bar.low), "close": num(bar.close), "volume": num(bar.volume),
        }
        for bar in bars
    ]
    overlay_series = {
        column: [
            num(getattr(features[bar.date], column, None)) if bar.date in features else None
            for bar in bars
        ]
        for column in columns
    }

    patterns = session.scalars(
        select(Pattern)
        .where(
            Pattern.stock_id == stock.id,
            Pattern.base_end >= start, Pattern.base_start <= end,
        )
        .order_by(Pattern.detected_on)
    ).all()

    return {
        "symbol": stock.symbol,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "candles": candles,
        "overlays": overlay_series,
        "annotations": {
            "bases": [
                {
                    "pattern_id": p.id, "status": p.status,
                    "start": iso(p.base_start), "end": iso(p.base_end),
                    "depth_pct": num(p.base_depth_pct),
                    "pivot_price": num(p.pivot_price),
                    "contractions": p.contraction_count,
                }
                for p in patterns
            ]
        },
        **prov.envelope(
            engine_version=VCP_ENGINE_VERSION, feature_version=FEATURES_VERSION
        ),
    }


@router.get("/{symbol}/patterns")
def stock_patterns(
    symbol: str,
    session: SessionDep,
    prov: ProvenanceDep,
    status: Annotated[str | None, Query()] = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
) -> dict:
    """Every base this symbol has formed in the window, failures included."""
    stock = _stock_or_404(session, symbol)
    start, end = window(from_, to, default_days=730)

    stmt = select(Pattern).where(
        Pattern.stock_id == stock.id,
        Pattern.detected_on >= start, Pattern.detected_on <= end,
    )
    if status:
        wanted = [s.strip().upper() for s in status.split(",") if s.strip()]
        valid = {s.value for s in PatternStatus}
        unknown = set(wanted) - valid
        if unknown:
            raise errors.invalid_params(
                f"Unknown status: {', '.join(sorted(unknown))}", allowed=sorted(valid)
            )
        stmt = stmt.where(Pattern.status.in_(wanted))

    rows = session.scalars(stmt.order_by(Pattern.detected_on.desc())).all()
    return {
        "symbol": stock.symbol,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "patterns": [pattern_payload(row, stock.symbol) for row in rows],
        **prov.envelope(engine_version=VCP_ENGINE_VERSION),
    }


@router.get("/{symbol}/xray")
def stock_xray(symbol: str, session: SessionDep, prov: ProvenanceDep) -> dict:
    """Stored pattern history with outcomes.

    Reads the ledger rather than replaying the detector: a replay is a
    computation, and docs/09 section 1 keeps computation out of the read path.
    ``services.xray`` performs the replay offline and its results land in
    ``patterns`` / ``breakouts``.
    """
    from ...db.models import Breakout

    stock = _stock_or_404(session, symbol)
    patterns = session.scalars(
        select(Pattern)
        .where(Pattern.stock_id == stock.id)
        .order_by(Pattern.detected_on.desc())
    ).all()
    breakouts = {
        row.pattern_id: row
        for row in session.scalars(
            select(Breakout).where(Breakout.stock_id == stock.id)
        ).all()
    }

    entries = []
    resolved = wins = 0
    for pattern in patterns:
        breakout = breakouts.get(pattern.id)
        outcome = breakout.outcome if breakout else None
        # Resolution is the BREAKOUT'S STATUS, the same definition
        # failure_statistics uses. Reading it from `outcome.exit_date`
        # instead counted every confirmed breakout as unresolved, because a
        # confirmed breakout has not exited -- and the panel then disagreed
        # with the ledger about the same rows.
        if breakout is not None and breakout.status in ("CONFIRMED", "FAILED"):
            resolved += 1
            if (
                outcome is not None
                and outcome.return_pct is not None
                and float(outcome.return_pct) > 0
            ):
                wins += 1
        entries.append(
            {
                **pattern_payload(pattern, stock.symbol),
                "breakout_date": iso(breakout.breakout_date) if breakout else None,
                "breakout_price": num(breakout.breakout_price) if breakout else None,
                "breakout_status": breakout.status if breakout else None,
                "outcome": None if outcome is None else {
                    "failed": bool(outcome.failed),
                    "failure_reason": outcome.failure_reason,
                    "return_pct": num(outcome.return_pct),
                    "mfe_pct": num(outcome.mfe_pct),
                    "mae_pct": num(outcome.mae_pct),
                    "days_held": outcome.days_held,
                },
            }
        )

    return {
        "symbol": stock.symbol,
        "patterns": entries,
        "summary": {
            "total_patterns": len(patterns),
            "resolved": resolved,
            "unresolved": len(patterns) - resolved,
            # None, not 0.0: a win rate over nothing is undefined, and 0.0
            # would read as "this setup has never worked here".
            "win_rate": wins / resolved if resolved else None,
        },
        **prov.envelope(engine_version=VCP_ENGINE_VERSION),
    }


@router.get("/{symbol}/structure")
def stock_structure(
    symbol: str,
    session: SessionDep,
    prov: ProvenanceDep,
    timeframe: Annotated[str, Query()] = "D1",
    as_of: Annotated[str | None, Query()] = None,
) -> dict:
    """Swings, structure breaks, order blocks, liquidity and open FVGs.

    Swings are filtered on ``confirmed_on``, not ``date``: a swing is only
    knowable some bars after it printed, and returning it by its own date
    would hand the caller information it could not have had on that date.
    """
    from ...db.models import (
        FvgZone,
        LiquidityEventRow,
        OrderBlockRow,
        StructureBreakRow,
        SwingPointRow,
    )
    from ...versioning import FVG_ENGINE_VERSION, SMC_ENGINE_VERSION

    stock = _stock_or_404(session, symbol)
    target = parse_date(as_of, "as_of") or prov.as_of

    def limit_to(stmt, column):
        return stmt.where(column <= target) if target else stmt

    swings = session.scalars(
        limit_to(
            select(SwingPointRow).where(
                SwingPointRow.stock_id == stock.id,
                SwingPointRow.timeframe == timeframe,
                SwingPointRow.confirmed_on.isnot(None),
            ),
            SwingPointRow.confirmed_on,
        ).order_by(SwingPointRow.date.desc())
    ).all()

    breaks = session.scalars(
        limit_to(
            select(StructureBreakRow).where(
                StructureBreakRow.stock_id == stock.id,
                StructureBreakRow.timeframe == timeframe,
            ),
            StructureBreakRow.date,
        ).order_by(StructureBreakRow.date.desc())
    ).all()

    blocks = session.scalars(
        limit_to(
            select(OrderBlockRow).where(
                OrderBlockRow.stock_id == stock.id,
                OrderBlockRow.timeframe == timeframe,
            ),
            OrderBlockRow.date,
        ).order_by(OrderBlockRow.date.desc())
    ).all()

    liquidity = session.scalars(
        limit_to(
            select(LiquidityEventRow).where(
                LiquidityEventRow.stock_id == stock.id,
                LiquidityEventRow.timeframe == timeframe,
            ),
            LiquidityEventRow.date,
        ).order_by(LiquidityEventRow.date.desc())
    ).all()

    gaps = session.scalars(
        limit_to(
            select(FvgZone).where(
                FvgZone.stock_id == stock.id,
                FvgZone.timeframe == timeframe,
            ),
            FvgZone.created_at_bar,
        ).order_by(FvgZone.created_at_bar.desc())
    ).all()

    return {
        "symbol": stock.symbol,
        "timeframe": timeframe,
        "swings": [
            {
                "date": iso(s.date), "price": num(s.price), "type": s.swing_type,
                "strength": num(s.strength),
                # The lag itself, so a reader can see how late the
                # confirmation was rather than assuming it was immediate.
                "confirmed_on": iso(s.confirmed_on),
                "left_bars": s.left_bars, "right_bars": s.right_bars,
            }
            for s in swings
        ],
        "breaks": [
            {
                "date": iso(b.date), "kind": b.kind, "direction": b.direction,
                "broken_level": num(b.broken_level),
                "broken_swing_date": iso(b.broken_swing_date),
                "close_price": num(b.close_price),
                "prior_structure": b.prior_structure,
            }
            for b in breaks
        ],
        "order_blocks": [
            {
                "date": iso(o.date), "direction": o.direction,
                "upper": num(o.upper), "lower": num(o.lower),
                "status": o.status, "mitigated_at": iso(o.mitigated_at),
            }
            for o in blocks
        ],
        "liquidity": [
            {
                "date": iso(e.date), "kind": e.kind, "level": num(e.level),
                "direction": e.direction, "reclaimed": e.reclaimed,
            }
            for e in liquidity
        ],
        "fvgs": [
            {
                "created_at_bar": iso(g.created_at_bar), "direction": g.direction,
                "upper_bound": num(g.upper_bound), "lower_bound": num(g.lower_bound),
                "size_pct": num(g.size_pct), "status": g.status,
                "mitigation_pct": num(g.mitigation_pct),
            }
            for g in gaps
        ],
        **prov.envelope(
            engine_version=SMC_ENGINE_VERSION,
            fvg_engine_version=FVG_ENGINE_VERSION,
            as_of=iso(target) if target else None,
        ),
    }
