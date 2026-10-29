"""
config.py
=========
Settings for the Telegram signal follower.

You post a call in the signal group:

    NIFTY 22800PE
    Buy Above 100        (or  Buy Near 90  /  Buy CMP)
    Sl 80                (optional)
    Tgt 120 140 160++    (optional)

and the bot takes a 2-lot paper trade on the nearest expiry at the live Kite
quote. Tag a line that only names the option (Sensex 72800 PE) with
Buy Above 120 / Buy Near 110 / Buy CMP to take that one. Tag the call with a
follow-up (Exit, Book half, SL to cost, Tgt 110 …) to manage it.
"Exit all" closes everything.

Everything is paper. No order is ever sent to the exchange.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()


def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default).strip().strip("'\"").strip().rstrip(",")


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════════════════════════
#  1. CREDENTIALS  (.env only)
# ══════════════════════════════════════════════════════════════════════════════
ZERODHA_API_KEY      = _env_str("ZERODHA_API_KEY", "")
ZERODHA_API_SECRET   = _env_str("ZERODHA_API_SECRET", "")
ZERODHA_ACCESS_TOKEN = _env_str("ZERODHA_ACCESS_TOKEN", "")
ZERODHA_USER_ID      = _env_str("ZERODHA_USER_ID", "")

TELEGRAM_BOT_TOKEN   = _env_str("TELEGRAM_BOT_TOKEN", "")
# Calls and follow-ups are read from the signal group. Everything the bot
# says goes to the update group. Same id in both = one group for all.
TELEGRAM_SIGNAL_CHAT_ID = _env_str("TELEGRAM_SIGNAL_CHAT_ID", _env_str("TELEGRAM_CHAT_ID", ""))
TELEGRAM_UPDATE_CHAT_ID = _env_str("TELEGRAM_UPDATE_CHAT_ID", "") or TELEGRAM_SIGNAL_CHAT_ID

# Optional. Comma-separated Telegram user ids whose messages are traded.
# Empty means every member of the group can post calls.
TELEGRAM_ALLOWED_USER_IDS = [
    item.strip() for item in _env_str("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if item.strip()
]


# ══════════════════════════════════════════════════════════════════════════════
#  2. INSTRUMENTS
# ══════════════════════════════════════════════════════════════════════════════
# The name is read from the words right before the strike ("NIFTY 22800PE").
# Order matters: the mini contracts are matched before the big ones, so
# "GOLD MINI" is GOLDM and not GOLD.
#
# aliases       regexes on the upper-cased name, matched in full.
# multiplier    units per lot for P&L. Kite lists every MCX contract with
#               lot size 1, so MCX needs it here. NFO/BFO use the exchange
#               lot size from the instrument dump.
SYMBOLS: dict = {
    "NIFTY":      {"exchange": "NFO", "aliases": [r"NIFTY\s*50", r"NIFTY"], "multiplier": None},
    "SENSEX":     {"exchange": "BFO", "aliases": [r"SENSEX"], "multiplier": None},
    "CRUDEOILM":  {"exchange": "MCX", "aliases": [r"CRUDE\s*OIL\s*M(?:INI)?", r"CRUDE\s*MINI"], "multiplier": 10},
    "CRUDEOIL":   {"exchange": "MCX", "aliases": [r"CRUDE\s*OIL", r"CRUDE"], "multiplier": 100},
    "NATGASMINI": {"exchange": "MCX", "aliases": [r"NAT(?:URAL)?\s*GAS\s*MINI", r"NG\s*MINI"], "multiplier": 250},
    "NATURALGAS": {"exchange": "MCX", "aliases": [r"NATURAL\s*GAS", r"NAT\s*GAS", r"NG"], "multiplier": 1250},
    "GOLDM":      {"exchange": "MCX", "aliases": [r"GOLD\s*M(?:INI)?"], "multiplier": 10},
    "GOLD":       {"exchange": "MCX", "aliases": [r"GOLD"], "multiplier": 100},
    "SILVERM":    {"exchange": "MCX", "aliases": [r"SILVER\s*M(?:INI)?"], "multiplier": 5},
    "SILVER":     {"exchange": "MCX", "aliases": [r"SILVER"], "multiplier": 30},
}

# Every other NFO option name (stocks such as BAJAJ-AUTO, and BANKNIFTY /
# FINNIFTY / MIDCPNIFTY) is tradable too, matched ignoring spaces and dashes:
# "Bajaj auto 10000 PE", "Bank nifty 54200PE".
NFO_STOCK_OPTIONS = True
STOCK_DEFAULTS: dict = {"exchange": "NFO", "aliases": [], "multiplier": None}


# ══════════════════════════════════════════════════════════════════════════════
#  3. SESSIONS (IST, "HH:MM")
# ══════════════════════════════════════════════════════════════════════════════
# MCX closes at 23:30 while the US is on daylight saving and 23:55 otherwise.
# "MCX" below is replaced by that close on the day.
SESSIONS: dict = {
    "NFO": {"open": "09:15", "close": "15:30", "square_off": "15:20"},
    "BFO": {"open": "09:15", "close": "15:30", "square_off": "15:20"},
    "MCX": {"open": "09:00", "close": "MCX",   "square_off_before_close_min": 10},
}

# Open trades are closed, and waiting calls dropped, at the square-off time
# above. Set to False to carry trades until you exit or their expiry day.
AUTO_SQUARE_OFF = True

HOLIDAYS: list = []      # "YYYY-MM-DD" — entries are refused on these days


# ══════════════════════════════════════════════════════════════════════════════
#  4. TRADE RULES
# ══════════════════════════════════════════════════════════════════════════════
TRADE: dict = {
    # Every trade is this many lots, on the nearest expiry. "Book half" sells
    # half of what is open (1 of 2).
    "lots":                    2,

    # "Buy Above X": buys once the premium is at or above X, and no more
    # than this % above it. Higher than that, it waits.
    "above_band_pct":          2.5,

    # "Buy Near X": buys at any price up to this % above X. Higher, it waits.
    "near_band_pct":           2.5,

    # A call without an SL gets this one, from the fill, until you send yours.
    # "No sl" / "Hero zero" removes it.
    "default_sl_pct":          30.0,

    # A new call older than this (bot was offline) is not taken.
    "max_signal_age_sec":      180,

    # Flat cost charged on every paper buy and sell, in rupees.
    "cost_per_order":          20.0,
}


# ══════════════════════════════════════════════════════════════════════════════
#  5. LOOPS
# ══════════════════════════════════════════════════════════════════════════════
ENGINE: dict = {
    "monitor_interval_sec":    1.0,     # SL / entry level / target check
    "telegram_poll_sec":       25,      # long-poll timeout
    "kite_rps":                _env_float("KITE_RPS", 2.5),
    "quote_outage_alert_sec":  60.0,    # warn on Telegram when quotes stop
    "keep_closed_days":        3,       # closed trades kept so late replies still match
}

NOTIFY: dict = {
    "max_msgs_per_min":  18,
    "retry_attempts":    3,
    "timeout_sec":       20,
}


# ══════════════════════════════════════════════════════════════════════════════
#  6. PATHS AND LOGGING
# ══════════════════════════════════════════════════════════════════════════════
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, "state")
LOG_DIR   = os.path.join(BASE_DIR, "logs")

PATHS: dict = {
    "positions":       os.path.join(STATE_DIR, "positions.json"),
    "telegram_offset": os.path.join(STATE_DIR, "telegram_offset.json"),
    "trade_journal":   os.path.join(STATE_DIR, "trades.csv"),
    "ledger_daily":    os.path.join(STATE_DIR, "ledger_daily.csv"),
    "ledger_monthly":  os.path.join(STATE_DIR, "ledger_monthly.csv"),
    "log_dir":         LOG_DIR,
}

LOGGING: dict = {
    "level":   _env_str("LOG_LEVEL", "INFO").upper(),
    "console": True,
    "file":    True,
}


# ══════════════════════════════════════════════════════════════════════════════
#  7. HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def symbol_cfg(name: str) -> dict:
    return SYMBOLS.get(name) or STOCK_DEFAULTS


def exchanges() -> list:
    out = []
    for cfg in SYMBOLS.values():
        if cfg["exchange"] not in out:
            out.append(cfg["exchange"])
    if NFO_STOCK_OPTIONS and "NFO" not in out:
        out.append("NFO")
    return out


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
    return first_sunday + timedelta(weeks=n - 1)


def mcx_close(day: date) -> str:
    """23:30 from the Monday after the second Sunday of March to the Monday
    after the first Sunday of November, 23:55 the rest of the year."""
    dst_start = _nth_sunday(day.year, 3, 2)
    dst_end = _nth_sunday(day.year, 11, 1)
    return "23:30" if dst_start < day <= dst_end else "23:55"


def validate() -> list:
    errs = []
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_SIGNAL_CHAT_ID):
        errs.append("TELEGRAM_BOT_TOKEN / TELEGRAM_SIGNAL_CHAT_ID are missing in .env")
    if not ZERODHA_API_KEY:
        errs.append("ZERODHA_API_KEY is missing in .env")
    if not ZERODHA_ACCESS_TOKEN:
        errs.append("ZERODHA_ACCESS_TOKEN is missing — run python3 login_zerodha.py")
    if TRADE["lots"] < 1:
        errs.append("TRADE['lots'] must be at least 1")
    if not 0 < TRADE["default_sl_pct"] < 100:
        errs.append("TRADE['default_sl_pct'] must sit between 0 and 100")
    if TRADE["above_band_pct"] < 0 or TRADE["near_band_pct"] < 0:
        errs.append("TRADE above / near bands must be ≥ 0")
    for name, cfg in SYMBOLS.items():
        if cfg["exchange"] not in SESSIONS:
            errs.append(f"{name}: exchange {cfg['exchange']} has no entry in SESSIONS")
        if cfg["exchange"] == "MCX" and not cfg.get("multiplier"):
            errs.append(f"{name}: MCX contracts need a multiplier")
    return errs
