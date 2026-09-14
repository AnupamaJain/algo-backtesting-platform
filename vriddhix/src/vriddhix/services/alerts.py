"""Alerts. Phase 10.

An alert is a saved condition evaluated against each scan. It uses the SAME
condition evaluator as the custom scanner and the backtester, so an alert and
the screen it was created from can never disagree about what they mean.

Delivery is deliberately abstracted behind ``Channel``. Phase 10 ships in-app
only; email/Telegram/webhook slot in without touching evaluation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Alert, AlertTrigger, Stock
from .conditions import evaluate, validate

logger = logging.getLogger(__name__)


class Channel(Protocol):
    name: str

    def deliver(self, alert: Alert, detail: dict) -> None: ...


class InAppChannel:
    """Persists the trigger; the UI reads it. No external dependency."""

    name = "IN_APP"

    def deliver(self, alert: Alert, detail: dict) -> None:  # noqa: D401
        return None


@dataclass
class AlertReport:
    evaluated: int = 0
    triggered: int = 0
    skipped: int = 0
    errors: list[str] = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


#: Built-in alert kinds expressed as condition trees, so they run through the
#: same evaluator as user-defined ones rather than via bespoke `if` branches.
BUILTIN_CONDITIONS: dict[str, dict] = {
    "PRICE_REACHES_PIVOT": {
        "op": "AND",
        "children": [
            {"field": "distance_from_pivot_pct", "cmp": "lte", "value": 0.5},
            {"field": "distance_from_pivot_pct", "cmp": "gte", "value": -5.0},
        ],
    },
    "BREAKOUT": {"field": "vcp_stage", "cmp": "eq", "value": "BREAKOUT"},
    "NEAR_PIVOT": {"field": "vcp_stage", "cmp": "eq", "value": "NEAR_PIVOT"},
    "VOLUME_CONFIRMATION": {"field": "rel_volume", "cmp": "gte", "value": 1.5},
    "RS_IMPROVEMENT": {
        "op": "AND",
        "children": [
            {"field": "rs_trend", "cmp": "eq", "value": "IMPROVING"},
            {"field": "rs_score", "cmp": "gte", "value": 70},
        ],
    },
    "SCORE_ABOVE_80": {"field": "vcp_score", "cmp": "gte", "value": 80},
    "BOS_BULLISH": {"field": "bos_direction", "cmp": "eq", "value": "BULLISH"},
    "SECTOR_LEADING": {"field": "sector_quadrant", "cmp": "eq", "value": "LEADING"},
}


def conditions_for(alert: Alert) -> dict | None:
    """A custom tree if present, otherwise the built-in for this kind."""
    if alert.conditions:
        try:
            return json.loads(alert.conditions)
        except json.JSONDecodeError:
            logger.error("alert %s has unparseable conditions", alert.id)
            return None
    return BUILTIN_CONDITIONS.get(alert.kind)


def evaluate_alerts(
    session: Session, rows, as_of: date, *, channels: dict[str, Channel] | None = None
) -> AlertReport:
    """Run every active alert against the scan rows."""
    channels = channels or {"IN_APP": InAppChannel()}
    report = AlertReport()

    alerts = list(session.scalars(select(Alert).where(Alert.is_active.is_(True))).all())
    if not alerts:
        return report

    by_symbol = {row.symbol: row for row in rows}
    stock_symbols = {
        sid: symbol
        for sid, symbol in session.execute(select(Stock.id, Stock.symbol)).all()
    }

    for alert in alerts:
        tree = conditions_for(alert)
        if tree is None:
            report.skipped += 1
            continue
        try:
            validate(tree)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"alert {alert.id}: {exc}")
            report.skipped += 1
            continue

        # A stock-scoped alert watches one symbol; an unscoped one watches the
        # whole scan.
        if alert.stock_id is not None:
            symbol = stock_symbols.get(alert.stock_id)
            candidates = [by_symbol[symbol]] if symbol in by_symbol else []
        else:
            candidates = list(rows)

        for row in candidates:
            report.evaluated += 1
            result = evaluate(tree, row.as_fields())
            if not result.passed:
                continue
            if _already_triggered(session, alert.id, row.symbol, as_of):
                continue

            detail = {
                "symbol": row.symbol,
                "matched": list(result.matched),
                "close": row.close,
                "vcp_score": row.vcp_score,
                "vcp_stage": row.vcp_stage,
                "rs_score": row.rs_score,
            }
            session.add(
                AlertTrigger(
                    alert_id=alert.id, trigger_date=as_of, detail=json.dumps(detail)
                )
            )
            channel = channels.get(alert.channel, channels["IN_APP"])
            channel.deliver(alert, detail)
            report.triggered += 1

    session.flush()
    return report


def _already_triggered(session: Session, alert_id: int, symbol: str, on: date) -> bool:
    """One trigger per alert per symbol per day.

    Without this a daily scan re-alerts every day a condition stays true, and
    the user stops reading them -- which is the same as having no alerts.
    """
    existing = session.scalars(
        select(AlertTrigger).where(
            AlertTrigger.alert_id == alert_id, AlertTrigger.trigger_date == on
        )
    ).all()
    for trigger in existing:
        if not trigger.detail:
            continue
        try:
            if json.loads(trigger.detail).get("symbol") == symbol:
                return True
        except json.JSONDecodeError:
            continue
    return False


def acknowledge(session: Session, trigger_id: int) -> None:
    trigger = session.get(AlertTrigger, trigger_id)
    if trigger is not None:
        trigger.acknowledged = True
        session.flush()
