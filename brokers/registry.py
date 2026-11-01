"""
Which brokers can send an order today.

Zerodha and Kotak Neo place orders. Dhan and Groww can be saved now; their
adapters refuse to place an order until they exist.
"""

from __future__ import annotations

from typing import Dict

BROKERS: Dict[str, dict] = {
    "zerodha": {"label": "Zerodha", "live": True, "daily_auth": True},
    "kotak":   {"label": "Kotak Neo", "live": True, "daily_auth": True},
    "dhan":    {"label": "Dhan",    "live": False, "daily_auth": False},
    "groww":   {"label": "Groww",   "live": False, "daily_auth": False},
}

READY = {name for name, cfg in BROKERS.items() if cfg["live"]}


def label(name: str) -> str:
    return BROKERS.get(name, {}).get("label", name)
