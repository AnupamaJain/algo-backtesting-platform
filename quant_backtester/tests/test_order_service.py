"""OrderService: the safety controls that sit above every adapter."""

from __future__ import annotations

import pytest

from quant_backtester.src.broker import (
    BrokerUnavailable,
    MockAdapter,
    OrderRejected,
    OrderService,
    OrderStatus,
    RateLimited,
    SafetyGateBlocked,
    SafetySettings,
    Side,
)
from quant_backtester.src.broker.mock import rate_limited_adapter, unavailable_adapter
from quant_backtester.src.broker.store import BrokerStore

PRICES = {"SPY": 500.0, "AAPL": 200.0}


@pytest.fixture
def store(tmp_path) -> BrokerStore:
    return BrokerStore(tmp_path / "svc.db")


def service(store, adapter=None, **safety) -> OrderService:
    return OrderService(
        adapter or MockAdapter(prices=dict(PRICES)),
        store,
        SafetySettings(**safety),
    )


# ==========================================================================
# The dry-run gate
# ==========================================================================


def test_dry_run_is_the_default(store):
    """A fresh install must never be able to place a real order."""
    assert SafetySettings().live_trading is False
    assert service(store).safety.live_trading is False


def test_dry_run_does_not_reach_the_broker(store):
    adapter = MockAdapter(prices=dict(PRICES))
    order = service(store, adapter, live_trading=False).place_order("SPY", Side.BUY, 10)

    assert order.dry_run is True
    assert order.status is OrderStatus.REJECTED
    assert "DRY-RUN" in order.status_message
    assert adapter.get_orders() == [], "no order should have reached the adapter"
    assert adapter.get_positions() == [], "no position should have been created"


def test_dry_run_order_is_still_persisted(store):
    """The point is auditability: a blocked order must still be reconstructable."""
    order = service(store, live_trading=False).place_order("SPY", Side.BUY, 10)
    saved = store.get_order(order.order_id)
    assert saved is not None
    assert saved.dry_run is True
    assert saved.symbol == "SPY" and saved.quantity == 10


def test_dry_run_returns_no_fabricated_broker_id(store):
    """A fake order id would let callers believe the order is live."""
    order = service(store, live_trading=False).place_order("SPY", Side.BUY, 10)
    assert not order.broker_order_id


def test_live_mode_actually_places(store):
    adapter = MockAdapter(prices=dict(PRICES))
    order = service(store, adapter, live_trading=True).place_order("SPY", Side.BUY, 10)

    assert order.dry_run is False
    assert order.status is OrderStatus.COMPLETE
    assert len(adapter.get_orders()) == 1


def test_force_cannot_bypass_the_dry_run_gate(store):
    """force exists for duplicate suppression only. The gate is absolute."""
    adapter = MockAdapter(prices=dict(PRICES))
    order = service(store, adapter, live_trading=False).place_order(
        "SPY", Side.BUY, 10, force=True
    )
    assert order.dry_run is True
    assert adapter.get_orders() == []


# ==========================================================================
# Duplicate suppression
# ==========================================================================


def test_identical_order_within_window_is_blocked(store):
    svc = service(store, live_trading=True, duplicate_window_seconds=60)
    svc.place_order("SPY", Side.BUY, 10)
    with pytest.raises(SafetyGateBlocked, match="duplicate"):
        svc.place_order("SPY", Side.BUY, 10)


def test_force_overrides_duplicate_suppression(store):
    svc = service(store, live_trading=True, duplicate_window_seconds=60)
    svc.place_order("SPY", Side.BUY, 10)
    second = svc.place_order("SPY", Side.BUY, 10, force=True)
    assert second.status is OrderStatus.COMPLETE


def test_different_orders_are_not_duplicates(store):
    svc = service(store, live_trading=True, duplicate_window_seconds=60)
    svc.place_order("SPY", Side.BUY, 10)
    svc.place_order("SPY", Side.BUY, 11)      # different size
    svc.place_order("SPY", Side.SELL, 10)     # different side
    svc.place_order("AAPL", Side.BUY, 10)     # different symbol
    assert len(store.list_orders()) == 4


def test_zero_window_disables_suppression(store):
    svc = service(store, live_trading=True, duplicate_window_seconds=0)
    svc.place_order("SPY", Side.BUY, 10)
    svc.place_order("SPY", Side.BUY, 10)
    assert len(store.list_orders()) == 2


# ==========================================================================
# Notional ceiling
# ==========================================================================


def test_oversized_order_is_blocked(store):
    svc = service(store, live_trading=True, max_order_value=1000)
    with pytest.raises(SafetyGateBlocked, match="exceeds max_order_value"):
        svc.place_order("SPY", Side.BUY, 100)  # 100 x 500 = 50,000


def test_order_within_ceiling_passes(store):
    svc = service(store, live_trading=True, max_order_value=10_000)
    assert svc.place_order("SPY", Side.BUY, 10).status is OrderStatus.COMPLETE


def test_blocked_order_is_recorded_as_an_event(store):
    svc = service(store, live_trading=True, max_order_value=100)
    with pytest.raises(SafetyGateBlocked):
        svc.place_order("SPY", Side.BUY, 100)
    kinds = [e["kind"] for e in store.list_events()]
    assert "order_blocked" in kinds


# ==========================================================================
# Retry policy
# ==========================================================================


def test_transient_failure_is_retried(store):
    adapter = rate_limited_adapter(times=2)
    adapter.set_price("SPY", 500.0)
    order = service(
        store, adapter, live_trading=True, max_retries=3, backoff_seconds=0.001
    ).place_order("SPY", Side.BUY, 1)
    assert order.status is OrderStatus.COMPLETE


def test_retry_gives_up_after_the_limit(store):
    adapter = unavailable_adapter(times=99)
    adapter.set_price("SPY", 500.0)
    with pytest.raises(BrokerUnavailable):
        service(
            store, adapter, live_trading=True, max_retries=2, backoff_seconds=0.001
        ).place_order("SPY", Side.BUY, 1)


def test_rejection_is_never_retried(store):
    """Retrying a rejected order just gets it rejected again."""
    adapter = MockAdapter(prices=dict(PRICES))
    adapter.fail_next_with = OrderRejected("insufficient margin", broker="mock")
    adapter.fail_times = 99

    with pytest.raises(OrderRejected):
        service(
            store, adapter, live_trading=True, max_retries=5, backoff_seconds=0.001
        ).place_order("SPY", Side.BUY, 1)

    attempts = [e for e in store.list_events() if e["kind"] == "order_failed"]
    assert attempts and attempts[0]["detail"]["attempts"] == 1


def test_rate_limited_error_is_marked_retryable():
    assert RateLimited("slow down").retryable is True
    assert OrderRejected("no margin").retryable is False
    assert BrokerUnavailable("down").retryable is True


# ==========================================================================
# Cancellation and reporting
# ==========================================================================


def test_cancelling_a_dry_run_order_is_local(store):
    svc = service(store, live_trading=False)
    order = svc.place_order("SPY", Side.BUY, 10)
    cancelled = svc.cancel_order(order.order_id)
    assert cancelled.status is OrderStatus.CANCELLED
    assert "dry-run" in cancelled.status_message.lower()


def test_describe_reports_the_active_mode(store):
    assert service(store, live_trading=False).describe()["safety"]["mode"] == "DRY-RUN"
    assert service(store, live_trading=True).describe()["safety"]["mode"] == "LIVE"
