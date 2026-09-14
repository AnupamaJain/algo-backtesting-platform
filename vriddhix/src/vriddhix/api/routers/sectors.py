"""Sector standing and rotation. docs/09 section 3."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from ...db.models import Industry, RelativeStrength, Sector, SectorMetric, Stock
from ...versioning import SECTOR_ENGINE_VERSION
from .. import errors
from ..deps import ProvenanceDep, SessionDep, parse_date
from ..serialise import num, sector_payload

router = APIRouter(prefix="/api/v1/sectors", tags=["sectors"])

SORTABLE = {"rs_score", "rs_rank", "momentum", "ret_1m", "ret_3m", "ret_6m",
            "constituent_count", "breakouts"}


def _rows_for(session, target):
    return session.execute(
        select(SectorMetric, Sector.code, Sector.name)
        .join(Sector, Sector.id == SectorMetric.sector_id)
        .where(SectorMetric.date == target)
    ).all()


@router.get("")
def list_sectors(
    session: SessionDep,
    prov: ProvenanceDep,
    as_of: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "rs_score",
    order: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
) -> dict:
    if sort not in SORTABLE:
        raise errors.invalid_params(
            f"Cannot sort by '{sort}'.", allowed=sorted(SORTABLE)
        )
    target = parse_date(as_of, "as_of") or prov.as_of
    if target is None:
        raise errors.scan_not_run()

    rows = _rows_for(session, target)
    if not rows:
        raise errors.scan_not_run(target)

    payloads = [sector_payload(metric, code, name) for metric, code, name in rows]

    # Unranked sectors go last in BOTH directions. They are not "worst" --
    # they have no position at all, and letting a null lead a descending sort
    # would put the least-known sectors at the top of the screen.
    ranked = [p for p in payloads if p.get(sort) is not None]
    unranked = [p for p in payloads if p.get(sort) is None]
    ranked.sort(key=lambda item: item[sort], reverse=(order == "desc"))

    return {
        "sectors": ranked + unranked,
        **prov.envelope(
            engine_version=SECTOR_ENGINE_VERSION, as_of=target.isoformat()
        ),
    }


@router.get("/rotation")
def rotation(
    session: SessionDep,
    prov: ProvenanceDep,
    as_of: Annotated[str | None, Query()] = None,
) -> dict:
    """The quadrant matrix: RS on one axis, momentum on the other."""
    target = parse_date(as_of, "as_of") or prov.as_of
    if target is None:
        raise errors.scan_not_run()

    rows = _rows_for(session, target)
    if not rows:
        raise errors.scan_not_run(target)

    quadrants: dict[str, list] = {
        "LEADING": [], "IMPROVING": [], "WEAKENING": [], "LAGGING": [], "UNRANKED": [],
    }
    for metric, code, name in rows:
        # An unranked sector is listed separately rather than dropped: it is
        # still part of the market, it just has no position to plot.
        bucket = metric.quadrant or "UNRANKED"
        quadrants.setdefault(bucket, []).append(
            {
                "code": code,
                "name": name,
                "rs_score": num(metric.rs_score),
                "momentum": num(metric.momentum_score),
                "constituent_count": metric.constituent_count,
                "low_sample": bool(metric.low_sample),
            }
        )

    return {
        "quadrants": quadrants,
        **prov.envelope(
            engine_version=SECTOR_ENGINE_VERSION, as_of=target.isoformat()
        ),
    }


@router.get("/{code}")
def sector_detail(
    code: str,
    session: SessionDep,
    prov: ProvenanceDep,
    as_of: Annotated[str | None, Query()] = None,
) -> dict:
    target = parse_date(as_of, "as_of") or prov.as_of
    if target is None:
        raise errors.scan_not_run()

    sector = session.scalar(select(Sector).where(Sector.code == code.upper()))
    if sector is None:
        raise errors.not_found(f"Sector '{code}'")

    metric = session.get(SectorMetric, (sector.id, target))
    constituents = session.execute(
        select(Stock.symbol, Stock.name, RelativeStrength.rs_score, RelativeStrength.rs_rank)
        .join(Industry, Industry.id == Stock.industry_id)
        .outerjoin(
            RelativeStrength,
            (RelativeStrength.stock_id == Stock.id) & (RelativeStrength.date == target),
        )
        .where(Industry.sector_id == sector.id)
        .order_by(Stock.symbol)
    ).all()

    return {
        "metrics": (
            sector_payload(metric, sector.code, sector.name) if metric else None
        ),
        "constituents": [
            {"symbol": symbol, "name": name,
             "rs_score": num(rs_score), "rs_rank": rs_rank}
            for symbol, name, rs_score, rs_rank in constituents
        ],
        **prov.envelope(
            engine_version=SECTOR_ENGINE_VERSION, as_of=target.isoformat()
        ),
    }
