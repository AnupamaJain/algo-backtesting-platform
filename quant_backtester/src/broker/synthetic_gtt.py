"""Trigger-price orders for brokers with no native GTT.

Zerodha calls them GTT, Dhan calls them Forever Orders, and Flattrade does
not expose them on the PiConnect API at all. The strategies that need them
should not have to care: a stop is a stop.

This is the PRD's stated remedy (FR-MG4) for the third case — "a local
trigger-price watcher that places a regular order when hit". Triggers are
persisted, polled against live quotes, and converted into ordinary market or
limit orders the moment they fire.

WHAT A LOCAL TRIGGER IS NOT. An exchange-resident GTT survives your process
dying, your laptop sleeping and your network dropping. This does not. It is
strictly weaker, and the difference matters most in exactly the situation a
stop exists for — a fast move while nobody is watching. So:

  * `is_exchange_resident` is False, and callers can read it
  * every trigger records `armed_at`, so a stale watcher is visible
  * `check()` reports what it fired rather than firing silently

Use it for paper trading and for brokers that leave no alternative. Prefer a
native GTT wherever the broker has one.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .exceptions import BrokerError
from .models import OrderType, Side, UnifiedOrder

logger = logging.getLogger(__name__)

__all__ = ["SyntheticGTT", "Trigger", "TriggerStore"]


@dataclass
class Trigger:
    """One standing instruction, waiting on a price."""

    symbol: str
    side: str                    # BUY | SELL
    quantity: float
    trigger_price: float
    #: ABOVE fires when price rises to the trigger, BELOW when it falls.
    direction: str
    limit_price: float | None = None
    product: str = "INTRADAY"
    tag: str = ""
    trigger_id: str = field(default_factory=lambda: f"SGTT-{uuid.uuid4().hex[:10].upper()}")
    status: str = "active"       # active | triggered | cancelled
    armed_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    fired_at: str | None = None
    order_id: str | None = None

    def fires_at(self, price: float) -> bool:
        if self.status != "active":
            return False
        return price >= self.trigger_price if self.direction == "ABOVE" else price <= self.trigger_price


class TriggerStore:
    """Triggers on disk, so a restart does not silently disarm every stop."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[Trigger]:
        if not self._path.exists():
            return []
        try:
            rows = json.loads(self._path.read_text())
        except Exception as exc:  # noqa: BLE001
            # Losing the file silently would disarm every stop it held.
            logger.error("Unreadable trigger store %s: %s", self._path, exc)
            raise BrokerError(
                f"trigger store {self._path} is unreadable; refusing to continue "
                "with stops in an unknown state",
                broker="synthetic_gtt",
            ) from exc
        return [Trigger(**row) for row in rows]

    def save(self, triggers: list[Trigger]) -> None:
        self._path.write_text(json.dumps([asdict(t) for t in triggers], indent=1))


class SyntheticGTT:
    """Watches prices and turns triggers into real orders.

    Depends on `BrokerAdapter` only, so it works over any broker — including
    ones that DO have native GTT, for the cases where a local trigger is
    preferable (a condition the exchange cannot express, for instance).
    """

    #: The honest distinction from a native GTT.
    is_exchange_resident = False

    def __init__(self, adapter, store: TriggerStore) -> None:
        self._adapter = adapter
        self._store = store

    # -- managing triggers ---------------------------------------------

    def place(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        trigger_price: float,
        direction: str | None = None,
        limit_price: float | None = None,
        product: str = "INTRADAY",
        tag: str = "",
    ) -> str:
        """Arm a trigger. Returns its id.

        `direction` defaults from the side: a SELL stop sits BELOW the market,
        a BUY stop ABOVE it. Getting this backwards produces a trigger that
        fires immediately, so it is derived rather than left to the caller
        unless they say otherwise.
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if trigger_price <= 0:
            raise ValueError("trigger_price must be positive")

        side = str(side).upper()
        if direction is None:
            direction = "BELOW" if side == "SELL" else "ABOVE"
        direction = str(direction).upper()
        if direction not in ("ABOVE", "BELOW"):
            raise ValueError("direction must be ABOVE or BELOW")

        trigger = Trigger(
            symbol=symbol, side=side, quantity=float(quantity),
            trigger_price=float(trigger_price), direction=direction,
            limit_price=limit_price, product=product, tag=tag,
        )
        triggers = self._store.load()
        triggers.append(trigger)
        self._store.save(triggers)
        logger.info(
            "Armed %s: %s %g %s if price goes %s %.2f",
            trigger.trigger_id, side, quantity, symbol, direction, trigger_price,
        )
        return trigger.trigger_id

    def active(self) -> list[Trigger]:
        return [t for t in self._store.load() if t.status == "active"]

    def all(self) -> list[Trigger]:
        return self._store.load()

    def cancel(self, trigger_id: str) -> str:
        triggers = self._store.load()
        for trigger in triggers:
            if trigger.trigger_id == trigger_id:
                if trigger.status == "active":
                    trigger.status = "cancelled"
                self._store.save(triggers)
                return trigger_id
        raise BrokerError(f"unknown trigger {trigger_id}", broker="synthetic_gtt")

    def modify(self, trigger_id: str, **changes) -> str:
        triggers = self._store.load()
        for trigger in triggers:
            if trigger.trigger_id == trigger_id:
                for key in ("trigger_price", "quantity", "limit_price", "direction"):
                    if key in changes and changes[key] is not None:
                        setattr(trigger, key, changes[key])
                self._store.save(triggers)
                return trigger_id
        raise BrokerError(f"unknown trigger {trigger_id}", broker="synthetic_gtt")

    # -- the watch loop -------------------------------------------------

    def check(self, quotes: dict | None = None) -> list[dict]:
        """Fire every trigger whose price has been reached.

        Returns one record per trigger that fired, so a caller can log or
        alert. Placement failures are recorded against the trigger rather
        than raised: one unfillable symbol must not stop the rest of the
        book being protected.
        """
        triggers = self._store.load()
        active = [t for t in triggers if t.status == "active"]
        if not active:
            return []

        if quotes is None:
            symbols = sorted({t.symbol for t in active})
            quotes = self._adapter.get_quotes(symbols)

        fired = []
        for trigger in active:
            quote = quotes.get(trigger.symbol)
            if quote is None:
                logger.warning(
                    "No quote for %s; trigger %s cannot be evaluated this pass",
                    trigger.symbol, trigger.trigger_id,
                )
                continue
            price = float(getattr(quote, "last_price", quote))
            if not trigger.fires_at(price):
                continue

            record = {
                "trigger_id": trigger.trigger_id, "symbol": trigger.symbol,
                "side": trigger.side, "quantity": trigger.quantity,
                "trigger_price": trigger.trigger_price, "market_price": price,
            }
            try:
                order = UnifiedOrder(
                    symbol=trigger.symbol,
                    side=Side.BUY if trigger.side == "BUY" else Side.SELL,
                    quantity=trigger.quantity,
                    order_type=OrderType.LIMIT if trigger.limit_price else OrderType.MARKET,
                    limit_price=trigger.limit_price,
                    tags={"synthetic_gtt": trigger.trigger_id, "tag": trigger.tag},
                )
                placed = self._adapter.place_order(order)
                trigger.status = "triggered"
                trigger.fired_at = datetime.now().isoformat(timespec="seconds")
                trigger.order_id = placed.broker_order_id or placed.order_id
                record["order_id"] = trigger.order_id
                logger.warning(
                    "Trigger %s FIRED at %.2f -> order %s",
                    trigger.trigger_id, price, trigger.order_id,
                )
            except Exception as exc:  # noqa: BLE001
                # Leave it active: the condition still holds and the next
                # pass should try again. Marking it triggered here would
                # silently drop the protection.
                record["error"] = str(exc)
                logger.error(
                    "Trigger %s fired but the order failed (%s); leaving it armed",
                    trigger.trigger_id, exc,
                )
            fired.append(record)

        self._store.save(triggers)
        return fired

    def describe(self) -> dict:
        active = self.active()
        return {
            "exchange_resident": self.is_exchange_resident,
            "active_triggers": len(active),
            "symbols": sorted({t.symbol for t in active}),
            "warning": (
                "Local triggers only fire while this process is running. They "
                "are not held at the exchange and will not protect a position "
                "if the watcher stops."
            ),
        }
