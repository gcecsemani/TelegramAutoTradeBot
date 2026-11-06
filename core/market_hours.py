"""
core/market_hours.py
====================
Decides whether the signal bot should be running.

The window is weekday mornings from 08:00 IST through a few minutes after
the MCX close (23:30 or 23:55). Saturday, Sunday, and configured holidays
stay stopped. The portal is not part of this decision.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta
from typing import Optional

import config
from core import clock

_START = 8 * 60
_STOP = 10


def should_run(now: Optional[datetime] = None, holidays: Optional[set] = None) -> bool:
    now = now or clock.now()
    if now.tzinfo is None:
        now = clock.IST.localize(now)
    else:
        now = now.astimezone(clock.IST)
    closed = holidays if holidays is not None else set(config.HOLIDAYS)
    minutes = now.hour * 60 + now.minute
    if minutes < _STOP:
        day = now.date() - timedelta(days=1)
        return day.weekday() < 5 and day.isoformat() not in closed
    day = now.date()
    if day.weekday() >= 5 or day.isoformat() in closed:
        return False
    return minutes >= _START


def main() -> int:
    try:
        now = clock.now()
        run = should_run(now)
        running = subprocess.call(["systemctl", "is-active", "--quiet", "telegram-bot.service"]) == 0
        print(f"{now.strftime('%Y-%m-%d %H:%M')} IST should_run={run} running={running}", flush=True)
        if run:
            if not running:
                subprocess.check_call(["systemctl", "start", "telegram-bot.service"])
                print("signal bot started", flush=True)
            return 0
        if running:
            subprocess.check_call(["systemctl", "stop", "telegram-bot.service"])
            print("signal bot stopped", flush=True)
        return 0
    except Exception as exc:
        print(f"market hours check failed: {exc}", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
