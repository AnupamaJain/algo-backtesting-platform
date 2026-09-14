"""Screens and backtests. docs/09 section 7.

Screens are stored as condition trees and evaluated by the SAME evaluator the
scanner and backtester use. A screen therefore cannot mean one thing on the
dashboard and another in a backtest -- there is only one implementation of
what a condition means.

Backtests are asynchronous: POST enqueues and returns 202. A synchronous
backtest over ten years of NIFTY 500 would exceed any sensible request
timeout, and a timed-out backtest that keeps running is worse than one that
was never started.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query
from sqlalchemy import select

from ...db.models import BacktestRun, BacktestTrade, SavedScreen
from ...services.conditions import fields_used, validate
from ...versioning import SCORING_RULE_VERSION, VCP_ENGINE_VERSION
from .. import errors
from ..deps import PageDep, ProvenanceDep, SessionDep, UserDep
from ..serialise import iso, loads, num

router = APIRouter(prefix="/api/v1", tags=["research"])


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


@router.get("/screens")
def list_screens(session: SessionDep, user: UserDep) -> dict:
    """This user's screens. Scoped by token, never by a query parameter."""
    rows = session.scalars(
        select(SavedScreen).where(SavedScreen.user_id == user.id)
        .order_by(SavedScreen.id.desc())
    ).all()
    return {
        "screens": [
            {"id": row.id, "name": row.name, "conditions": loads(row.conditions, {})}
            for row in rows
        ]
    }


@router.post("/screens", status_code=201)
def create_screen(
    session: SessionDep, user: UserDep, payload: Annotated[dict, Body()]
) -> dict:
    name = (payload.get("name") or "").strip()
    conditions = payload.get("conditions")
    if not name:
        raise errors.invalid_params("A screen needs a name.")
    if not isinstance(conditions, dict):
        raise errors.invalid_params("'conditions' must be a condition tree object.")

    try:
        validate(conditions)
    except Exception as exc:  # noqa: BLE001
        # Validated on write, not on first run. A screen that cannot be
        # evaluated should fail where the user is looking at it.
        raise errors.invalid_params(f"Invalid condition tree: {exc}") from exc

    row = SavedScreen(
        user_id=user.id, name=name, conditions=json.dumps(conditions)
    )
    session.add(row)
    session.flush()
    return {
        "id": row.id,
        "name": row.name,
        "conditions": conditions,
        "fields_used": sorted(fields_used(conditions)),
    }


@router.post("/screens/{screen_id}/run")
def run_screen_endpoint(
    screen_id: int,
    session: SessionDep,
    user: UserDep,
    prov: ProvenanceDep,
    page: PageDep,
    as_of: Annotated[str | None, Query()] = None,
) -> dict:
    """Apply a stored screen to the latest stored scan rows.

    This filters what the scan already produced; it does not re-run engines.
    """
    from ..routers.scanners import vcp_scanner
    from ...config import get_config
    from ...services.conditions import evaluate

    row = session.scalar(
        select(SavedScreen).where(
            SavedScreen.id == screen_id, SavedScreen.user_id == user.id
        )
    )
    if row is None:
        raise errors.not_found(f"Screen {screen_id}")

    tree = loads(row.conditions, {})
    scan = vcp_scanner(
        session=session, prov=prov, cfg=get_config(), page=page, as_of=as_of
    )
    matched = [r for r in scan["results"] if evaluate(tree, r).passed]

    return {
        "screen": {"id": row.id, "name": row.name},
        "total": len(matched),
        "results": matched,
        **prov.envelope(
            engine_version=VCP_ENGINE_VERSION, rule_version=SCORING_RULE_VERSION,
            as_of=scan.get("as_of"),
        ),
    }


# ---------------------------------------------------------------------------
# Backtests
# ---------------------------------------------------------------------------


def _run_payload(row: BacktestRun) -> dict:
    return {
        "run_id": row.id,
        "name": row.name,
        "status": row.status,
        "start_date": iso(row.start_date),
        "end_date": iso(row.end_date),
        "initial_capital": num(row.initial_capital),
        "config": loads(row.config, {}),
        "engine_version": row.engine_version,
        "rule_version": row.rule_version,
        # Travels with the numbers, in the payload, not in documentation
        # nobody reads.
        "survivorship_mode": row.survivorship_mode,
        "survivorship_warning": row.survivorship_warning,
        "started_at": iso(row.started_at),
        "finished_at": iso(row.finished_at),
    }


@router.post("/backtests", status_code=202)
def create_backtest(
    session: SessionDep, user: UserDep, payload: Annotated[dict, Body()]
) -> dict:
    """Enqueue a backtest. Returns 202 with a run id to poll."""
    from datetime import date as date_type

    from ...universe.service import UniverseService

    name = (payload.get("name") or "").strip()
    if not name:
        raise errors.invalid_params("A backtest needs a name.")
    try:
        start = date_type.fromisoformat(payload["start_date"])
        end = date_type.fromisoformat(payload["end_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise errors.invalid_params(
            "start_date and end_date are required ISO dates."
        ) from exc
    if start >= end:
        raise errors.invalid_params("start_date must be before end_date.")

    index_code = payload.get("index", "NIFTY500")
    resolution = UniverseService(session).members_as_of(index_code, start)

    row = BacktestRun(
        user_id=user.id,
        name=name,
        start_date=start,
        end_date=end,
        initial_capital=float(payload.get("initial_capital", 1_000_000)),
        config=json.dumps(payload.get("config", {})),
        engine_version=VCP_ENGINE_VERSION,
        rule_version=SCORING_RULE_VERSION,
        survivorship_mode=resolution.mode.value,
        # Recorded at creation, not at read time: the warning describes the
        # universe this run was defined against and must not change later.
        survivorship_warning=resolution.warning,
        status="QUEUED",
    )
    session.add(row)
    session.flush()
    return _run_payload(row)


@router.get("/backtests")
def list_backtests(session: SessionDep, user: UserDep) -> dict:
    rows = session.scalars(
        select(BacktestRun).where(BacktestRun.user_id == user.id)
        .order_by(BacktestRun.id.desc())
    ).all()
    return {"runs": [_run_payload(row) for row in rows]}


def _owned_run(session, user, run_id: int) -> BacktestRun:
    row = session.scalar(
        select(BacktestRun).where(
            BacktestRun.id == run_id, BacktestRun.user_id == user.id
        )
    )
    if row is None:
        raise errors.not_found(f"Backtest {run_id}")
    return row


@router.get("/backtests/compare")
def compare_backtests(
    session: SessionDep,
    user: UserDep,
    run_ids: Annotated[str, Query()],
) -> dict:
    """Compare completed runs side by side.

    Runs with different survivorship modes are compared, but the mode is
    reported per run: a point-in-time run and a current-universe run are not
    measuring the same thing and the payload must not imply they are.
    """
    try:
        ids = [int(part) for part in run_ids.split(",") if part.strip()]
    except ValueError as exc:
        raise errors.invalid_params("run_ids must be comma-separated integers.") from exc
    if not ids:
        raise errors.invalid_params("run_ids is required.")

    runs = [_owned_run(session, user, run_id) for run_id in ids]
    comparisons = []
    for run in runs:
        trades = session.scalars(
            select(BacktestTrade).where(BacktestTrade.run_id == run.id)
        ).all()
        comparisons.append({**_run_payload(run), "metrics": _metrics_from(trades, run)})

    modes = {run.survivorship_mode for run in runs}
    return {
        "runs": comparisons,
        "comparable": len(modes) == 1,
        "note": None if len(modes) == 1 else (
            "Runs use different survivorship modes; their returns are not "
            "directly comparable."
        ),
    }


@router.get("/backtests/{run_id}")
def get_backtest(run_id: int, session: SessionDep, user: UserDep) -> dict:
    return _run_payload(_owned_run(session, user, run_id))


@router.get("/backtests/{run_id}/trades")
def backtest_trades(
    run_id: int, session: SessionDep, user: UserDep, page: PageDep
) -> dict:
    run = _owned_run(session, user, run_id)
    stmt = select(BacktestTrade).where(BacktestTrade.run_id == run.id)
    if page.cursor and page.cursor.get("id"):
        stmt = stmt.where(BacktestTrade.id > int(page.cursor["id"]))

    rows = session.scalars(stmt.order_by(BacktestTrade.id).limit(page.limit + 1)).all()
    has_more = len(rows) > page.limit
    rows = rows[: page.limit]

    return {
        "run_id": run.id,
        "trades": [
            {
                "id": row.id, "symbol": row.symbol,
                "entry_date": iso(row.entry_date), "entry_price": num(row.entry_price),
                "exit_date": iso(row.exit_date), "exit_price": num(row.exit_price),
                "quantity": row.quantity,
                "gross_pnl": num(row.gross_pnl), "costs": num(row.costs),
                "net_pnl": num(row.net_pnl), "return_pct": num(row.return_pct),
                "mfe_pct": num(row.mfe_pct), "mae_pct": num(row.mae_pct),
                "days_held": row.days_held,
                "entry_reason": row.entry_reason, "exit_reason": row.exit_reason,
                "context": loads(row.context, {}),
            }
            for row in rows
        ],
        "next_cursor": page.encode(id=rows[-1].id) if has_more and rows else None,
    }


def _metrics_from(trades, run: BacktestRun) -> dict[str, Any]:
    """Aggregate stored trades.

    Returns None for every ratio when there are no trades. A win rate of 0.0
    over zero trades reads as "this strategy never won", which is a claim the
    data does not support.
    """
    if not trades:
        return {
            "trades": 0, "win_rate": None, "net_pnl": None,
            "avg_return_pct": None, "profit_factor": None,
        }

    net = [float(t.net_pnl) for t in trades if t.net_pnl is not None]
    wins = [v for v in net if v > 0]
    losses = [v for v in net if v <= 0]
    returns = [float(t.return_pct) for t in trades if t.return_pct is not None]
    gross_loss = abs(sum(losses))

    return {
        "trades": len(trades),
        "closed": sum(1 for t in trades if t.exit_date is not None),
        "win_rate": len(wins) / len(net) if net else None,
        "net_pnl": sum(net) if net else None,
        "avg_return_pct": sum(returns) / len(returns) if returns else None,
        # None rather than infinity when nothing lost: a profit factor with no
        # denominator is undefined, and rendering "inf" invites a reader to
        # treat a three-trade sample as a perfect strategy.
        "profit_factor": (sum(wins) / gross_loss) if gross_loss > 0 else None,
        "initial_capital": num(run.initial_capital),
    }


@router.get("/backtests/{run_id}/metrics")
def backtest_metrics(run_id: int, session: SessionDep, user: UserDep) -> dict:
    run = _owned_run(session, user, run_id)
    if run.status in ("QUEUED", "RUNNING"):
        raise errors.backtest_running(run.id, status=run.status)

    trades = session.scalars(
        select(BacktestTrade).where(BacktestTrade.run_id == run.id)
    ).all()
    return {
        "run_id": run.id,
        "status": run.status,
        "metrics": _metrics_from(trades, run),
        # Always present, per docs/09 section 7.
        "survivorship_mode": run.survivorship_mode,
        "survivorship_warning": run.survivorship_warning,
        "engine_version": run.engine_version,
        "rule_version": run.rule_version,
    }
