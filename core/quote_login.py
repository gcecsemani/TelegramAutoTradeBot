"""
core/quote_login.py
===================
The Zerodha key in .env is only for live quotes and the contract list.
Trading accounts log in on their own pages.

This writes a fresh access token into .env and into the current process.
The signal bot watches the same file and applies a new token without a restart.
"""

from __future__ import annotations

import os

from dotenv import dotenv_values, set_key
from kiteconnect import KiteConnect
from kiteconnect import exceptions as kite_exc

import config
from core import clock


def env_file() -> str:
    return os.path.join(config.BASE_DIR, ".env")


def _clean(value: object) -> str:
    return str(value or "").strip().strip("'\"").strip()


def saved(path: str = "") -> dict:
    """What .env currently says. The access token itself is not returned."""
    values = dotenv_values(path or env_file())
    token = _clean(values.get("ZERODHA_ACCESS_TOKEN"))
    day = _clean(values.get("ZERODHA_TOKEN_DATE"))
    return {
        "has_key": bool(_clean(values.get("ZERODHA_API_KEY"))),
        "has_secret": bool(_clean(values.get("ZERODHA_API_SECRET"))),
        "has_token": bool(token),
        "user_id": _clean(values.get("ZERODHA_USER_ID")),
        "token_date": day,
        "token_today": bool(token) and day == clock.today().isoformat(),
        "access_token": token,
    }


def login_url(path: str = "") -> str:
    state = saved(path)
    if not state["has_key"]:
        raise RuntimeError("ZERODHA_API_KEY is missing from .env")
    key = _clean(dotenv_values(path or env_file()).get("ZERODHA_API_KEY"))
    return KiteConnect(api_key=key).login_url()


def exchange(request_token: str, path: str = "") -> dict:
    """Turn a Kite redirect token into today's quote session and store it."""
    values = dotenv_values(path or env_file())
    key = _clean(values.get("ZERODHA_API_KEY"))
    secret = _clean(values.get("ZERODHA_API_SECRET"))
    if not key or not secret:
        raise RuntimeError("ZERODHA_API_KEY and ZERODHA_API_SECRET must be in .env")
    kite = KiteConnect(api_key=key)
    try:
        data = kite.generate_session(request_token, api_secret=secret)
    except kite_exc.TokenException as exc:
        raise RuntimeError(str(exc)) from exc
    token = _clean(data.get("access_token"))
    if not token:
        raise RuntimeError("Zerodha did not return an access token")
    user_id = _clean(data.get("user_id"))
    save(token, user_id, path)
    return {"user_id": user_id, "user_name": _clean(data.get("user_name")) or user_id}


def save(access_token: str, user_id: str = "", path: str = "") -> None:
    target = path or env_file()
    if not os.path.exists(target):
        raise RuntimeError(f"Env file not found: {target}")
    set_key(target, "ZERODHA_ACCESS_TOKEN", access_token)
    set_key(target, "ZERODHA_TOKEN_DATE", clock.today().isoformat())
    if user_id:
        set_key(target, "ZERODHA_USER_ID", user_id)
    if os.path.abspath(target) == os.path.abspath(env_file()):
        config.ZERODHA_ACCESS_TOKEN = access_token
        if user_id:
            config.ZERODHA_USER_ID = user_id


def apply_if_changed(gateway, previous: str, path: str = "") -> str:
    """Point the quote gateway at a newer .env token. Returns the token now in use."""
    token = saved(path)["access_token"]
    if not token or token == previous:
        return previous
    gateway.apply_token(token)
    config.ZERODHA_ACCESS_TOKEN = token
    user_id = saved(path)["user_id"]
    if user_id:
        config.ZERODHA_USER_ID = user_id
    return token
