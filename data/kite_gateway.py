"""
data/kite_gateway.py
====================
The one place that touches the Kite REST API. Read-only: profile, instrument
dumps and quotes. Every call goes through the rate limiter. TokenException
is escalated as TokenExpired because only a fresh login can fix it.
"""

from __future__ import annotations

from typing import Any, Dict, List

from kiteconnect import KiteConnect
from kiteconnect import exceptions as kite_exc

import config
from core.logging_setup import get
from data.rate_limiter import kite_call

log = get("kite")


class TokenExpired(RuntimeError):
    """Raised when the Kite access token is no longer valid."""


class KiteGateway:
    def __init__(self) -> None:
        if not config.ZERODHA_API_KEY:
            raise RuntimeError("ZERODHA_API_KEY missing — check .env")
        self.kite = KiteConnect(api_key=config.ZERODHA_API_KEY)
        if config.ZERODHA_ACCESS_TOKEN:
            self.kite.set_access_token(config.ZERODHA_ACCESS_TOKEN)
        self.profile_name = ""
        self.user_id = config.ZERODHA_USER_ID

    def check_connection(self) -> bool:
        try:
            profile = kite_call(self.kite.profile)
            self.profile_name = profile.get("user_name", "")
            self.user_id = profile.get("user_id", self.user_id)
            log.info("Kite connected as %s (%s)", self.profile_name, self.user_id)
            return True
        except kite_exc.TokenException as exc:
            raise TokenExpired(str(exc)) from exc
        except Exception as exc:
            log.error("Kite connection check failed: %s", exc)
            return False

    def instruments(self, exchange: str) -> List[Dict[str, Any]]:
        try:
            rows = kite_call(self.kite.instruments, exchange)
            log.info("Downloaded %d instruments for %s", len(rows or []), exchange)
            return rows or []
        except kite_exc.TokenException as exc:
            raise TokenExpired(str(exc)) from exc
        except Exception as exc:
            log.error("instruments(%s) failed: %s", exchange, exc)
            return []

    def quote(self, keys: List[str]) -> Dict[str, Any]:
        """Full quotes (LTP + depth), batched to Kite's 500-instrument limit."""
        out: Dict[str, Any] = {}
        for start in range(0, len(keys), 500):
            batch = keys[start:start + 500]
            try:
                out.update(kite_call(self.kite.quote, batch) or {})
            except kite_exc.TokenException as exc:
                raise TokenExpired(str(exc)) from exc
            except Exception as exc:
                log.error("quote %s failed: %s", batch[:3], exc)
        return out
