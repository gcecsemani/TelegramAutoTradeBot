"""
execution/store.py
==================
SQLite record of users, broker accounts, risk rules and order intents.

An intent row is written before the broker is called. The idempotency key is
unique, so a retry or a second process cannot open a second order for the
same action. state/portal.db is local runtime data and is not committed.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator, List, Optional

import config
from core import clock

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    broker TEXT NOT NULL,
    label TEXT NOT NULL,
    client_id TEXT NOT NULL DEFAULT '',
    api_key_enc TEXT NOT NULL DEFAULT '',
    api_secret_enc TEXT NOT NULL DEFAULT '',
    access_token_enc TEXT NOT NULL DEFAULT '',
    token_date TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'paper',
    enabled INTEGER NOT NULL DEFAULT 1,
    product TEXT NOT NULL DEFAULT 'MIS',
    kill_switch INTEGER NOT NULL DEFAULT 0,
    kill_reason TEXT NOT NULL DEFAULT '',
    auto_kill INTEGER NOT NULL DEFAULT 1,
    flatten_on_kill INTEGER NOT NULL DEFAULT 0,
    max_loss_trade REAL,
    max_loss_day REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS symbol_rules (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    lots INTEGER NOT NULL,
    min_lots INTEGER NOT NULL,
    max_lots INTEGER NOT NULL,
    slippage_mode TEXT NOT NULL,
    slippage_value REAL NOT NULL,
    UNIQUE(account_id, symbol)
);
CREATE TABLE IF NOT EXISTS intents (
    id INTEGER PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    account_id INTEGER NOT NULL,
    position_id TEXT NOT NULL,
    action TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    tradingsymbol TEXT NOT NULL,
    lots INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    multiplier INTEGER NOT NULL DEFAULT 1,
    order_type TEXT NOT NULL,
    limit_price REAL NOT NULL DEFAULT 0,
    product TEXT NOT NULL,
    tag TEXT NOT NULL,
    stop_trigger REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    broker_order_id TEXT NOT NULL DEFAULT '',
    avg_price REAL NOT NULL DEFAULT 0,
    filled_qty INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intents_status ON intents(status);
CREATE INDEX IF NOT EXISTS idx_intents_pos ON intents(account_id, position_id, action);
CREATE TABLE IF NOT EXISTS legs (
    account_id INTEGER NOT NULL,
    position_id TEXT NOT NULL,
    lots_open INTEGER NOT NULL,
    quantity_open INTEGER NOT NULL,
    avg_entry REAL NOT NULL,
    multiplier INTEGER NOT NULL,
    tradingsymbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    product TEXT NOT NULL,
    sl_order_id TEXT NOT NULL DEFAULT '',
    sl_trigger REAL NOT NULL DEFAULT 0,
    sl_quantity INTEGER NOT NULL DEFAULT 0,
    sl_tag TEXT NOT NULL DEFAULT '',
    sl_status TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, position_id)
);
CREATE TABLE IF NOT EXISTS pnl (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    position_id TEXT NOT NULL,
    day TEXT NOT NULL,
    pnl REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pnl_day ON pnl(account_id, day);
"""


def db_path() -> str:
    return os.environ.get("PORTAL_DB") or os.path.join(config.STATE_DIR, "portal.db")


def init() -> None:
    path = db_path()
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with connect() as conn:
        conn.executescript(_SCHEMA)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "role" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
        intent_cols = {row[1] for row in conn.execute("PRAGMA table_info(intents)")}
        if "stop_trigger" not in intent_cols:
            conn.execute("ALTER TABLE intents ADD COLUMN stop_trigger REAL NOT NULL DEFAULT 0")
        leg_cols = {row[1] for row in conn.execute("PRAGMA table_info(legs)")}
        for name, typedef in (
            ("sl_order_id", "TEXT NOT NULL DEFAULT ''"),
            ("sl_trigger", "REAL NOT NULL DEFAULT 0"),
            ("sl_quantity", "INTEGER NOT NULL DEFAULT 0"),
            ("sl_tag", "TEXT NOT NULL DEFAULT ''"),
            ("sl_status", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in leg_cols:
                conn.execute(f"ALTER TABLE legs ADD COLUMN {name} {typedef}")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path(), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def _one(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> Optional[dict]:
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


def _all(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> List[dict]:
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


def count_users() -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def create_user(email: str, password_hash: str, role: str = "user") -> int:
    now = clock.now().isoformat()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (email.strip().lower(), password_hash, role if role == "admin" else "user", now),
        )
        return int(cur.lastrowid)


def ensure_admin(email: str, password_hash: str, rename_from: str = "") -> None:
    """One operator sees every account. A password is written only when that login is created."""
    init()
    email = email.strip().lower()
    current = user_by_email(email)
    if current:
        if current.get("role") != "admin":
            with connect() as conn:
                conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (current["id"],))
        return
    renamed = user_by_email(rename_from) if rename_from else None
    if renamed:
        with connect() as conn:
            conn.execute(
                "UPDATE users SET email = ?, password_hash = ?, role = 'admin' WHERE id = ?",
                (email, password_hash, renamed["id"]),
            )
        return
    create_user(email, password_hash, role="admin")


def user_by_email(email: str) -> Optional[dict]:
    with connect() as conn:
        return _one(conn, "SELECT * FROM users WHERE email = ?", (email.strip().lower(),))


def user_by_id(user_id: int) -> Optional[dict]:
    with connect() as conn:
        return _one(conn, "SELECT * FROM users WHERE id = ?", (user_id,))


def create_account(user_id: int, broker: str, label: str, client_id: str,
                   api_key_enc: str, api_secret_enc: str) -> int:
    now = clock.now().isoformat()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """INSERT INTO accounts
               (user_id, broker, label, client_id, api_key_enc, api_secret_enc, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, broker, label.strip(), client_id.strip(), api_key_enc, api_secret_enc, now),
        )
        account_id = int(cur.lastrowid)
        conn.execute(
            """INSERT INTO symbol_rules
               (account_id, symbol, lots, min_lots, max_lots, slippage_mode, slippage_value)
               VALUES (?, '*', 2, 1, 2, 'PERCENT', 2.5)""",
            (account_id,),
        )
        conn.execute("COMMIT")
        return account_id


def update_account(account_id: int, user_id: Optional[int], fields: dict) -> None:
    allowed = {
        "label", "client_id", "api_key_enc", "api_secret_enc", "mode", "enabled",
        "product", "kill_switch", "kill_reason", "auto_kill", "flatten_on_kill",
        "max_loss_trade", "max_loss_day", "broker",
    }
    sets, args = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key} = ?")
            args.append(value)
    if not sets:
        return
    args.append(account_id)
    sql = f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?"
    if user_id is not None:
        sql += " AND user_id = ?"
        args.append(user_id)
    with connect() as conn:
        conn.execute(sql, args)


def set_token(account_id: int, token_enc: str, client_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE accounts SET access_token_enc = ?, token_date = ?, client_id = ? WHERE id = ?",
            (token_enc, clock.today().isoformat(), client_id, account_id),
        )


def set_kill(account_id: int, on: bool, reason: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE accounts SET kill_switch = ?, kill_reason = ? WHERE id = ?",
            (1 if on else 0, reason if on else "", account_id),
        )


def get_account(account_id: int, user_id: Optional[int] = None) -> Optional[dict]:
    with connect() as conn:
        if user_id is None:
            return _one(conn, "SELECT * FROM accounts WHERE id = ?", (account_id,))
        return _one(conn, "SELECT * FROM accounts WHERE id = ? AND user_id = ?", (account_id, user_id))


def list_accounts(user_id: Optional[int] = None) -> List[dict]:
    sql = """SELECT accounts.*, users.email AS owner_email
             FROM accounts JOIN users ON users.id = accounts.user_id"""
    args: tuple = ()
    if user_id is not None:
        sql += " WHERE accounts.user_id = ?"
        args = (user_id,)
    sql += " ORDER BY accounts.id"
    with connect() as conn:
        return _all(conn, sql, args)


def copy_accounts() -> List[dict]:
    """Enabled accounts the signal book should copy into."""
    with connect() as conn:
        return _all(conn, "SELECT * FROM accounts WHERE enabled = 1 ORDER BY id")


def upsert_rule(account_id: int, symbol: str, lots: int, min_lots: int, max_lots: int,
                slippage_mode: str, slippage_value: float) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO symbol_rules
               (account_id, symbol, lots, min_lots, max_lots, slippage_mode, slippage_value)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_id, symbol) DO UPDATE SET
                 lots = excluded.lots, min_lots = excluded.min_lots, max_lots = excluded.max_lots,
                 slippage_mode = excluded.slippage_mode, slippage_value = excluded.slippage_value""",
            (account_id, symbol, lots, min_lots, max_lots, slippage_mode, slippage_value),
        )


def delete_account(account_id: int, user_id: Optional[int] = None) -> str:
    """Remove an account and its rules, orders, and legs. Refuses while lots are open
    or an order is still unconfirmed. Returns a short reason, or "" when removed."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if user_id is None:
            account = _one(conn, "SELECT id, label FROM accounts WHERE id = ?", (account_id,))
        else:
            account = _one(conn, "SELECT id, label FROM accounts WHERE id = ? AND user_id = ?",
                           (account_id, user_id))
        if account is None:
            conn.execute("ROLLBACK")
            return "missing"
        open_n = conn.execute(
            "SELECT COUNT(*) FROM legs WHERE account_id = ? AND lots_open > 0", (account_id,)
        ).fetchone()[0]
        if open_n:
            conn.execute("ROLLBACK")
            return "open"
        busy = conn.execute(
            """SELECT COUNT(*) FROM intents WHERE account_id = ?
               AND status IN ('NEW', 'SENDING', 'UNKNOWN', 'WORKING', 'AUTH', 'WAITING')""",
            (account_id,),
        ).fetchone()[0]
        if busy:
            conn.execute("ROLLBACK")
            return "busy"
        for table in ("symbol_rules", "legs", "pnl", "intents"):
            conn.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.execute("COMMIT")
    return ""


def delete_rule(account_id: int, symbol: str) -> None:
    if symbol == "*":
        return
    with connect() as conn:
        conn.execute("DELETE FROM symbol_rules WHERE account_id = ? AND symbol = ?", (account_id, symbol))


def rules_for(account_id: int) -> List[dict]:
    with connect() as conn:
        return _all(conn, "SELECT * FROM symbol_rules WHERE account_id = ? ORDER BY symbol != '*', symbol",
                    (account_id,))


def effective_rule(account_id: int, symbol: str) -> dict:
    rows = {row["symbol"]: row for row in rules_for(account_id)}
    base = rows.get("*") or {
        "lots": 1, "min_lots": 1, "max_lots": 1,
        "slippage_mode": "PERCENT", "slippage_value": 2.5,
    }
    over = rows.get((symbol or "").upper())
    if not over:
        return dict(base)
    merged = dict(base)
    for key in ("lots", "min_lots", "max_lots", "slippage_mode", "slippage_value"):
        merged[key] = over[key]
    return merged


def claim_intent(row: dict) -> bool:
    """Insert the intent. False means this action was already claimed."""
    now = clock.now().isoformat()
    with connect() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO intents (
                       idempotency_key, account_id, position_id, action, symbol, exchange,
                       tradingsymbol, lots, quantity, multiplier, order_type, limit_price,
                       product, tag, stop_trigger, status, reason, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["idempotency_key"], row["account_id"], row["position_id"], row["action"],
                    row["symbol"], row["exchange"], row["tradingsymbol"], row["lots"], row["quantity"],
                    row.get("multiplier") or 1, row["order_type"], row.get("limit_price") or 0,
                    row["product"], row["tag"], row.get("stop_trigger") or 0, row["status"],
                    row.get("reason") or "", now, now,
                ),
            )
            conn.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            return False


def intent_by_id(intent_id: int) -> Optional[dict]:
    with connect() as conn:
        return _one(conn, "SELECT * FROM intents WHERE id = ?", (intent_id,))


def intent_by_tag(tag: str) -> Optional[dict]:
    if not tag:
        return None
    with connect() as conn:
        return _one(conn, "SELECT * FROM intents WHERE tag = ? ORDER BY id DESC LIMIT 1", (tag,))


def intent_by_key(key: str) -> Optional[dict]:
    with connect() as conn:
        return _one(conn, "SELECT * FROM intents WHERE idempotency_key = ?", (key,))


def buy_intent(account_id: int, position_id: str) -> Optional[dict]:
    with connect() as conn:
        return _one(
            conn,
            """SELECT * FROM intents
               WHERE account_id = ? AND position_id = ? AND action = 'BUY'
               ORDER BY id DESC LIMIT 1""",
            (account_id, position_id),
        )


def update_intent(intent_id: int, **fields) -> None:
    allowed = {
        "status", "reason", "broker_order_id", "avg_price", "filled_qty",
        "attempts", "last_error", "lots", "quantity", "limit_price",
    }
    sets = ["updated_at = ?"]
    args: list = [clock.now().isoformat()]
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key} = ?")
            args.append(value)
    args.append(intent_id)
    with connect() as conn:
        conn.execute(f"UPDATE intents SET {', '.join(sets)} WHERE id = ?", args)


def bump_attempt(intent_id: int) -> None:
    now = clock.now().isoformat()
    with connect() as conn:
        conn.execute(
            "UPDATE intents SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (now, intent_id),
        )


def recent_intents(user_id: Optional[int] = None, limit: int = 80) -> List[dict]:
    sql = """SELECT intents.*, accounts.label AS account_label, accounts.broker, accounts.mode,
                    users.email AS owner_email
             FROM intents JOIN accounts ON accounts.id = intents.account_id
             JOIN users ON users.id = accounts.user_id"""
    args: list = []
    if user_id is not None:
        sql += " WHERE accounts.user_id = ?"
        args.append(user_id)
    sql += " ORDER BY intents.id DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return _all(conn, sql, args)


def inflight() -> List[dict]:
    with connect() as conn:
        return _all(
            conn,
            """SELECT * FROM intents
               WHERE status IN ('NEW', 'SENDING', 'UNKNOWN', 'WORKING', 'AUTH', 'WAITING')
               ORDER BY id""",
        )


def set_leg_stop(account_id: int, position_id: str, order_id: str, trigger: float,
                 quantity: int, tag: str, status: str) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE legs SET sl_order_id = ?, sl_trigger = ?, sl_quantity = ?, sl_tag = ?,
               sl_status = ?, updated_at = ? WHERE account_id = ? AND position_id = ?""",
            (order_id, trigger, quantity, tag, status, clock.now().isoformat(), account_id, position_id),
        )


def get_leg(account_id: int, position_id: str) -> Optional[dict]:
    with connect() as conn:
        return _one(conn, "SELECT * FROM legs WHERE account_id = ? AND position_id = ?",
                    (account_id, position_id))


def open_legs(account_id: Optional[int] = None) -> List[dict]:
    with connect() as conn:
        if account_id is None:
            return _all(conn, "SELECT * FROM legs WHERE lots_open > 0")
        return _all(conn, "SELECT * FROM legs WHERE account_id = ? AND lots_open > 0", (account_id,))


def apply_fill(account_id: int, position_id: str, *, side: str, lots: int, quantity: int,
               price: float, multiplier: int, tradingsymbol: str, exchange: str, product: str) -> float:
    """Update the open leg and return the realised P&L of a sell (0 for a buy)."""
    now = clock.now().isoformat()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        leg = _one(conn, "SELECT * FROM legs WHERE account_id = ? AND position_id = ?",
                   (account_id, position_id))
        pnl = 0.0
        if side == "BUY":
            if leg:
                total_qty = int(leg["quantity_open"]) + quantity
                avg = ((float(leg["avg_entry"]) * int(leg["quantity_open"])) + price * quantity) / total_qty
                conn.execute(
                    """UPDATE legs SET lots_open = lots_open + ?, quantity_open = ?, avg_entry = ?,
                       updated_at = ? WHERE account_id = ? AND position_id = ?""",
                    (lots, total_qty, avg, now, account_id, position_id),
                )
            else:
                conn.execute(
                    """INSERT INTO legs (
                           account_id, position_id, lots_open, quantity_open, avg_entry, multiplier,
                           tradingsymbol, exchange, product, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (account_id, position_id, lots, quantity, price, multiplier,
                     tradingsymbol, exchange, product, now),
                )
        elif leg and int(leg["lots_open"]) > 0:
            sell_lots = min(lots, int(leg["lots_open"]))
            sell_qty = min(quantity, int(leg["quantity_open"])) or sell_lots * max(1, int(leg["multiplier"]))
            pnl = round((price - float(leg["avg_entry"])) * sell_qty, 2)
            left_lots = int(leg["lots_open"]) - sell_lots
            left_qty = max(0, int(leg["quantity_open"]) - sell_qty)
            conn.execute(
                """UPDATE legs SET lots_open = ?, quantity_open = ?, updated_at = ?
                   WHERE account_id = ? AND position_id = ?""",
                (left_lots, left_qty, now, account_id, position_id),
            )
            conn.execute(
                "INSERT INTO pnl (account_id, position_id, day, pnl, created_at) VALUES (?, ?, ?, ?, ?)",
                (account_id, position_id, clock.today().isoformat(), pnl, now),
            )
        conn.execute("COMMIT")
        return pnl


def day_realized(account_id: int, day: Optional[str] = None) -> float:
    day = day or clock.today().isoformat()
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM pnl WHERE account_id = ? AND day = ?",
            (account_id, day),
        ).fetchone()
        return float(row[0] or 0)


def active_sell(account_id: int, position_id: str) -> Optional[dict]:
    with connect() as conn:
        return _one(
            conn,
            """SELECT * FROM intents
               WHERE account_id = ? AND position_id = ? AND action = 'SELL'
                 AND status IN ('NEW', 'SENDING', 'UNKNOWN', 'WORKING', 'WAITING', 'AUTH')
               ORDER BY id DESC LIMIT 1""",
            (account_id, position_id),
        )
