"""
brokers/zerodha.py
==================
Kite Connect orders for one user account.

place() does not retry. A timeout after Kite accepted the order is reported
as UNKNOWN so the engine can look the order up by its tag instead of sending
a second one. The tag is 20 characters, which is Kite's limit.
"""

from __future__ import annotations

import time

from kiteconnect import KiteConnect
from kiteconnect import exceptions as kite_exc

from brokers.base import BrokerError, OrderRequest, OrderView, TokenError, TransientError
from data.rate_limiter import TokenBucket

_STATUS = {
    "COMPLETE": "COMPLETE",
    "REJECTED": "REJECTED",
    "CANCELLED": "CANCELLED",
}


class ZerodhaBroker:
    name = "zerodha"

    def __init__(self, api_key: str, api_secret: str, access_token: str) -> None:
        if not api_key or not api_secret:
            raise BrokerError("Zerodha API key or secret is missing")
        self.api_secret = api_secret
        self.kite = KiteConnect(api_key=api_key)
        if access_token:
            self.kite.set_access_token(access_token)
        self._bucket = TokenBucket(2.0, 2)

    def login_url(self) -> str:
        return self.kite.login_url()

    def exchange(self, request_token: str) -> dict:
        self._pace()
        try:
            data = self.kite.generate_session(request_token, api_secret=self.api_secret)
        except kite_exc.TokenException as exc:
            raise TokenError(str(exc)) from exc
        except Exception as exc:
            raise BrokerError(str(exc)) from exc
        token = data.get("access_token") or ""
        if not token:
            raise BrokerError("Zerodha did not return an access token")
        self.kite.set_access_token(token)
        return {
            "access_token": token,
            "user_id": data.get("user_id") or "",
            "user_name": data.get("user_name") or "",
        }

    def place(self, order: OrderRequest) -> OrderView:
        side = self.kite.TRANSACTION_TYPE_BUY if order.side == "BUY" else self.kite.TRANSACTION_TYPE_SELL
        product = self.kite.PRODUCT_MIS if order.product == "MIS" else self.kite.PRODUCT_NRML
        if order.order_type == "SL-M":
            order_type = self.kite.ORDER_TYPE_SLM
        elif order.order_type == "MARKET":
            order_type = self.kite.ORDER_TYPE_MARKET
        else:
            order_type = self.kite.ORDER_TYPE_LIMIT
        validity = self.kite.VALIDITY_IOC if order.validity == "IOC" else self.kite.VALIDITY_DAY
        self._pace()
        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=order.exchange,
                tradingsymbol=order.tradingsymbol,
                transaction_type=side,
                quantity=int(order.quantity),
                product=product,
                order_type=order_type,
                price=order.price if order_type == self.kite.ORDER_TYPE_LIMIT else None,
                trigger_price=order.trigger_price if order_type == self.kite.ORDER_TYPE_SLM else None,
                validity=validity,
                tag=order.tag[:20],
            )
        except kite_exc.TokenException as exc:
            raise TokenError(str(exc)) from exc
        except kite_exc.InputException as exc:
            raise BrokerError(str(exc)) from exc
        except Exception as exc:
            raise TransientError(str(exc)) from exc
        if order.order_type == "SL-M":
            return self._await_working(str(order_id))
        return self._await(str(order_id))

    def modify(self, order_id: str, trigger_price: float, quantity: int) -> OrderView:
        self._pace()
        try:
            self.kite.modify_order(
                variety=self.kite.VARIETY_REGULAR,
                order_id=order_id,
                quantity=int(quantity),
                trigger_price=float(trigger_price),
                order_type=self.kite.ORDER_TYPE_SLM,
            )
        except kite_exc.TokenException as exc:
            raise TokenError(str(exc)) from exc
        except kite_exc.InputException as exc:
            raise BrokerError(str(exc)) from exc
        except Exception as exc:
            raise TransientError(str(exc)) from exc
        return OrderView(str(order_id), "OPEN", 0.0, 0, "stop modified")

    def cancel(self, order_id: str) -> OrderView:
        self._pace()
        try:
            self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=order_id)
        except kite_exc.TokenException as exc:
            raise TokenError(str(exc)) from exc
        except kite_exc.InputException as exc:
            raise BrokerError(str(exc)) from exc
        except Exception as exc:
            raise TransientError(str(exc)) from exc
        try:
            return self.fetch(order_id)
        except TransientError:
            return OrderView(str(order_id), "CANCELLED", 0.0, 0, "cancel sent")

    def fetch(self, order_id: str) -> OrderView:
        self._pace()
        try:
            rows = self.kite.order_history(order_id) or []
        except kite_exc.TokenException as exc:
            raise TokenError(str(exc)) from exc
        except Exception as exc:
            raise TransientError(str(exc)) from exc
        if not rows:
            return OrderView(order_id, "UNKNOWN", 0.0, 0, "order history is empty")
        return _view(rows[-1])

    def find_by_tag(self, tag: str) -> OrderView | None:
        self._pace()
        try:
            rows = self.kite.orders() or []
        except kite_exc.TokenException as exc:
            raise TokenError(str(exc)) from exc
        except Exception as exc:
            raise TransientError(str(exc)) from exc
        matches = [row for row in rows if str(row.get("tag") or "") == tag]
        if not matches:
            return None
        matches.sort(key=lambda row: int(row.get("filled_quantity") or 0), reverse=True)
        return _view(matches[0])

    def _await_working(self, order_id: str) -> OrderView:
        """A stop is working while it waits. Do not poll until it fills."""
        try:
            view = self.fetch(order_id)
        except TransientError:
            return OrderView(order_id, "UNKNOWN", 0.0, 0, "stop placed; status not confirmed")
        if view.status == "UNKNOWN":
            return OrderView(order_id, "OPEN", 0.0, 0, view.message or "stop placed")
        return view

    def _await(self, order_id: str) -> OrderView:
        last = OrderView(order_id, "UNKNOWN", 0.0, 0, "placed; status not confirmed")
        for _ in range(5):
            try:
                last = self.fetch(order_id)
            except TransientError:
                time.sleep(0.25)
                continue
            if last.status in ("COMPLETE", "REJECTED", "CANCELLED"):
                return last
            time.sleep(0.25)
        return last

    def _pace(self) -> None:
        wait = self._bucket.acquire()
        if wait > 0:
            time.sleep(wait)


def _view(row: dict) -> OrderView:
    raw = str(row.get("status") or "").upper()
    status = _STATUS.get(raw, "OPEN" if raw else "UNKNOWN")
    return OrderView(
        order_id=str(row.get("order_id") or ""),
        status=status,
        average_price=float(row.get("average_price") or 0),
        filled_quantity=int(row.get("filled_quantity") or 0),
        message=str(row.get("status_message") or ""),
    )
