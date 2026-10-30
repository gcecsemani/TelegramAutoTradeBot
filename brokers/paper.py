"""Fills at the requested limit. Used when an account's mode is paper."""

from __future__ import annotations

from brokers.base import OrderRequest, OrderView


class PaperBroker:
    name = "paper"

    def place(self, order: OrderRequest) -> OrderView:
        if order.order_type == "SL-M":
            return OrderView(f"paper-sl-{order.tag}", "OPEN", 0.0, 0, "paper stop")
        return OrderView(
            order_id="paper",
            status="COMPLETE",
            average_price=order.price if order.price > 0 else 0.0,
            filled_quantity=order.quantity,
            message="paper fill",
        )

    def modify(self, order_id: str, trigger_price: float, quantity: int) -> OrderView:
        return OrderView(order_id, "OPEN", 0.0, 0, "paper stop modified")

    def cancel(self, order_id: str) -> OrderView:
        return OrderView(order_id, "CANCELLED", 0.0, 0, "paper stop cancelled")

    def find_by_tag(self, tag: str) -> None:
        return None

    def fetch(self, order_id: str) -> OrderView | None:
        if str(order_id).startswith("paper-sl"):
            return OrderView(order_id, "OPEN", 0.0, 0, "paper stop working")
        return None
