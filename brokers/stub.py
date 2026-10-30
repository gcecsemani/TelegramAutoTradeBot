"""Placeholder for brokers that are named in the portal but not wired yet."""

from __future__ import annotations

from brokers.base import BrokerError, OrderRequest


class UnavailableBroker:
    def __init__(self, name: str) -> None:
        self.name = name

    def place(self, order: OrderRequest):
        raise BrokerError(f"{self.name} cannot place orders yet")

    def find_by_tag(self, tag: str):
        return None

    def fetch(self, order_id: str):
        return None
