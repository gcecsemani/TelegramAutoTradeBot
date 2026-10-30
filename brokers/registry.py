"""
Which brokers can send an order today.

Zerodha is live. Dhan, Kotak and Groww are registered so accounts and keys
can be stored now; their adapters refuse to place an order until they exist.
"""

from __future__ import annotations

from typing import Dict

BROKERS: Dict[str, dict] = {
    "zerodha": {"label": "Zerodha", "live": True, "daily_auth": True},
    "dhan":    {"label": "Dhan",    "live": False, "daily_auth": False},
    "kotak":   {"label": "Kotak",   "live": False, "daily_auth": False},
    "groww":   {"label": "Groww",   "live": False, "daily_auth": False},
}

READY = {name for name, cfg in BROKERS.items() if cfg["live"]}


def label(name: str) -> str:
    return BROKERS.get(name, {}).get("label", name)
