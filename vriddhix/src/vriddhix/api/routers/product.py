"""Watchlists and alerts. docs/09 section 8.

Everything here is user-owned, and every query filters on the id from the
token. A ``user_id`` accepted from the client would be an authorisation bug
with a convenient interface.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Body, Query
from sqlalchemy import select

from ...db.models import Alert, AlertTrigger, Stock, Watchlist, WatchlistItem
from ...services.alerts import BUILTIN_CONDITIONS, acknowledge
from ...services.conditions import validate
from .. import errors
from ..deps import SessionDep, UserDep, window
from ..serialise import iso, loads

router = APIRouter(prefix="/api/v1", tags=["product"])


# ---------------------------------------------------------------------------
# Watchlists
# ---------------------------------------------------------------------------


def _owned_watchlist(session, user, watchlist_id: int) -> Watchlist:
    row = session.scalar(
        select(Watchlist).where(
            Watchlist.id == watchlist_id, Watchlist.user_id == user.id
        )
    )
    if row is None:
        # 404, not 403: confirming that someone else's watchlist exists is
        # itself a disclosure.
        raise errors.not_found(f"Watchlist {watchlist_id}")
    return row


@router.get("/watchlists")
def list_watchlists(session: SessionDep, user: UserDep) -> dict:
    rows = session.scalars(
        select(Watchlist).where(Watchlist.user_id == user.id).order_by(Watchlist.id)
    ).all()
    return {
        "watchlists": [
            {"id": row.id, "name": row.name, "item_count": len(row.items)}
            for row in rows
        ]
    }


@router.post("/watchlists", status_code=201)
def create_watchlist(
    session: SessionDep, user: UserDep, payload: Annotated[dict, Body()]
) -> dict:
    name = (payload.get("name") or "").strip()
    if not name:
        raise errors.invalid_params("A watchlist needs a name.")
    row = Watchlist(user_id=user.id, name=name)
    session.add(row)
    session.flush()
    return {"id": row.id, "name": row.name, "item_count": 0}


@router.get("/watchlists/{watchlist_id}/items")
def list_items(watchlist_id: int, session: SessionDep, user: UserDep) -> dict:
    watchlist = _owned_watchlist(session, user, watchlist_id)
    rows = session.execute(
        select(WatchlistItem, Stock.symbol, Stock.name)
        .join(Stock, Stock.id == WatchlistItem.stock_id)
        .where(WatchlistItem.watchlist_id == watchlist.id)
        .order_by(Stock.symbol)
    ).all()
    return {
        "watchlist": {"id": watchlist.id, "name": watchlist.name},
        "items": [
            {"id": item.id, "symbol": symbol, "name": name,
             "note": item.note, "added_at": iso(item.added_at)}
            for item, symbol, name in rows
        ],
    }


@router.post("/watchlists/{watchlist_id}/items", status_code=201)
def add_item(
    watchlist_id: int,
    session: SessionDep,
    user: UserDep,
    payload: Annotated[dict, Body()],
) -> dict:
    watchlist = _owned_watchlist(session, user, watchlist_id)
    symbol = (payload.get("symbol") or "").strip().upper()
    if not symbol:
        raise errors.invalid_params("'symbol' is required.")

    stock = session.scalar(select(Stock).where(Stock.symbol == symbol))
    if stock is None:
        raise errors.not_found(f"Symbol '{symbol}'")

    existing = session.scalar(
        select(WatchlistItem).where(
            WatchlistItem.watchlist_id == watchlist.id,
            WatchlistItem.stock_id == stock.id,
        )
    )
    if existing is not None:
        return {"id": existing.id, "symbol": symbol, "note": existing.note,
                "already_present": True}

    item = WatchlistItem(
        watchlist_id=watchlist.id, stock_id=stock.id, note=payload.get("note")
    )
    session.add(item)
    session.flush()
    return {"id": item.id, "symbol": symbol, "note": item.note,
            "already_present": False}


@router.delete("/watchlists/{watchlist_id}/items/{item_id}", status_code=204)
def remove_item(
    watchlist_id: int, item_id: int, session: SessionDep, user: UserDep
) -> None:
    watchlist = _owned_watchlist(session, user, watchlist_id)
    item = session.scalar(
        select(WatchlistItem).where(
            WatchlistItem.id == item_id, WatchlistItem.watchlist_id == watchlist.id
        )
    )
    if item is None:
        raise errors.not_found(f"Watchlist item {item_id}")
    session.delete(item)
    session.flush()


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@router.get("/alerts")
def list_alerts(session: SessionDep, user: UserDep) -> dict:
    rows = session.scalars(
        select(Alert).where(Alert.user_id == user.id).order_by(Alert.id)
    ).all()
    symbols = dict(session.execute(select(Stock.id, Stock.symbol)).all())
    return {
        "alerts": [
            {
                "id": row.id, "kind": row.kind, "channel": row.channel,
                "is_active": row.is_active,
                "symbol": symbols.get(row.stock_id),
                "conditions": loads(row.conditions) or BUILTIN_CONDITIONS.get(row.kind),
            }
            for row in rows
        ],
        "builtin_kinds": sorted(BUILTIN_CONDITIONS),
    }


@router.post("/alerts", status_code=201)
def create_alert(
    session: SessionDep, user: UserDep, payload: Annotated[dict, Body()]
) -> dict:
    kind = (payload.get("kind") or "").strip().upper()
    conditions = payload.get("conditions")

    if not kind and not conditions:
        raise errors.invalid_params(
            "An alert needs a built-in 'kind' or a custom 'conditions' tree.",
            builtin_kinds=sorted(BUILTIN_CONDITIONS),
        )
    if kind and not conditions and kind not in BUILTIN_CONDITIONS:
        raise errors.invalid_params(
            f"Unknown alert kind '{kind}'.", builtin_kinds=sorted(BUILTIN_CONDITIONS)
        )
    if conditions is not None:
        if not isinstance(conditions, dict):
            raise errors.invalid_params("'conditions' must be a condition tree object.")
        try:
            validate(conditions)
        except Exception as exc:  # noqa: BLE001
            raise errors.invalid_params(f"Invalid condition tree: {exc}") from exc

    stock_id = None
    symbol = (payload.get("symbol") or "").strip().upper()
    if symbol:
        stock = session.scalar(select(Stock).where(Stock.symbol == symbol))
        if stock is None:
            raise errors.not_found(f"Symbol '{symbol}'")
        stock_id = stock.id

    row = Alert(
        user_id=user.id,
        stock_id=stock_id,
        kind=kind or "CUSTOM",
        conditions=json.dumps(conditions) if conditions else None,
        channel=(payload.get("channel") or "IN_APP").upper(),
        is_active=bool(payload.get("is_active", True)),
    )
    session.add(row)
    session.flush()
    return {"id": row.id, "kind": row.kind, "symbol": symbol or None,
            "channel": row.channel, "is_active": row.is_active}


@router.delete("/alerts/{alert_id}", status_code=204)
def delete_alert(alert_id: int, session: SessionDep, user: UserDep) -> None:
    row = session.scalar(
        select(Alert).where(Alert.id == alert_id, Alert.user_id == user.id)
    )
    if row is None:
        raise errors.not_found(f"Alert {alert_id}")
    session.delete(row)
    session.flush()


@router.get("/alerts/triggered")
def triggered(
    session: SessionDep,
    user: UserDep,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    unacknowledged_only: Annotated[bool, Query()] = False,
) -> dict:
    start, end = window(from_, to, default_days=30)
    stmt = (
        select(AlertTrigger, Alert.kind)
        .join(Alert, Alert.id == AlertTrigger.alert_id)
        .where(
            Alert.user_id == user.id,
            AlertTrigger.trigger_date >= start,
            AlertTrigger.trigger_date <= end,
        )
    )
    if unacknowledged_only:
        stmt = stmt.where(AlertTrigger.acknowledged.is_(False))

    rows = session.execute(stmt.order_by(AlertTrigger.id.desc())).all()
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "triggers": [
            {
                "id": trigger.id, "alert_id": trigger.alert_id, "kind": kind,
                "trigger_date": iso(trigger.trigger_date),
                "triggered_at": iso(trigger.triggered_at),
                "acknowledged": trigger.acknowledged,
                "detail": loads(trigger.detail, {}),
            }
            for trigger, kind in rows
        ],
    }


@router.post("/alerts/triggered/{trigger_id}/acknowledge")
def acknowledge_trigger(
    trigger_id: int, session: SessionDep, user: UserDep
) -> dict:
    owned = session.scalar(
        select(AlertTrigger.id)
        .join(Alert, Alert.id == AlertTrigger.alert_id)
        .where(AlertTrigger.id == trigger_id, Alert.user_id == user.id)
    )
    if owned is None:
        raise errors.not_found(f"Alert trigger {trigger_id}")
    acknowledge(session, trigger_id)
    return {"id": trigger_id, "acknowledged": True}
