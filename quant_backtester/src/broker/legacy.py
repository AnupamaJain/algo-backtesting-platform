"""Migration shim for legacy modules written against a broker-specific SDK.

The production strategies at the repo root predate the abstraction layer.
They were written against one vendor's SDK and call it directly — `quote()`,
`positions()`, `place_order()` and so on, in that vendor's vocabulary.

This module lets them run on *any* configured broker without being rewritten,
by translating those calls into `BrokerAdapter` calls and translating the
`Unified*` results back into the plain dicts they expect.

The direction of the dependency is the point. This shim depends on
`BrokerAdapter`; nothing in the platform depends on this shim. It is an
adapter at the edge of the system for code we have not migrated yet, not an
interface anyone should build against. New code uses `BrokerAdapter` directly
and speaks in `UnifiedOrder`/`UnifiedPosition`, which carry meaning no vendor
vocabulary can.

CAPABILITY-DRIVEN, NOT VENDOR-DRIVEN. Whether GTT orders work is a question
about the configured broker, answered by `adapter.capabilities.supports_gtt` —
not by hardcoding one vendor's limitations. A broker that gains the feature
gets it here for free; one that lacks it fails loudly rather than returning an
empty list. An empty GTT list would tell a monitor that no triggers exist,
which reads as "this position needs no protection" — a silent wrong answer
about a stop-loss, with real money behind it.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from .adapter import BrokerAdapter
from .exceptions import BrokerError
from .models import (
    Fill,
    OrderType,
    ProductType,
    Side,
    UnifiedOrder,
    UnifiedPosition,
    UnifiedQuote,
)

logger = logging.getLogger(__name__)

__all__ = [
    "UnsupportedOperation",
    "DryRunBlocked",
    "LegacyBrokerShim",
    "build_legacy_client",
]


class UnsupportedOperation(BrokerError):
    """The configured broker does not provide this capability."""


class DryRunBlocked(BrokerError):
    """An order was refused because live trading is switched off."""


def _live_trading_enabled() -> bool:
    """Read the dry-run kill switch, defaulting to OFF.

    Deliberately fail-safe: a missing or unreadable config means NO live
    orders. The legacy modules were written against a broker they had to be
    logged into manually, and several of them — early_exit_lib among them —
    call place_order() with no gate of their own. Pointing that code at a
    live adapter without a check here is how a research session places real
    NFO orders.
    """
    from ..config import REPO_ROOT

    return read_live_trading_flag(REPO_ROOT.parent / "configfile.ini")


def read_live_trading_flag(path) -> bool:
    """Whether `path` arms live trading. Missing or unreadable means NO."""
    import configparser
    from pathlib import Path as _Path

    path = _Path(path)
    if not path.exists():
        return False
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
        return parser.getboolean("safety", "live_trading", fallback=False)
    except Exception:  # noqa: BLE001 - unreadable config means unsafe
        return False


#: Legacy vocabulary -> the platform's neutral enums. Kept here, at the edge,
#: so no other module has to know a vendor's spelling of "market order".
_SIDES = {"BUY": Side.BUY, "B": Side.BUY, "SELL": Side.SELL, "S": Side.SELL}
_ORDER_TYPES = {
    "MARKET": OrderType.MARKET,
    "LIMIT": OrderType.LIMIT,
    "SL": OrderType.STOP_LIMIT,
    "SL-M": OrderType.STOP,
    "SLM": OrderType.STOP,
}
_PRODUCTS = {
    "MIS": ProductType.INTRADAY,
    "CNC": ProductType.DELIVERY,
    "NRML": ProductType.CARRYFORWARD,
}


class LegacyBrokerShim:
    """Presents the legacy SDK's method surface over any `BrokerAdapter`."""

    # Constants the legacy modules read off the client object.
    EXCHANGE_NSE = "NSE"
    EXCHANGE_BSE = "BSE"
    EXCHANGE_NFO = "NFO"
    EXCHANGE_BFO = "BFO"
    EXCHANGE_MCX = "MCX"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"
    PRODUCT_MIS = "MIS"
    PRODUCT_CNC = "CNC"
    PRODUCT_NRML = "NRML"
    ORDER_TYPE_MARKET = "MARKET"
    ORDER_TYPE_LIMIT = "LIMIT"
    ORDER_TYPE_SL = "SL"
    ORDER_TYPE_SLM = "SL-M"
    VARIETY_REGULAR = "regular"
    VARIETY_AMO = "amo"
    VALIDITY_DAY = "DAY"
    VALIDITY_IOC = "IOC"

    def __init__(self, adapter: BrokerAdapter, default_exchange: str = "NSE") -> None:
        self._adapter = adapter
        self._default_exchange = default_exchange

    @property
    def adapter(self) -> BrokerAdapter:
        """The real client, for code that has been migrated."""
        return self._adapter

    @property
    def access_token(self) -> str | None:
        """Kite-shaped code reads `kite.access_token` directly to test
        whether a session exists (cas_tracker, expiry_trade_lib among them).

        The shim authenticates at construction rather than via
        set_access_token(), so without this property such a check always
        reads None and the calling code concludes "not logged in" forever —
        a silent no-op wearing the shape of a real session check.
        """
        session = self._adapter.session
        return session.access_token if session else None

    # -- session ------------------------------------------------------

    def set_access_token(self, access_token: str) -> None:
        """No-op: authentication is the adapter's business.

        The legacy modules call this unconditionally at startup, so it must
        exist and must not raise.
        """
        logger.debug("set_access_token ignored; the adapter owns authentication")

    def generate_session(self, request_token: str = "", api_secret: str = "") -> dict:
        session = self._adapter.session or self._adapter.authenticate()
        return {"access_token": session.access_token, "user_id": session.user_id}

    def profile(self) -> dict:
        session = self._adapter.session or self._adapter.authenticate()
        return {"user_id": session.user_id, "broker": self._adapter.__class__.__name__}

    def margins(self, segment: str | None = None) -> dict:
        account = self._adapter.get_account()
        payload = {
            "enabled": True,
            "net": float(account.equity),
            "available": {"live_balance": float(account.cash), "cash": float(account.cash)},
            "utilised": {"debits": float(account.equity - account.cash)},
        }
        return payload if segment else {"equity": payload, "commodity": payload}

    # -- market data --------------------------------------------------

    def quote(self, instruments) -> dict:
        out: dict = {}
        for key in _as_list(instruments):
            _, symbol = _split(key, self._default_exchange)
            try:
                q = self._adapter.get_quote(symbol)
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not kill a batch
                logger.warning("quote failed for %s: %s", key, exc)
                continue
            out[key] = _quote_to_dict(q)
        return out

    def ltp(self, instruments) -> dict:
        return {
            key: {"instrument_token": q.get("instrument_token"), "last_price": q["last_price"]}
            for key, q in self.quote(instruments).items()
        }

    def historical_data(
        self,
        instrument_token,
        from_date,
        to_date,
        interval: str = "day",
        continuous: bool = False,
        oi: bool = False,
    ) -> list[dict]:
        """Historical bars, if the configured broker can supply them."""
        fetch = getattr(self._adapter, "get_history", None)
        if fetch is None:
            raise UnsupportedOperation(
                f"{self._adapter.__class__.__name__} does not expose historical bars; "
                "use the HistoricalDataManager or IntradayDataManager instead",
                broker=self._broker_name(),
            )
        _, symbol = _split(str(instrument_token), self._default_exchange)
        frame = fetch(symbol, _as_datetime(from_date), _as_datetime(to_date), interval)
        return [
            {
                "date": index.to_pydatetime() if hasattr(index, "to_pydatetime") else index,
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row.get("Volume", 0) or 0),
            }
            for index, row in frame.iterrows()
        ]

    def instruments(self, exchange: str | None = None) -> list[dict]:
        rows = self._adapter.get_instruments()
        return [
            {
                "tradingsymbol": i.symbol,
                "exchange": i.exchange,
                "instrument_token": i.broker_token,
                "lot_size": i.lot_size,
                "tick_size": i.tick_size,
                "instrument_type": str(i.instrument_type),
            }
            for i in rows
            if exchange is None or i.exchange == exchange
        ]

    # -- orders -------------------------------------------------------

    def place_order(
        self,
        variety: str = "regular",
        exchange: str | None = None,
        tradingsymbol: str | None = None,
        transaction_type: str = "BUY",
        quantity: int = 0,
        product: str = "MIS",
        order_type: str = "MARKET",
        price: float | None = None,
        trigger_price: float | None = None,
        validity: str = "DAY",
        tag: str | None = None,
        **_ignored,
    ) -> str:
        order = UnifiedOrder(
            symbol=tradingsymbol or "",
            side=_SIDES.get(str(transaction_type).upper(), Side.BUY),
            quantity=abs(int(quantity)),
            order_type=_ORDER_TYPES.get(str(order_type).upper(), OrderType.MARKET),
            limit_price=float(price) if price else None,
            stop_price=float(trigger_price) if trigger_price else None,
            product=_PRODUCTS.get(str(product).upper(), ProductType.INTRADAY),
            tags={"legacy_tag": tag} if tag else {},
        )
        if not self._adapter.is_simulated and not _live_trading_enabled():
            # Log the full intent so a dry run is still an audit trail.
            logger.warning(
                "DRY RUN — refusing to place %s %s %s (%s). Set "
                "[safety] live_trading = true in configfile.ini to arm.",
                order.side.value, order.quantity, order.symbol,
                order.order_type.value,
            )
            raise DryRunBlocked(
                f"live trading is disabled; {order.side.value} {order.quantity:g} "
                f"{order.symbol} was not sent",
                broker=self._broker_name(),
            )
        # A simulated broker (paper/mock) has no real money to protect, so
        # the gate does not apply -- blocking it would defeat the entire
        # point of paper mode, which is to observe realistic fills.

        placed = self._adapter.place_order(order)
        if not placed.broker_order_id:
            raise BrokerError(
                f"order was not accepted: {placed.status_message or placed.status}",
                broker=self._broker_name(),
            )
        return str(placed.broker_order_id)

    def modify_order(
        self,
        variety: str = "regular",
        order_id: str | None = None,
        quantity: int | None = None,
        price: float | None = None,
        order_type: str | None = None,
        trigger_price: float | None = None,
        **_ignored,
    ) -> str:
        self._adapter.modify_order(
            str(order_id),
            quantity=int(quantity) if quantity is not None else None,
            limit_price=float(price) if price is not None else None,
            stop_price=float(trigger_price) if trigger_price is not None else None,
        )
        return str(order_id)

    def cancel_order(self, variety: str = "regular", order_id: str | None = None, **_ignored) -> str:
        self._adapter.cancel_order(str(order_id))
        return str(order_id)

    def orders(self) -> list[dict]:
        return [_order_to_dict(o) for o in self._adapter.get_orders()]

    def order_history(self, order_id: str) -> list[dict]:
        return [_order_to_dict(self._adapter.get_order_status(str(order_id)))]

    def trades(self) -> list[dict]:
        return [_fill_to_dict(f) for f in self._adapter.get_fills()]

    # -- portfolio ----------------------------------------------------

    def positions(self) -> dict:
        rows = [_position_to_dict(p) for p in self._adapter.get_positions()]
        # The legacy callers read both keys; this book is already net.
        return {"net": rows, "day": rows}

    def holdings(self) -> list[dict]:
        return [
            r for r in _as_list_of_dicts(self.positions()["net"])
            if str(r.get("product")) == str(ProductType.DELIVERY)
        ]

    # -- capability-gated ---------------------------------------------

    @property
    def _synthetic(self):
        """Local trigger watcher, for brokers with no native GTT.

        Built lazily and only when needed. The PRD's remedy (FR-MG4) for a
        broker that cannot express a standing trigger: watch the price
        locally and place a real order when it is reached.
        """
        if getattr(self, "_synthetic_gtt", None) is None:
            from pathlib import Path

            from ..config import REPO_ROOT
            from .synthetic_gtt import SyntheticGTT, TriggerStore

            from .adapter import account_key

            store = TriggerStore(
                Path(REPO_ROOT) / "state" / f"{account_key(self._adapter)}_triggers.json"
            )
            self._synthetic_gtt = SyntheticGTT(self._adapter, store)
        return self._synthetic_gtt

    def _require(self, capability: str, feature: str) -> None:
        caps = self._adapter.capabilities
        if not getattr(caps, capability, False):
            raise UnsupportedOperation(
                f"{feature} is not supported by the configured broker "
                f"({self._broker_name()}). Returning an empty result here would "
                f"misreport protection that does not exist, so this fails instead. "
                f"Broker capabilities: {caps.describe()}",
                broker=self._broker_name(),
            )

    def _broker_name(self) -> str:
        return getattr(self._adapter, "name", self._adapter.__class__.__name__)

    def get_gtts(self):
        """Standing triggers — native where the broker has them, local
        otherwise. Callers cannot tell, which is the point."""
        if self._adapter.capabilities.supports_gtt:
            return self._adapter.get_gtts()  # type: ignore[attr-defined]
        return [
            {
                "id": t.trigger_id,
                "status": t.status,
                "condition": {
                    "tradingsymbol": t.symbol,
                    "trigger_values": [t.trigger_price],
                },
                "orders": [
                    {"transaction_type": t.side, "quantity": int(t.quantity),
                     "price": t.limit_price or t.trigger_price}
                ],
                # Flagged so a monitor can tell the difference if it cares.
                "exchange_resident": False,
            }
            for t in self._synthetic.all()
        ]

    def place_gtt(self, *args, **kwargs):
        if not self._adapter.is_simulated and not _live_trading_enabled():
            raise DryRunBlocked(
                "live trading is disabled; the GTT was not placed",
                broker=self._broker_name(),
            )
        if self._adapter.capabilities.supports_gtt:
            return self._adapter.place_gtt(*args, **kwargs)  # type: ignore[attr-defined]

        # No native GTT: arm a local trigger instead. Weaker — it only fires
        # while the watcher runs — but a stop that exists beats one refused.
        symbol = kwargs.get("symbol") or kwargs.get("tradingsymbol")
        triggers = kwargs.get("trigger_values") or [kwargs.get("trigger_price")]
        orders = kwargs.get("orders") or [{}]
        first = orders[0] if orders else {}
        logger.warning(
            "%s has no native GTT; arming a LOCAL trigger for %s. It will not "
            "fire if this process stops.", self._broker_name(), symbol,
        )
        return self._synthetic.place(
            symbol=symbol,
            side=kwargs.get("side") or first.get("transaction_type", "SELL"),
            quantity=kwargs.get("quantity") or first.get("quantity", 0),
            trigger_price=float(triggers[0]),
            limit_price=kwargs.get("limit_price") or first.get("price"),
        )

    def modify_gtt(self, trigger_id=None, *args, **kwargs):
        if self._adapter.capabilities.supports_gtt:
            return self._adapter.modify_gtt(trigger_id, *args, **kwargs)  # type: ignore[attr-defined]
        return self._synthetic.modify(trigger_id, **kwargs)

    def delete_gtt(self, trigger_id=None, *args, **kwargs):
        if self._adapter.capabilities.supports_gtt:
            return self._adapter.delete_gtt(trigger_id, *args, **kwargs)  # type: ignore[attr-defined]
        return self._synthetic.cancel(trigger_id)

    def check_gtts(self) -> list[dict]:
        """Poll local triggers and fire any that have been reached.

        A no-op on brokers with native GTT: the exchange is doing the
        watching. Callers can run it unconditionally in their loop.
        """
        if self._adapter.capabilities.supports_gtt:
            return []
        return self._synthetic.check()

    def basket_order_margins(self, *args, **kwargs):
        self._require("supports_bracket_orders", "basket order margin calculation")
        return self._adapter.basket_order_margins(*args, **kwargs)  # type: ignore[attr-defined]

    def place_mf_order(self, *args, **kwargs):
        raise UnsupportedOperation(
            "mutual-fund orders are outside this platform's scope",
            broker=self._broker_name(),
        )

    cancel_mf_order = place_mf_order
    mf_instruments = place_mf_order


# ==========================================================================
# TRANSLATION
# ==========================================================================


def _as_list(value) -> list[str]:
    return [value] if isinstance(value, str) else [str(v) for v in value]


def _as_list_of_dicts(rows) -> list[dict]:
    return list(rows)


def _split(key: str, default_exchange: str) -> tuple[str, str]:
    if ":" in key:
        exchange, symbol = key.split(":", 1)
        return exchange, symbol
    return default_exchange, key


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    return datetime.fromisoformat(str(value))


def _f(value) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result == result else 0.0


def _quote_to_dict(q: UnifiedQuote) -> dict:
    return {
        # A numeric broker token when the quote source resolved one -- legacy
        # code subscribes a websocket ticker by this value, and every
        # broker's ticker protocol needs its own native id, not the symbol
        # text. Falls back to the symbol only for sources with no native
        # token concept (cached history, static test quotes).
        "instrument_token": q.broker_token or q.symbol,
        "last_price": _f(q.last_price),
        "volume": _f(q.volume),
        # The unified quote carries a last price and the previous close, not
        # an intraday OHLC. Emitting zeros for open/high/low would look like
        # real prices, so only the field that exists is populated.
        "ohlc": {"close": _f(q.previous_close)},
        "depth": {
            "buy": [{"price": _f(q.bid)}],
            "sell": [{"price": _f(q.ask)}],
        },
        "timestamp": q.timestamp,
    }


def self_exchange(o: UnifiedOrder) -> str:
    """Exchange is not part of the unified order; legacy callers still read
    the key, so it is reported as unknown rather than invented."""
    return (o.tags or {}).get("exchange", "")


def _order_to_dict(o: UnifiedOrder) -> dict:
    return {
        "order_id": o.broker_order_id or o.order_id,
        "exchange": self_exchange(o),
        "tradingsymbol": o.symbol,
        "transaction_type": "BUY" if o.side == Side.BUY else "SELL",
        "quantity": int(o.quantity),
        "filled_quantity": int(o.filled_quantity),
        "pending_quantity": int(o.remaining_quantity),
        "price": _f(o.limit_price),
        "trigger_price": _f(o.stop_price),
        "average_price": _f(o.average_price),
        "product": str(o.product),
        "order_type": str(o.order_type),
        "status": str(o.status).upper(),
        "status_message": o.status_message or "",
        "order_timestamp": o.created_at,
        "tag": (o.tags or {}).get("legacy_tag", ""),
    }


def _fill_to_dict(f: Fill) -> dict:
    return {
        "order_id": f.order_id,
        "tradingsymbol": f.symbol,
        "transaction_type": "BUY" if f.side == Side.BUY else "SELL",
        "quantity": int(f.quantity),
        "average_price": _f(f.price),
        "fill_timestamp": f.timestamp,
    }


def _position_to_dict(p: UnifiedPosition) -> dict:
    return {
        "tradingsymbol": p.symbol,
        # Several legacy callers (get_position_for_symbol and siblings) read
        # position['instrument_token'] unconditionally -- a holdover from
        # Kite, where every position always carries one. Falling back to the
        # symbol keeps the key present (no KeyError) when the adapter has no
        # native token for this position.
        "instrument_token": p.broker_token or p.symbol,
        "product": str(p.product),
        "quantity": int(p.quantity),
        "average_price": _f(p.average_price),
        "last_price": _f(p.last_price),
        "pnl": _f(p.unrealized_pnl) + _f(p.realized_pnl),
        "unrealised": _f(p.unrealized_pnl),
        "realised": _f(p.realized_pnl),
        "multiplier": 1.0,
    }


def build_legacy_client(
    config: dict | None = None, broker: str | None = None
) -> LegacyBrokerShim:
    """Build the shim over whichever broker configuration selects.

    This is the drop-in for a legacy `SomeVendorSDK(api_key=...)` call. Which
    broker it reaches is decided by broker.yaml, not by this call site — that
    is the whole point.
    """
    from pathlib import Path

    import yaml

    from ..config import REPO_ROOT
    from .factory import BrokerFactory

    if config is None:
        with (REPO_ROOT / "config" / "broker.yaml").open("r", encoding="utf-8") as fh:
            config = yaml.safe_load(fh)
    settings = config
    factory = BrokerFactory(
        settings,
        state_dir=Path(REPO_ROOT) / "state",
        data_dir=Path(REPO_ROOT) / "data",
    )
    adapter = factory.build_adapter(broker)
    adapter.authenticate()
    name = broker or factory.active_broker
    exchange = settings.get("brokers", {}).get(name, {}).get("exchange", "NSE")
    return LegacyBrokerShim(adapter, default_exchange=exchange)
