#!/usr/bin/env python
"""JSON command interface to the broker layer.

The console calls this for every read and every write, so Python remains the
single source of truth for broker state. Duplicating order or position logic
in TypeScript would let the dashboard and the engine drift apart — exactly the
failure this layer exists to prevent.

All output is JSON on stdout. Errors are JSON too, with a non-zero exit code,
so the caller never has to parse a traceback.

Commands:
    state                       account, positions, safety, broker health
    orders   [--page N] [--page-size N] [--symbol S] [--status S] [--strategy S]
    fills    [--page N] [--page-size N]
    events   [--page N] [--page-size N]
    quote    --symbol SPY
    quotes   --symbols SPY,QQQ
    place    --symbol SPY --side BUY --quantity 10 [--order-type LIMIT --limit-price 500]
    cancel   --order-id ORD-...
    poll                        re-check resting orders against the market
    reset                       wipe the paper account (destructive)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from quant_backtester.src.broker import BrokerFactory, BrokerError, Side  # noqa: E402
from quant_backtester.src.broker.models import OrderType, ProductType  # noqa: E402

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "broker.yaml"
STATE_DIR = ROOT / "state"
DATA_DIR = ROOT / "data"


def load_factory(config_path: Path = CONFIG_PATH) -> BrokerFactory:
    with config_path.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    return BrokerFactory(config, STATE_DIR, DATA_DIR)


def _quote_dict(quote) -> dict:
    age = (datetime.now() - quote.timestamp).total_seconds()
    return {
        "symbol": quote.symbol,
        "last_price": quote.last_price,
        "bid": quote.bid,
        "ask": quote.ask,
        "previous_close": quote.previous_close,
        "change_pct": quote.change_pct,
        "timestamp": quote.timestamp.isoformat(),
        "age_seconds": round(age, 1),
        # Anything older than a few minutes is a cached close, not a live
        # tick. The console says so rather than implying real-time data.
        "stale": age > 300,
    }


def cmd_state(factory: BrokerFactory, args) -> dict:
    service = factory.build_service()
    adapter = service.adapter

    # Resting orders can only become fillable when the market moves, so bring
    # them up to date before reporting anything.
    filled = service.sync()

    account = adapter.get_account()
    quotes = adapter.get_quotes([p.symbol for p in account.positions])

    return {
        "broker": adapter.describe(),
        "safety": service.describe()["safety"],
        "account": {
            "cash": account.cash,
            "equity": account.equity,
            "realized_pnl": account.realized_pnl,
            "unrealized_pnl": account.unrealized_pnl,
            "gross_exposure": account.gross_exposure,
            "net_exposure": account.net_exposure,
            "leverage": account.leverage,
            "timestamp": account.timestamp.isoformat() if account.timestamp else None,
        },
        "positions": [
            {**p.to_dict(), "quote": _quote_dict(quotes[p.symbol]) if p.symbol in quotes else None}
            for p in account.positions
        ],
        "universe": [i.symbol for i in adapter.get_instruments()],
        "just_filled": [o.to_dict() for o in filled],
    }


def cmd_orders(factory: BrokerFactory, args) -> dict:
    store = factory.build_store()
    offset = max(args.page - 1, 0) * args.page_size
    orders, total = store.page_orders(
        offset=offset,
        limit=args.page_size,
        symbol=args.symbol,
        status=args.status,
        strategy=args.strategy,
    )
    return {
        "rows": [o.to_dict() for o in orders],
        "pagination": _pagination(args.page, args.page_size, total),
        "filters": {
            "symbols": store.distinct_values("symbol"),
            "statuses": store.distinct_values("status"),
            "strategies": store.distinct_values("strategy"),
        },
    }


def cmd_fills(factory: BrokerFactory, args) -> dict:
    store = factory.build_store()
    offset = max(args.page - 1, 0) * args.page_size
    fills, total = store.page_fills(offset=offset, limit=args.page_size)
    return {
        "rows": [
            {
                "order_id": f.order_id,
                "symbol": f.symbol,
                "side": f.side.value,
                "quantity": f.quantity,
                "price": f.price,
                "commission": f.commission,
                "slippage": f.slippage,
                "notional": f.notional,
                "timestamp": f.timestamp.isoformat(),
            }
            for f in fills
        ],
        "pagination": _pagination(args.page, args.page_size, total),
    }


def cmd_events(factory: BrokerFactory, args) -> dict:
    store = factory.build_store()
    offset = max(args.page - 1, 0) * args.page_size
    events, total = store.page_events(offset=offset, limit=args.page_size)
    return {"rows": events, "pagination": _pagination(args.page, args.page_size, total)}


def cmd_quote(factory: BrokerFactory, args) -> dict:
    return _quote_dict(factory.build_adapter().get_quote(args.symbol))


def cmd_quotes(factory: BrokerFactory, args) -> dict:
    symbols = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()]
    adapter = factory.build_adapter()
    if not symbols:
        symbols = [i.symbol for i in adapter.get_instruments()]
    quotes = adapter.get_quotes(symbols)
    return {"quotes": {s: _quote_dict(q) for s, q in quotes.items()}}


def cmd_place(factory: BrokerFactory, args) -> dict:
    service = factory.build_service()
    order = service.place_order(
        symbol=args.symbol.upper(),
        side=Side(args.side.upper()),
        quantity=float(args.quantity),
        order_type=OrderType(args.order_type.upper()),
        limit_price=args.limit_price,
        stop_price=args.stop_price,
        product=ProductType(args.product.upper()),
        strategy=args.strategy or "manual",
        force=args.force,
    )
    return {"order": order.to_dict(), "safety": service.describe()["safety"]}


def cmd_cancel(factory: BrokerFactory, args) -> dict:
    return {"order": factory.build_service().cancel_order(args.order_id).to_dict()}


def cmd_poll(factory: BrokerFactory, args) -> dict:
    changed = factory.build_service().sync()
    return {"changed": [o.to_dict() for o in changed], "count": len(changed)}


def cmd_reset(factory: BrokerFactory, args) -> dict:
    adapter = factory.build_adapter()
    if not hasattr(adapter, "reset"):
        raise BrokerError(f"{adapter.name} does not support reset")
    adapter.reset()
    return {"ok": True, "message": "paper account reset to starting cash"}


def _pagination(page: int, page_size: int, total: int) -> dict:
    pages = max((total + page_size - 1) // page_size, 1)
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
        "has_prev": page > 1,
        "has_next": page < pages,
    }


def cmd_brokers(factory: BrokerFactory, args) -> dict:
    """Every configured broker, so the console can offer a switcher.

    Reports what each one *is* without connecting to it — building an adapter
    is cheap, but authenticating is not, and listing brokers must not require
    a live token for every one of them.
    """
    rows = []
    for name in factory.available():
        settings = factory._config.get("brokers", {}).get(name, {})
        kind = settings.get("adapter", name)
        rows.append(
            {
                "name": name,
                "adapter": kind,
                "simulated": kind in ("paper", "mock"),
                "exchange": settings.get("exchange", ""),
                "universe_size": len(settings.get("universe", []) or []),
                "active": name == factory.active_broker,
            }
        )
    return {"brokers": [r["name"] for r in rows], "active": factory.active_broker, "detail": rows}


COMMANDS = {
    "brokers": cmd_brokers,
    "state": cmd_state,
    "orders": cmd_orders,
    "fills": cmd_fills,
    "events": cmd_events,
    "quote": cmd_quote,
    "quotes": cmd_quotes,
    "place": cmd_place,
    "cancel": cmd_cancel,
    "poll": cmd_poll,
    "reset": cmd_reset,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--symbol")
    parser.add_argument("--symbols")
    parser.add_argument("--status")
    parser.add_argument("--strategy")
    parser.add_argument("--side", default="BUY")
    parser.add_argument("--quantity", type=float, default=1.0)
    parser.add_argument("--order-type", default="MARKET")
    parser.add_argument("--limit-price", type=float)
    parser.add_argument("--stop-price", type=float)
    parser.add_argument("--product", default="INTRADAY")
    parser.add_argument("--order-id")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument(
        "--broker",
        help="override the active broker from config (e.g. paper, paper_india)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        factory = load_factory(Path(args.config))
        if args.broker:
            # Selecting the broker per call lets one config serve several
            # accounts — a US paper book and an NSE one — without editing it.
            factory._config = {**factory._config, "active": args.broker}
        # Page size is bounded: an unbounded request would let the console
        # pull the entire order history into one response.
        args.page_size = max(1, min(args.page_size, 500))
        args.page = max(1, args.page)
        print(json.dumps(COMMANDS[args.command](factory, args), default=str))
        return 0
    except BrokerError as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stdout)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stdout)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
