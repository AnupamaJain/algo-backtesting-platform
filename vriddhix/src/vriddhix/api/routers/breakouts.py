"""The breakout ledger. docs/09 section 6.

Failed breakouts are first-class rows with the same shape as successes, and
no endpoint here filters them out by default. A ledger that quietly hides its
failures reports a hit rate over survivors, which is the single most
flattering lie a research tool can tell.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from ...db.models import Breakout, Industry, Sector, Stock
from ...services.failure import failure_statistics
from ...versioning import VCP_ENGINE_VERSION
from .. import errors
from ..deps import PageDep, ProvenanceDep, SessionDep, window
from ..serialise import breakout_payload

router = APIRouter(prefix="/api/v1/breakouts", tags=["breakouts"])


@router.get("")
def list_breakouts(
    session: SessionDep,
    prov: ProvenanceDep,
    page: PageDep,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    sector: Annotated[str | None, Query()] = None,
    min_score: Annotated[float | None, Query(ge=0, le=100)] = None,
) -> dict:
    start, end = window(from_, to, default_days=180)

    stmt = (
        select(Breakout, Stock.symbol)
        .join(Stock, Stock.id == Breakout.stock_id)
        .where(Breakout.breakout_date >= start, Breakout.breakout_date <= end)
    )
    if status:
        wanted = [s.strip().upper() for s in status.split(",") if s.strip()]
        valid = {"PENDING", "CONFIRMED", "FAILED"}
        unknown = set(wanted) - valid
        if unknown:
            raise errors.invalid_params(
                f"Unknown status: {', '.join(sorted(unknown))}", allowed=sorted(valid)
            )
        stmt = stmt.where(Breakout.status.in_(wanted))
    if sector:
        stmt = (
            stmt.join(Industry, Industry.id == Stock.industry_id)
            .join(Sector, Sector.id == Industry.sector_id)
            .where(Sector.code == sector.upper())
        )
    if min_score is not None:
        stmt = stmt.where(Breakout.setup_score >= min_score)
    if page.cursor and page.cursor.get("id"):
        stmt = stmt.where(Breakout.id < int(page.cursor["id"]))

    rows = session.execute(
        stmt.order_by(Breakout.breakout_date.desc(), Breakout.id.desc())
        .limit(page.limit + 1)
    ).all()
    has_more = len(rows) > page.limit
    rows = rows[: page.limit]

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "breakouts": [breakout_payload(row, symbol) for row, symbol in rows],
        "next_cursor": page.encode(id=rows[-1][0].id) if has_more and rows else None,
        **prov.envelope(engine_version=VCP_ENGINE_VERSION),
    }


@router.get("/statistics")
def statistics(session: SessionDep, prov: ProvenanceDep) -> dict:
    """Ledger aggregates. Failures are the point, not an exclusion."""
    return {
        "statistics": failure_statistics(session),
        **prov.envelope(engine_version=VCP_ENGINE_VERSION),
    }


@router.get("/{breakout_id}")
def breakout_detail(
    breakout_id: int, session: SessionDep, prov: ProvenanceDep
) -> dict:
    row = session.get(Breakout, breakout_id)
    if row is None:
        raise errors.not_found(f"Breakout {breakout_id}")
    symbol = session.scalar(select(Stock.symbol).where(Stock.id == row.stock_id))
    return {
        **breakout_payload(row, symbol),
        **prov.envelope(engine_version=VCP_ENGINE_VERSION),
    }
