"""OrderService — the single path every order takes.

The base platform PRD requires that all order placement, from any strategy,
funnel through one shared function with retry, a dry-run gate, and durable
persistence. This is that function.

Everything here happens ABOVE the adapter, once, so a new broker integration
cannot accidentally omit a safety control:

  * **Dry-run gate.** Off by default. When live trading is disabled the order
    is fully constructed, logged and persisted exactly as if it had been
    sent — but no adapter is called. Callers get a clearly-marked simulated
    order rather than a fabricated broker id, so "nothing happened" is never
    mistaken for "it worked".
  * **Duplicate suppression.** The same economic intent fired twice within a
    short window is almost always a bug or a double-click, not two trades.
  * **Retry with backoff.** Only for failures that are actually transient —
    a rejected order is retried never, a rate limit is retried after waiting.
  * **Audit trail.** Every attempt is recorded, including the ones that were
    blocked, because "why didn't it trade?" is as important as "why did it?".
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from .adapter import BrokerAdapter
from .exceptions import BrokerError, SafetyGateBlocked
from .models import OrderStatus, ProductType, Side, UnifiedOrder
from .store import BrokerStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SafetySettings:
    """Operator-controlled guardrails.

    `live_trading` defaults to False and must be turned on deliberately —
    a fresh install can never place a real order by accident.
    """

    live_trading: bool = False
    max_order_value: float = 100_000.0
    duplicate_window_seconds: int = 30
    max_retries: int = 3
    backoff_seconds: float = 1.0

    def describe(self) -> dict:
        return {
            "live_trading": self.live_trading,
            "mode": "LIVE" if self.live_trading else "DRY-RUN",
            "max_order_value": self.max_order_value,
            "duplicate_window_seconds": self.duplicate_window_seconds,
        }


class OrderService:
    """Shared, retry-safe, gated order placement."""

    def __init__(
        self,
        adapter: BrokerAdapter,
        store: BrokerStore,
        safety: SafetySettings | None = None,
    ) -> None:
        self._adapter = adapter
        self._store = store
        self._safety = safety or SafetySettings()

    @property
    def safety(self) -> SafetySettings:
        return self._safety

    @property
    def adapter(self) -> BrokerAdapter:
        return self._adapter

    # -- placement --------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        side: Side | str,
        quantity: float,
        *,
        order_type=None,
        limit_price: float | None = None,
        stop_price: float | None = None,
        product: ProductType | str = ProductType.INTRADAY,
        strategy: str = "manual",
        force: bool = False,
    ) -> UnifiedOrder:
        """Place an order through every safety control.

        `force` bypasses duplicate suppression only — it can never bypass the
        dry-run gate, because the gate is the one control that stands between
        a misconfiguration and real money.
        """
        from .models import OrderType

        order = UnifiedOrder(
            symbol=symbol,
            side=Side(side) if isinstance(side, str) else side,
            quantity=float(quantity),
            order_type=OrderType(order_type) if isinstance(order_type, str) else (order_type or OrderType.MARKET),
            product=ProductType(product) if isinstance(product, str) else product,
            limit_price=limit_price,
            stop_price=stop_price,
            strategy=strategy,
            broker=self._adapter.name,
            order_id=f"ORD-{uuid.uuid4().hex[:12].upper()}",
            created_at=datetime.now(),
        )

        self._check_notional(order)
        if not force:
            self._check_duplicate(order)

        # The gate guards REAL money. A simulated broker has none, so
        # blocking it would only prevent practice.
        if not self._safety.live_trading and not self._adapter.is_simulated:
            return self._simulate(order)

        return self._send_with_retry(order)

    def cancel_order(self, order_id: str) -> UnifiedOrder:
        order = self._store.get_order(order_id)
        if order is not None and order.dry_run:
            # A dry-run order never reached a broker, so there is nothing to
            # cancel there; close it out locally instead of erroring.
            order.status = OrderStatus.CANCELLED
            order.status_message = "dry-run order cancelled locally"
            order.updated_at = datetime.now()
            self._store.save_order(order)
            return order
        return self._adapter.cancel_order(order_id)

    # -- controls ---------------------------------------------------------

    def _check_notional(self, order: UnifiedOrder) -> None:
        """Reject an order whose value exceeds the configured ceiling.

        A fat-finger guard: the reference price is the limit when there is
        one, otherwise the live quote.
        """
        reference = order.limit_price
        if reference is None:
            try:
                reference = self._adapter.get_quote(order.symbol).last_price
            except BrokerError:
                reference = None
        if reference is None:
            return
        value = reference * order.quantity
        if value > self._safety.max_order_value:
            self._store.log_event(
                "order_blocked",
                f"{order.symbol} order value {value:,.0f} exceeds the "
                f"{self._safety.max_order_value:,.0f} ceiling",
                severity="error",
                detail={"symbol": order.symbol, "value": value},
            )
            raise SafetyGateBlocked(
                f"order value {value:,.2f} exceeds max_order_value "
                f"{self._safety.max_order_value:,.2f}",
                broker=self._adapter.name,
            )

    def _check_duplicate(self, order: UnifiedOrder) -> None:
        window_start = datetime.now() - timedelta(seconds=self._safety.duplicate_window_seconds)
        fingerprint = order.fingerprint()
        for existing_fingerprint, existing_id in self._store.recent_fingerprints(window_start):
            if existing_fingerprint == fingerprint:
                self._store.log_event(
                    "duplicate_suppressed",
                    f"identical {order.side.value} {order.symbol} order already "
                    f"placed as {existing_id}",
                    severity="warning",
                    detail={"original_order_id": existing_id},
                )
                raise SafetyGateBlocked(
                    f"duplicate of order {existing_id} within "
                    f"{self._safety.duplicate_window_seconds}s; pass force=True to override",
                    broker=self._adapter.name,
                )

    def _simulate(self, order: UnifiedOrder) -> UnifiedOrder:
        """Record the order as if sent, without touching the broker."""
        order.dry_run = True
        order.status = OrderStatus.REJECTED
        order.status_message = (
            "DRY-RUN: live_trading is disabled, so this order was logged but not sent"
        )
        order.updated_at = datetime.now()
        self._store.save_order(order)
        self._store.log_event(
            "dry_run_order",
            f"[DRY-RUN] {order.side.value} {order.quantity:g} {order.symbol} "
            f"({order.order_type.value}) — not sent",
            severity="warning",
            detail={"order_id": order.order_id, "strategy": order.strategy},
        )
        logger.warning(
            "DRY-RUN: %s %g %s not sent (live_trading disabled)",
            order.side.value,
            order.quantity,
            order.symbol,
        )
        return order

    def _send_with_retry(self, order: UnifiedOrder) -> UnifiedOrder:
        """Send, retrying only genuinely transient failures.

        Retrying a rejection would just be rejected again; retrying a rate
        limit after a pause usually succeeds. The exception's own `retryable`
        flag decides, so the policy stays broker-agnostic.
        """
        attempt = 0
        last_error: BrokerError | None = None

        while attempt <= self._safety.max_retries:
            try:
                placed = self._adapter.place_order(order)
                self._store.save_order(placed)
                return placed
            except BrokerError as exc:
                last_error = exc
                if not exc.retryable:
                    break
                attempt += 1
                if attempt > self._safety.max_retries:
                    break
                delay = getattr(exc, "retry_after", None) or (
                    self._safety.backoff_seconds * (2 ** (attempt - 1))
                )
                logger.warning(
                    "Order attempt %s/%s failed (%s); retrying in %.1fs",
                    attempt,
                    self._safety.max_retries,
                    exc,
                    delay,
                )
                time.sleep(delay)

        order.status = OrderStatus.REJECTED
        order.status_message = str(last_error) if last_error else "order failed"
        order.updated_at = datetime.now()
        self._store.save_order(order)
        self._store.log_event(
            "order_failed",
            f"{order.symbol} order failed: {order.status_message}",
            severity="error",
            detail={"order_id": order.order_id, "attempts": attempt + 1},
        )
        if last_error:
            raise last_error
        return order

    # -- reads ------------------------------------------------------------

    def sync(self) -> list[UnifiedOrder]:
        """Reconcile working orders with the broker.

        The authoritative source for strategy decisions — a streaming update
        can arrive out of order, so state is confirmed by asking.
        """
        if hasattr(self._adapter, "poll"):
            return self._adapter.poll()

        changed = []
        for order in self._store.list_working_orders():
            try:
                latest = self._adapter.get_order_status(order.order_id)
            except BrokerError:
                continue
            if latest.status is not order.status:
                self._store.save_order(latest)
                changed.append(latest)
        return changed

    @property
    def mode(self) -> str:
        """What is actually at stake right now."""
        if self._adapter.is_simulated:
            return "PAPER"
        return "LIVE" if self._safety.live_trading else "DRY-RUN"

    def describe(self) -> dict:
        safety = self._safety.describe()
        # The effective mode accounts for the adapter; the raw flag alone
        # would report DRY-RUN while paper orders were really executing.
        safety["mode"] = self.mode
        safety["simulated_broker"] = self._adapter.is_simulated
        return {"safety": safety, "broker": self._adapter.describe()}
