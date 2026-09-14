"""Operations. docs/09 section 10.

Health is reported as a structured status, not as a bare 200. A health check
that returns "ok" while the last scan is four days old is worse than no health
check, because it converts a visible problem into an invisible one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from ...db.models import (
    DataQualityEvent,
    MarketRegimeRow,
    OhlcvDaily,
    Pattern,
    ScanRun,
    Stock,
)
from .. import errors
from ..deps import ConfigDep, ProvenanceDep, SessionDep, window
from ..serialise import iso

router = APIRouter(prefix="/api/v1/ops", tags=["ops"])


@router.get("/health")
def health(session: SessionDep, prov: ProvenanceDep, cfg: ConfigDep) -> dict:
    """Database reachability, data freshness and last-scan age."""
    checks: dict[str, dict] = {}

    try:
        session.execute(select(func.count()).select_from(Stock)).scalar_one()
        checks["database"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        checks["database"] = {"ok": False, "error": str(exc)[:200]}

    last_bar = session.scalar(select(func.max(OhlcvDaily.date)))
    checks["prices"] = {
        "ok": last_bar is not None,
        "last_bar": iso(last_bar),
    }

    checks["scan"] = {
        # Stale is not unhealthy on its own -- a weekend is stale. It is
        # reported so an operator can decide, rather than paged on.
        "ok": prov.as_of is not None,
        "last_scan_date": iso(prov.as_of),
        "age_hours": round(prov.age_hours, 2) if prov.age_hours is not None else None,
        "is_stale": prov.is_stale,
        "stale_after_hours": prov.stale_after_hours,
    }

    errors_recent = session.scalar(
        select(func.count())
        .select_from(DataQualityEvent)
        .where(DataQualityEvent.severity == "ERROR")
    )
    checks["data_quality"] = {"ok": True, "error_events": int(errors_recent or 0)}

    healthy = all(check.get("ok") for check in checks.values())
    return {
        "status": "ok" if healthy else "degraded",
        "checks": checks,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/scans")
def scans(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> dict:
    """Recent scan runs, failures included.

    FAILED rows are listed, never hidden: a missing scan and a failed scan
    look identical if only successes are shown.
    """
    rows = session.scalars(
        select(ScanRun).order_by(ScanRun.id.desc()).limit(limit)
    ).all()
    return {
        "scans": [
            {
                "id": row.id,
                "scan_date": iso(row.scan_date),
                "status": row.status,
                "started_at": iso(row.started_at),
                "finished_at": iso(row.finished_at),
                "symbols_processed": row.symbols_processed,
                "symbols_failed": row.symbols_failed,
                "patterns_detected": row.patterns_detected,
                "breakouts_detected": row.breakouts_detected,
                "engine_version": row.engine_version,
                "notes": row.notes,
            }
            for row in rows
        ]
    }


@router.get("/data-quality")
def data_quality(
    session: SessionDep,
    severity: Annotated[str | None, Query()] = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict:
    start, end = window(from_, to, default_days=30)

    stmt = (
        select(DataQualityEvent, Stock.symbol)
        .outerjoin(Stock, Stock.id == DataQualityEvent.stock_id)
        .where(
            # A finding with no bar date (a whole-symbol problem) is still a
            # finding; excluding it would hide the widest failures.
            (DataQualityEvent.date.is_(None))
            | (
                (DataQualityEvent.date >= start)
                & (DataQualityEvent.date <= end)
            )
        )
    )
    if severity:
        wanted = [s.strip().upper() for s in severity.split(",") if s.strip()]
        valid = {"INFO", "WARN", "ERROR"}
        unknown = set(wanted) - valid
        if unknown:
            raise errors.invalid_params(
                f"Unknown severity: {', '.join(sorted(unknown))}", allowed=sorted(valid)
            )
        stmt = stmt.where(DataQualityEvent.severity.in_(wanted))

    rows = session.execute(
        stmt.order_by(DataQualityEvent.id.desc()).limit(limit)
    ).all()
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "events": [
            {
                "id": event.id,
                "symbol": symbol,
                "bar_date": iso(event.date),
                "category": event.category,
                "severity": event.severity,
                # SUSPECTED findings are recorded, never auto-corrected: a
                # split the platform "fixed" by itself is a price series
                # nobody can reconcile afterwards.
                "detail": event.detail,
                "provider": event.provider,
                "detected_at": iso(event.detected_at),
                "resolved_at": iso(event.resolved_at),
            }
            for event, symbol in rows
        ],
    }


@router.get("/coverage")
def coverage(session: SessionDep, prov: ProvenanceDep) -> dict:
    """What the database actually holds, for the admin panel."""
    counts = {
        "stocks": session.scalar(select(func.count()).select_from(Stock)),
        "bars": session.scalar(select(func.count()).select_from(OhlcvDaily)),
        "patterns": session.scalar(select(func.count()).select_from(Pattern)),
        "regime_days": session.scalar(select(func.count()).select_from(MarketRegimeRow)),
    }
    first_bar = session.scalar(select(func.min(OhlcvDaily.date)))
    last_bar = session.scalar(select(func.max(OhlcvDaily.date)))
    return {
        "counts": {k: int(v or 0) for k, v in counts.items()},
        "price_history": {"from": iso(first_bar), "to": iso(last_bar)},
        **prov.envelope(),
    }
