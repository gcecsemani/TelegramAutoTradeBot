"""
execution/store.py
==================
SQLite record of users, broker accounts, admin copy rules and order intents.

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
    telegram_chat_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    broker TEXT NOT NULL,
    label TEXT NOT NULL,
    client_id TEXT NOT NULL DEFAULT '',
    mobile TEXT NOT NULL DEFAULT '',
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
    max_loss_day_pct REAL,
    paper_balance REAL NOT NULL DEFAULT 100000,
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
CREATE TABLE IF NOT EXISTS margin_rules (
    symbol TEXT PRIMARY KEY,
    rupees_per_lot REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lot_bands (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    min_balance REAL NOT NULL,
    max_balance REAL NOT NULL,
    lots INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lot_bands_symbol ON lot_bands(symbol, min_balance);
CREATE TABLE IF NOT EXISTS slippage_rules (
    symbol TEXT PRIMARY KEY,
    slippage_mode TEXT NOT NULL,
    slippage_value REAL NOT NULL
);
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
        if "telegram_chat_id" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN telegram_chat_id TEXT NOT NULL DEFAULT ''")
        account_cols = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
        if "mobile" not in account_cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN mobile TEXT NOT NULL DEFAULT ''")
        if "paper_balance" not in account_cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN paper_balance REAL NOT NULL DEFAULT 100000")
        if "max_loss_day_pct" not in account_cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN max_loss_day_pct REAL")
        _seed_copy_rules(conn)
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


def _symbol_key(symbol: str) -> str:
    text = (symbol or "").strip().upper()
    return text or "*"


# The old one-amount-per-lot rows. An untouched copy of these becomes the
# balance ranges below. A customised amount is turned into ranges that keep
# the same lot count, so a portal edit is not thrown away.
_OLD_MARGIN = (
    ("NIFTY", 50000.0),
    ("SENSEX", 50000.0),
    ("BANKNIFTY", 50000.0),
    ("*", 100000.0),
)
# NIFTY, SENSEX and BANKNIFTY: ₹35,000–₹50,000 is 1 lot, then the next
# ranges step up. `*` is every other symbol. Both ends of a range count.
_INDEX_BANDS = (
    (35000, 50000, 1),
    (50001, 100000, 2),
    (100001, 150000, 3),
    (150001, 250000, 4),
    (250001, 500000, 5),
)
_OTHER_BANDS = (
    (100000, 199999, 1),
    (200000, 299999, 2),
    (300000, 499999, 3),
    (500000, 1000000, 4),
)


def _default_lot_bands() -> list:
    rows = []
    for symbol in ("NIFTY", "SENSEX", "BANKNIFTY"):
        for low, high, lots in _INDEX_BANDS:
            rows.append((symbol, low, high, lots))
    for low, high, lots in _OTHER_BANDS:
        rows.append(("*", low, high, lots))
    return rows


def _seed_copy_rules(conn: sqlite3.Connection) -> None:
    """First-run amounts. Later edits in the portal are left alone."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        _seed_lot_bands(conn)
        if int(conn.execute("SELECT COUNT(*) FROM slippage_rules").fetchone()[0]) == 0:
            conn.execute(
                """INSERT INTO slippage_rules (symbol, slippage_mode, slippage_value)
                   VALUES ('*', 'PERCENT', 1)"""
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


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


def set_telegram_chat(user_id: int, chat_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET telegram_chat_id = ? WHERE id = ?", (chat_id, int(user_id)))


def user_by_email(email: str) -> Optional[dict]:
    with connect() as conn:
        return _one(conn, "SELECT * FROM users WHERE email = ?", (email.strip().lower(),))


def user_by_id(user_id: int) -> Optional[dict]:
    with connect() as conn:
        return _one(conn, "SELECT * FROM users WHERE id = ?", (user_id,))


def create_account(user_id: int, broker: str, label: str, client_id: str,
                   api_key_enc: str, api_secret_enc: str, mobile: str = "") -> int:
    now = clock.now().isoformat()
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO accounts
               (user_id, broker, label, client_id, mobile, api_key_enc, api_secret_enc, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, broker, label.strip(), client_id.strip(), (mobile or "").strip(),
             api_key_enc, api_secret_enc, now),
        )
        return int(cur.lastrowid)


def update_account(account_id: int, user_id: Optional[int], fields: dict) -> None:
    allowed = {
        "label", "client_id", "mobile", "api_key_enc", "api_secret_enc", "mode", "enabled",
        "product", "kill_switch", "kill_reason", "auto_kill", "flatten_on_kill",
        "max_loss_trade", "max_loss_day", "max_loss_day_pct", "paper_balance", "broker",
        "access_token_enc", "token_date",
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


_ACCOUNT_FROM = """SELECT accounts.*, users.email AS owner_email,
                          users.telegram_chat_id AS owner_telegram
                   FROM accounts JOIN users ON users.id = accounts.user_id"""


def get_account(account_id: int, user_id: Optional[int] = None) -> Optional[dict]:
    sql = _ACCOUNT_FROM + " WHERE accounts.id = ?"
    args: tuple = (account_id,)
    if user_id is not None:
        sql += " AND accounts.user_id = ?"
        args = (account_id, user_id)
    with connect() as conn:
        return _one(conn, sql, args)


def list_accounts(user_id: Optional[int] = None) -> List[dict]:
    sql = _ACCOUNT_FROM
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
        return _all(conn, _ACCOUNT_FROM + " WHERE accounts.enabled = 1 ORDER BY accounts.id")


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


class RangeOverlap(Exception):
    """The new range crosses one that is already saved for this symbol."""

    def __init__(self, symbol: str, min_balance: float, max_balance: float):
        self.symbol = symbol
        self.min_balance = float(min_balance)
        self.max_balance = float(max_balance)
        super().__init__(symbol)


def ranges_overlap(left_min: float, left_max: float, right_min: float, right_max: float) -> bool:
    """True when the two ranges share more than an endpoint."""
    if left_max < right_min or right_max < left_min:
        return False
    if left_max == right_min or right_max == left_min:
        return False
    return True


def _seed_lot_bands(conn: sqlite3.Connection) -> None:
    if int(conn.execute("SELECT COUNT(*) FROM lot_bands").fetchone()[0]):
        return
    saved = conn.execute("SELECT symbol, rupees_per_lot FROM margin_rules").fetchall()
    current = {(row[0], float(row[1])) for row in saved}
    if not current or current == set(_OLD_MARGIN):
        conn.executemany(
            "INSERT INTO lot_bands (symbol, min_balance, max_balance, lots) VALUES (?, ?, ?, ?)",
            _default_lot_bands(),
        )
        return
    for row in saved:
        per = float(row[1])
        if per <= 0:
            continue
        for step in range(1, 11):
            conn.execute(
                "INSERT INTO lot_bands (symbol, min_balance, max_balance, lots) VALUES (?, ?, ?, ?)",
                (row[0], per * step, per * (step + 1) - 1, step),
            )


def lot_bands() -> List[dict]:
    with connect() as conn:
        return _all(conn, "SELECT * FROM lot_bands ORDER BY symbol = '*', symbol, min_balance")


def lot_bands_for(symbol: str) -> List[dict]:
    """Ranges for this symbol. A named symbol does not also use `*`."""
    key = _symbol_key(symbol)
    with connect() as conn:
        if key != "*":
            rows = _all(
                conn,
                "SELECT * FROM lot_bands WHERE symbol = ? ORDER BY min_balance, max_balance",
                (key,),
            )
            if rows:
                return rows
        return _all(
            conn,
            "SELECT * FROM lot_bands WHERE symbol = '*' ORDER BY min_balance, max_balance",
        )


def add_lot_band(symbol: str, min_balance: float, max_balance: float, lots: int) -> None:
    key = _symbol_key(symbol)
    low = float(min_balance)
    high = float(max_balance)
    count = int(lots)
    if low < 0 or high < low or count < 1:
        raise ValueError("Set a minimum, a maximum at least that high, and at least 1 lot.")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT min_balance, max_balance FROM lot_bands WHERE symbol = ?", (key,)
            ).fetchall()
            for row in rows:
                if ranges_overlap(low, high, float(row[0]), float(row[1])):
                    raise RangeOverlap(key, float(row[0]), float(row[1]))
            conn.execute(
                "INSERT INTO lot_bands (symbol, min_balance, max_balance, lots) VALUES (?, ?, ?, ?)",
                (key, low, high, count),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def delete_lot_band(band_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM lot_bands WHERE id = ?", (int(band_id),))


def slippage_rules() -> List[dict]:
    with connect() as conn:
        return _all(conn, "SELECT * FROM slippage_rules ORDER BY symbol != '*', symbol")


def upsert_slippage(symbol: str, mode: str, value: float) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO slippage_rules (symbol, slippage_mode, slippage_value) VALUES (?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                 slippage_mode = excluded.slippage_mode,
                 slippage_value = excluded.slippage_value""",
            (_symbol_key(symbol), mode, float(value)),
        )


def delete_slippage(symbol: str) -> None:
    key = _symbol_key(symbol)
    if key == "*":
        return
    with connect() as conn:
        conn.execute("DELETE FROM slippage_rules WHERE symbol = ?", (key,))


def effective_slippage(symbol: str) -> dict:
    key = _symbol_key(symbol)
    with connect() as conn:
        row = None
        if key != "*":
            row = _one(conn, "SELECT * FROM slippage_rules WHERE symbol = ?", (key,))
        if row is None:
            row = _one(conn, "SELECT * FROM slippage_rules WHERE symbol = '*'", ())
    if row is None:
        return {"slippage_mode": "PERCENT", "slippage_value": 1.0}
    return {"slippage_mode": row["slippage_mode"], "slippage_value": float(row["slippage_value"])}


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
            # Points times lots times the contract multiplier. Order quantity is
            # the broker size (Kotak MCX is the board lot; Kite MCX is the lot count).
            pnl = round(
                (price - float(leg["avg_entry"])) * sell_lots * max(1, int(leg["multiplier"] or 1)),
                2,
            )
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


def latest_action(account_id: int, position_id: str, action: str) -> Optional[dict]:
    with connect() as conn:
        return _one(
            conn,
            """SELECT * FROM intents
               WHERE account_id = ? AND position_id = ? AND action = ?
               ORDER BY id DESC LIMIT 1""",
            (account_id, position_id, action),
        )


def count_action_status(account_id: int, position_id: str, action: str, status: str) -> int:
    with connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) FROM intents
               WHERE account_id = ? AND position_id = ? AND action = ? AND status = ?""",
            (account_id, position_id, action, status),
        ).fetchone()
        return int(row[0] or 0)


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
