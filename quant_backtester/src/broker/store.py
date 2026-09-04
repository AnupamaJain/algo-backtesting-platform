"""Durable state for the paper broker.

A trading process that forgets its open orders when it restarts is worse than
useless — it can double up on a position it already holds. State therefore
lives in SQLite, written before any call returns, so a crash mid-session
loses nothing and the console reads exactly what the engine believes.

SQLite (not JSON) because orders and fills are appended continuously and read
concurrently by the console; a rewritten JSON file would race.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .models import (
    Fill,
    OrderStatus,
    OrderType,
    ProductType,
    Side,
    UnifiedOrder,
    UnifiedPosition,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id         TEXT PRIMARY KEY,
    broker_order_id  TEXT,
    broker           TEXT NOT NULL,
    strategy         TEXT,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,
    quantity         REAL NOT NULL,
    order_type       TEXT NOT NULL,
    product          TEXT NOT NULL,
    limit_price      REAL,
    stop_price       REAL,
    status           TEXT NOT NULL,
    filled_quantity  REAL NOT NULL DEFAULT 0,
    average_price    REAL,
    status_message   TEXT,
    dry_run          INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT,
    updated_at       TEXT,
    tags             TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);

CREATE TABLE IF NOT EXISTS fills (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    side       TEXT NOT NULL,
    quantity   REAL NOT NULL,
    price      REAL NOT NULL,
    commission REAL NOT NULL DEFAULT 0,
    slippage   REAL NOT NULL DEFAULT 0,
    timestamp  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);

CREATE TABLE IF NOT EXISTS account (
    broker        TEXT PRIMARY KEY,
    cash          REAL NOT NULL,
    realized_pnl  REAL NOT NULL DEFAULT 0,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,
    severity  TEXT NOT NULL DEFAULT 'info',
    message   TEXT NOT NULL,
    detail    TEXT,
    timestamp TEXT NOT NULL
);
"""


class BrokerStore:
    """SQLite-backed persistence for orders, fills, cash and audit events."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- orders ----------------------------------------------------------

    def save_order(self, order: UnifiedOrder) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO orders (order_id, broker_order_id, broker, strategy, symbol,
                       side, quantity, order_type, product, limit_price, stop_price,
                       status, filled_quantity, average_price, status_message, dry_run,
                       created_at, updated_at, tags)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(order_id) DO UPDATE SET
                       broker_order_id=excluded.broker_order_id,
                       status=excluded.status,
                       filled_quantity=excluded.filled_quantity,
                       average_price=excluded.average_price,
                       status_message=excluded.status_message,
                       updated_at=excluded.updated_at""",
                (
                    order.order_id,
                    order.broker_order_id,
                    order.broker,
                    order.strategy,
                    order.symbol,
                    order.side.value,
                    order.quantity,
                    order.order_type.value,
                    order.product.value,
                    order.limit_price,
                    order.stop_price,
                    order.status.value,
                    order.filled_quantity,
                    order.average_price,
                    order.status_message,
                    int(order.dry_run),
                    (order.created_at or datetime.now()).isoformat(),
                    (order.updated_at or datetime.now()).isoformat(),
                    json.dumps(order.tags),
                ),
            )

    def get_order(self, order_id: str) -> UnifiedOrder | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
        return _row_to_order(row) if row else None

    def list_orders(self, limit: int = 1000) -> list[UnifiedOrder]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_order(r) for r in rows]

    def page_orders(
        self,
        *,
        offset: int = 0,
        limit: int = 50,
        symbol: str | None = None,
        status: str | None = None,
        strategy: str | None = None,
    ) -> tuple[list[UnifiedOrder], int]:
        """One page of orders plus the TOTAL matching count.

        The total is what makes pagination honest: without it the UI cannot
        tell "50 orders" from "50 of 4,000" and the operator has no idea how
        much history they are not looking at.
        """
        clauses, params = [], []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if strategy:
            clauses.append("strategy = ?")
            params.append(strategy)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS n FROM orders {where}", params
            ).fetchone()["n"]
            rows = conn.execute(
                f"""SELECT * FROM orders {where}
                    ORDER BY datetime(created_at) DESC, rowid DESC
                    LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ).fetchall()
        return [_row_to_order(r) for r in rows], int(total)

    def page_fills(self, *, offset: int = 0, limit: int = 50) -> tuple[list[Fill], int]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM fills").fetchone()["n"]
            rows = conn.execute(
                "SELECT * FROM fills ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        fills = [
            Fill(
                order_id=r["order_id"],
                symbol=r["symbol"],
                side=Side(r["side"]),
                quantity=r["quantity"],
                price=r["price"],
                timestamp=datetime.fromisoformat(r["timestamp"]),
                commission=r["commission"],
                slippage=r["slippage"],
            )
            for r in rows
        ]
        return fills, int(total)

    def page_events(self, *, offset: int = 0, limit: int = 50) -> tuple[list[dict], int]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        events = [
            {
                "kind": r["kind"],
                "severity": r["severity"],
                "message": r["message"],
                "detail": json.loads(r["detail"] or "{}"),
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]
        return events, int(total)

    def distinct_values(self, column: str) -> list[str]:
        """Distinct values for a filterable column, for populating dropdowns."""
        if column not in ("symbol", "status", "strategy", "side"):
            raise ValueError(f"{column} is not filterable")
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT {column} AS v FROM orders WHERE {column} IS NOT NULL "
                f"AND {column} != '' ORDER BY v"
            ).fetchall()
        return [r["v"] for r in rows]

    def list_working_orders(self) -> list[UnifiedOrder]:
        working = [s.value for s in OrderStatus if s.is_working]
        placeholders = ",".join("?" * len(working))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM orders WHERE status IN ({placeholders})", working
            ).fetchall()
        return [_row_to_order(r) for r in rows]

    def recent_fingerprints(self, since: datetime) -> list[tuple[str, str]]:
        """(fingerprint-ish key, order_id) for orders placed since a moment.

        Used by duplicate detection. The key is rebuilt from stored columns
        rather than persisted, so changing the fingerprint definition does not
        require a migration.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE created_at >= ?", (since.isoformat(),)
            ).fetchall()
        return [(_row_to_order(r).fingerprint(), r["order_id"]) for r in rows]

    # -- fills -----------------------------------------------------------

    def save_fill(self, fill: Fill) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO fills (order_id, symbol, side, quantity, price,
                                      commission, slippage, timestamp)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    fill.order_id,
                    fill.symbol,
                    fill.side.value,
                    fill.quantity,
                    fill.price,
                    fill.commission,
                    fill.slippage,
                    fill.timestamp.isoformat(),
                ),
            )

    def list_fills(self, limit: int = 2000) -> list[Fill]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            Fill(
                order_id=r["order_id"],
                symbol=r["symbol"],
                side=Side(r["side"]),
                quantity=r["quantity"],
                price=r["price"],
                timestamp=datetime.fromisoformat(r["timestamp"]),
                commission=r["commission"],
                slippage=r["slippage"],
            )
            for r in rows
        ]

    # -- account ---------------------------------------------------------

    def init_account(self, broker: str, cash: float) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO account (broker, cash, realized_pnl, updated_at)
                   VALUES (?,?,0,?) ON CONFLICT(broker) DO NOTHING""",
                (broker, cash, datetime.now().isoformat()),
            )

    def get_account(self, broker: str) -> tuple[float, float]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cash, realized_pnl FROM account WHERE broker = ?", (broker,)
            ).fetchone()
        return (row["cash"], row["realized_pnl"]) if row else (0.0, 0.0)

    def update_account(self, broker: str, cash: float, realized_pnl: float) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE account SET cash = ?, realized_pnl = ?, updated_at = ?
                   WHERE broker = ?""",
                (cash, realized_pnl, datetime.now().isoformat(), broker),
            )

    # -- events (audit trail) --------------------------------------------

    def log_event(self, kind: str, message: str, *, severity: str = "info", detail: dict | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO events (kind, severity, message, detail, timestamp)
                   VALUES (?,?,?,?,?)""",
                (
                    kind,
                    severity,
                    message,
                    json.dumps(detail or {}),
                    datetime.now().isoformat(),
                ),
            )

    def list_events(self, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "kind": r["kind"],
                "severity": r["severity"],
                "message": r["message"],
                "detail": json.loads(r["detail"] or "{}"),
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]

    # -- derived ---------------------------------------------------------

    def compute_positions(self, broker: str) -> list[UnifiedPosition]:
        """Rebuild positions from the fill history.

        Positions are derived, never stored: the fills are the ledger, so
        replaying them can only ever produce a state consistent with what
        actually executed. Average price uses weighted-average cost, and a
        position that flips direction re-bases its cost at the flip.
        """
        fills = sorted(self.list_fills(limit=100000), key=lambda f: f.timestamp)
        books: dict[str, dict] = {}

        for fill in fills:
            book = books.setdefault(
                fill.symbol, {"qty": 0.0, "avg": 0.0, "realized": 0.0}
            )
            signed = fill.signed_quantity
            current, avg = book["qty"], book["avg"]

            if current == 0 or (current > 0) == (signed > 0):
                # Opening or adding: blend into the weighted-average cost.
                new_qty = current + signed
                if new_qty != 0:
                    book["avg"] = (avg * abs(current) + fill.price * abs(signed)) / abs(new_qty)
                book["qty"] = new_qty
            else:
                # Reducing, closing, or flipping.
                closing = min(abs(signed), abs(current))
                direction = 1 if current > 0 else -1
                book["realized"] += (fill.price - avg) * closing * direction
                new_qty = current + signed
                if (new_qty > 0) != (current > 0) and new_qty != 0:
                    # Flipped through zero: the remainder opens a new position
                    # at the fill price, so cost basis restarts here.
                    book["avg"] = fill.price
                elif new_qty == 0:
                    book["avg"] = 0.0
                book["qty"] = new_qty
            book["realized"] -= fill.commission

        return [
            UnifiedPosition(
                symbol=symbol,
                quantity=book["qty"],
                average_price=book["avg"],
                broker=broker,
                realized_pnl=book["realized"],
            )
            for symbol, book in books.items()
        ]

    def reset(self) -> None:
        """Wipe all state. Used by tests and by an explicit operator reset."""
        with self._connect() as conn:
            for table in ("orders", "fills", "account", "events"):
                conn.execute(f"DELETE FROM {table}")


def _row_to_order(row: sqlite3.Row) -> UnifiedOrder:
    order = UnifiedOrder(
        symbol=row["symbol"],
        side=Side(row["side"]),
        quantity=row["quantity"],
        order_type=OrderType(row["order_type"]),
        product=ProductType(row["product"]),
        limit_price=row["limit_price"],
        stop_price=row["stop_price"],
        order_id=row["order_id"],
        broker_order_id=row["broker_order_id"],
        broker=row["broker"],
        strategy=row["strategy"] or "",
        status=OrderStatus(row["status"]),
        filled_quantity=row["filled_quantity"],
        average_price=row["average_price"],
        status_message=row["status_message"] or "",
        dry_run=bool(row["dry_run"]),
        tags=json.loads(row["tags"] or "{}"),
    )
    order.created_at = datetime.fromisoformat(row["created_at"]) if row["created_at"] else None
    order.updated_at = datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None
    return order
