"""
Which brokers can send an order today.

Zerodha places orders. Kotak Neo, Dhan and Groww can be saved; they do not
place orders.
"""

from __future__ import annotations

from typing import Dict

BROKERS: Dict[str, dict] = {
    "zerodha": {"label": "Zerodha", "live": True, "daily_auth": True},
    "kotak":   {"label": "Kotak Neo", "live": False, "daily_auth": False},
    "dhan":    {"label": "Dhan",    "live": False, "daily_auth": False},
    "groww":   {"label": "Groww",   "live": False, "daily_auth": False},
}

READY = {name for name, cfg in BROKERS.items() if cfg["live"]}


def label(name: str) -> str:
    return BROKERS.get(name, {}).get("label", name)
