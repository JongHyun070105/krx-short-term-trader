from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from krx_trader.execution.state_machine import OrderStatus, transition
from krx_trader.models import Signal


class LedgerError(RuntimeError):
    pass


class SQLiteStore:
    def __init__(self, path: Path = Path("runtime/trader.sqlite3")) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self._initialize()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL, strategy_id TEXT NOT NULL, decision TEXT NOT NULL,
                    reason_codes TEXT NOT NULL, reference_price REAL, stop_price REAL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL UNIQUE REFERENCES decisions(decision_id),
                    symbol TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity > 0),
                    status TEXT NOT NULL, filled_quantity INTEGER NOT NULL DEFAULT 0,
                    strategy_id TEXT NOT NULL, created_at TEXT NOT NULL, limit_price REAL,
                    reserved_notional_krw REAL
                );
                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(order_id),
                    quantity INTEGER NOT NULL CHECK(quantity > 0), price REAL NOT NULL CHECK(price > 0),
                    fee_krw REAL NOT NULL DEFAULT 0, tax_krw REAL NOT NULL DEFAULT 0, timestamp TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT NOT NULL, strategy_id TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity > 0),
                    average_price REAL NOT NULL CHECK(average_price >= 0), PRIMARY KEY(symbol, strategy_id)
                );
                CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL, entity_id TEXT NOT NULL, payload TEXT NOT NULL
                );
                """
            )

    def initialize_cash(self, amount_krw: float = 100_000) -> None:
        if not 0 < amount_krw <= 100_000:
            raise ValueError("initial bot cash must be between 1 and 100000 KRW")
        with self._db() as db:
            current = db.execute("SELECT value FROM bot_state WHERE key = 'cash_krw'").fetchone()
            if current is None:
                db.execute("INSERT INTO bot_state(key, value) VALUES ('cash_krw', ?)", (str(amount_krw),))
                db.execute("INSERT INTO bot_state(key, value) VALUES ('realized_pnl_krw', '0')")
            elif float(current["value"]) != float(amount_krw):
                raise LedgerError("bot cash is already initialized; explicit reconciliation is required")

    def bot_cash_krw(self) -> float:
        with self._db() as db:
            row = db.execute("SELECT value FROM bot_state WHERE key = 'cash_krw'").fetchone()
            if row is None:
                raise LedgerError("bot cash has not been initialized")
            return float(row["value"])

    def available_cash_krw(self) -> float:
        cash = self.bot_cash_krw() - self.pending_buy_notional_krw()
        if cash < 0:
            raise LedgerError("pending buy reservations exceed bot cash")
        return cash

    def bot_nav_krw(self, mark_prices: dict[str, float]) -> float:
        positions = self.bot_positions()
        missing = sorted({item["symbol"] for item in positions} - mark_prices.keys())
        if missing:
            raise LedgerError("fresh marks are required for every bot-owned position")
        marked = 0.0
        for item in positions:
            price = mark_prices[item["symbol"]]
            if price <= 0:
                raise LedgerError("non-positive mark blocks NAV calculation")
            marked += int(item["quantity"]) * price
        return self.bot_cash_krw() + marked

    def bot_exposure_krw(self, mark_prices: dict[str, float]) -> float:
        nav_assets = self.bot_nav_krw(mark_prices) - self.bot_cash_krw()
        return nav_assets + self.pending_buy_notional_krw()

    def realized_pnl_krw(self) -> float:
        with self._db() as db:
            row = db.execute("SELECT value FROM bot_state WHERE key = 'realized_pnl_krw'").fetchone()
            if row is None:
                raise LedgerError("bot cash has not been initialized")
            return float(row["value"])

    def pending_buy_notional_krw(self) -> float:
        pending = (
            OrderStatus.CREATED.value,
            OrderStatus.RISK_APPROVED.value,
            OrderStatus.SUBMITTING.value,
            OrderStatus.ACCEPTED.value,
            OrderStatus.OPEN.value,
            OrderStatus.PARTIALLY_FILLED.value,
            OrderStatus.CANCEL_PENDING.value,
            OrderStatus.UNKNOWN.value,
        )
        marks = ",".join("?" for _ in pending)
        with self._db() as db:
            row = db.execute(
                f"SELECT COALESCE(SUM((quantity - filled_quantity) * reserved_notional_krw / quantity), 0) AS reserved "
                f"FROM orders WHERE side = 'BUY' AND status IN ({marks})",
                pending,
            ).fetchone()
            return float(row["reserved"])

    def record_decision(self, decision_id: str, run_id: str, signal: Signal) -> bool:
        payload = {
            "timestamp": signal.timestamp.isoformat(),
            "symbol": signal.symbol,
            "strategy_id": signal.strategy_id,
            "decision": signal.decision.value,
            "reason_codes": list(signal.reason_codes),
            "reference_price": signal.reference_price,
            "stop_price": signal.stop_price,
            "max_holding_bars": signal.max_holding_bars,
        }
        with self._db() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (decision_id, run_id, signal.timestamp.isoformat(), signal.symbol, signal.strategy_id,
                 signal.decision.value, json.dumps(signal.reason_codes), signal.reference_price, signal.stop_price,
                 json.dumps(payload, sort_keys=True)),
            )
            return cursor.rowcount == 1

    def create_order(
        self, order_id: str, decision_id: str, symbol: str, side: str, quantity: int, strategy_id: str, timestamp: str,
        limit_price: float | None = None, reserved_notional_krw: float | None = None,
    ) -> bool:
        if side not in {"BUY", "SELL"} or quantity <= 0:
            raise ValueError("invalid order side or quantity")
        if limit_price is None or limit_price <= 0:
            raise ValueError("a positive limit price is required for bounded orders")
        if side == "BUY" and reserved_notional_krw is None:
            raise ValueError("buy orders require an explicit reservation including costs")
        reserved = reserved_notional_krw if reserved_notional_krw is not None else limit_price * quantity
        if reserved < limit_price * quantity:
            raise ValueError("reserved notional must include at least the order notional")
        with self._db() as db:
            try:
                db.execute(
                    "INSERT INTO orders(order_id, decision_id, symbol, side, quantity, status, strategy_id, created_at, limit_price, reserved_notional_krw) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (order_id, decision_id, symbol, side, quantity, OrderStatus.CREATED.value, strategy_id, timestamp,
                     limit_price, reserved),
                )
                return True
            except sqlite3.IntegrityError:
                duplicate = db.execute(
                    "SELECT 1 FROM orders WHERE order_id = ? OR decision_id = ?",
                    (order_id, decision_id),
                ).fetchone()
                if duplicate is not None:
                    return False
                raise LedgerError("order violates ledger constraints") from None

    def transition_order(self, order_id: str, target: OrderStatus) -> OrderStatus:
        with self._db() as db:
            row = db.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            if row is None:
                raise LedgerError("order not found")
            current = OrderStatus(row["status"])
            next_status = transition(current, target)
            db.execute("UPDATE orders SET status = ? WHERE order_id = ?", (next_status.value, order_id))
            return next_status

    def record_fill(
        self, *, fill_id: str, order_id: str, quantity: int, price: float, fee_krw: float, tax_krw: float, timestamp: str
    ) -> bool:
        if quantity <= 0 or price <= 0 or fee_krw < 0 or tax_krw < 0:
            raise ValueError("invalid fill")
        with self._db() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                duplicate = db.execute("SELECT 1 FROM fills WHERE fill_id = ?", (fill_id,)).fetchone()
                if duplicate is not None:
                    return False
                order = db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
                if order is None:
                    raise LedgerError("fill references unknown order")
                filled = int(order["filled_quantity"])
                if filled + quantity > int(order["quantity"]):
                    raise LedgerError("fill exceeds requested quantity")
                db.execute(
                    "INSERT INTO fills(fill_id, order_id, quantity, price, fee_krw, tax_krw, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (fill_id, order_id, quantity, price, fee_krw, tax_krw, timestamp),
                )
                cash_row = db.execute("SELECT value FROM bot_state WHERE key = 'cash_krw'").fetchone()
                if cash_row is None:
                    raise LedgerError("bot cash has not been initialized")
                cash = float(cash_row["value"])
                realized_row = db.execute("SELECT value FROM bot_state WHERE key = 'realized_pnl_krw'").fetchone()
                realized = float(realized_row["value"]) if realized_row else 0.0
                position = db.execute(
                    "SELECT * FROM positions WHERE symbol = ? AND strategy_id = ?",
                    (order["symbol"], order["strategy_id"]),
                ).fetchone()
                if order["side"] == "BUY":
                    spend = price * quantity + fee_krw
                    if spend > cash:
                        raise LedgerError("fill exceeds bot-owned available cash")
                    old_qty = int(position["quantity"]) if position else 0
                    old_cost = float(position["average_price"]) * old_qty if position else 0.0
                    new_qty = old_qty + quantity
                    average = (old_cost + price * quantity + fee_krw) / new_qty
                    db.execute(
                        "INSERT INTO positions(symbol, strategy_id, quantity, average_price) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(symbol, strategy_id) DO UPDATE SET quantity=excluded.quantity, average_price=excluded.average_price",
                        (order["symbol"], order["strategy_id"], new_qty, average),
                    )
                    cash -= spend
                else:
                    if position is None or int(position["quantity"]) < quantity:
                        raise LedgerError("sell fill exceeds bot-owned position")
                    proceeds = price * quantity - fee_krw - tax_krw
                    realized += proceeds - float(position["average_price"]) * quantity
                    cash += proceeds
                    remaining = int(position["quantity"]) - quantity
                    if remaining:
                        db.execute(
                            "UPDATE positions SET quantity = ? WHERE symbol = ? AND strategy_id = ?",
                            (remaining, order["symbol"], order["strategy_id"]),
                        )
                    else:
                        db.execute(
                            "DELETE FROM positions WHERE symbol = ? AND strategy_id = ?",
                            (order["symbol"], order["strategy_id"]),
                        )
                db.execute(
                    "INSERT INTO bot_state(key, value) VALUES ('cash_krw', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(cash),),
                )
                db.execute(
                    "INSERT INTO bot_state(key, value) VALUES ('realized_pnl_krw', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(realized),),
                )
                new_filled = filled + quantity
                target = OrderStatus.FILLED if new_filled == int(order["quantity"]) else OrderStatus.PARTIALLY_FILLED
                current = OrderStatus(order["status"])
                if target != current:
                    transition(current, target)
                db.execute(
                    "UPDATE orders SET filled_quantity = ?, status = ? WHERE order_id = ?",
                    (new_filled, target.value, order_id),
                )
                return True
            except sqlite3.IntegrityError:
                raise LedgerError("fill violates ledger constraints") from None

    def record_event(self, event_id: str, run_id: str, timestamp: str, event_type: str, entity_id: str, payload: dict[str, Any]) -> bool:
        with self._db() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO audit_events VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, run_id, timestamp, event_type, entity_id, json.dumps(payload, sort_keys=True)),
            )
            return cursor.rowcount == 1

    def bot_positions(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT symbol, strategy_id, quantity, average_price FROM positions ORDER BY symbol").fetchall()
            return [dict(row) for row in rows]

    def order_status(self, order_id: str) -> OrderStatus | None:
        with self._db() as db:
            row = db.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            return OrderStatus(row["status"]) if row else None
