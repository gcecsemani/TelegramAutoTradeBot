"""
core/clock.py
=============
Every time decision goes through here so the whole bot shares one notion of
"now" in IST, independent of the machine's timezone.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from typing import Optional

import pytz

import config

IST = pytz.timezone("Asia/Kolkata")


def now() -> datetime:
    return datetime.now(IST)


def today() -> date:
    return now().date()


def hhmm(value: str) -> dtime:
    parts = [int(p) for p in value.strip().split(":")]
    while len(parts) < 3:
        parts.append(0)
    return dtime(parts[0], parts[1], parts[2])


def stamp(fmt: str = "%H:%M:%S") -> str:
    return now().strftime(fmt)


def datestamp() -> str:
    return now().strftime("%Y-%m-%d")


def from_unix(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, IST)


def parse_iso(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else IST.localize(dt)


def as_date(value) -> Optional[date]:
    """Coerce a Kite expiry field (date / datetime / 'YYYY-MM-DD') to a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


# ─── Sessions ─────────────────────────────────────────────────────────────────

def _close_str(exchange: str, day: date) -> str:
    close = config.SESSIONS[exchange]["close"]
    return config.mcx_close(day) if close == "MCX" else close


def is_trading_day(day: Optional[date] = None) -> bool:
    day = day or today()
    return day.weekday() < 5 and day.isoformat() not in set(config.HOLIDAYS)


def is_open(exchange: str, t: Optional[datetime] = None) -> bool:
    t = t or now()
    if not is_trading_day(t.date()):
        return False
    session = config.SESSIONS[exchange]
    return hhmm(session["open"]) <= t.time() <= hhmm(_close_str(exchange, t.date()))


def session_text(exchange: str) -> str:
    return f"{config.SESSIONS[exchange]['open']}–{_close_str(exchange, today())}"


def square_off_time(exchange: str, day: Optional[date] = None) -> dtime:
    day = day or today()
    session = config.SESSIONS[exchange]
    if "square_off" in session:
        return hhmm(session["square_off"])
    close = datetime.combine(day, hhmm(_close_str(exchange, day)))
    return (close - timedelta(minutes=int(session.get("square_off_before_close_min", 0)))).time()


def past_square_off(exchange: str, t: Optional[datetime] = None) -> bool:
    t = t or now()
    if not is_trading_day(t.date()):
        return False
    return square_off_time(exchange, t.date()) <= t.time() <= hhmm(_close_str(exchange, t.date()))
