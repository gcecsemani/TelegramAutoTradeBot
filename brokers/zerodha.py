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
        self.kite = KiteConnect(api_key=api_key, timeout=10)
        if access_token:
            self.kite.set_access_token(access_token)
        self._bucket = TokenBucket(2.0, 2)

    def cash_balance(self, exchange: str) -> float:
        """Cash used only to choose a lot size.

        Opening cash plus money added today. Margin already in a position is
        not subtracted, so one trade does not shrink the next.
        """
        self._pace()
        try:
            payload = self.kite.margins() or {}
        except kite_exc.TokenException as exc:
            raise TokenError(str(exc)) from exc
        except kite_exc.InputException as exc:
            raise BrokerError(str(exc)) from exc
        except Exception as exc:
            raise TransientError(str(exc)) from exc
        return cash_from_margins(payload, exchange)

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
        quantity = int(order.quantity)
        product = order.product or "MIS"
        if order.side == "SELL":
            product, held = self._open_long(order.exchange, order.tradingsymbol, product)
            quantity = min(quantity, held)
            if quantity <= 0:
                raise BrokerError(
                    f"No bought {order.tradingsymbol} at Zerodha, so the sell was not sent."
                )
        elif (order.exchange or "").upper() == "MCX":
            product = "NRML"
        view = self._submit(order, quantity, product, order.order_type, order.price)
        if order.order_type == "SL-M" and view.status == "REJECTED" and _slm_blocked(view.message):
            limit = stop_limit_price(order.trigger_price, order.tick_size)
            view = self._submit(order, quantity, product, "SL", limit)
        return view

    def _submit(self, order: OrderRequest, quantity: int, product: str,
                order_type: str, price: float) -> OrderView:
        side = self.kite.TRANSACTION_TYPE_BUY if order.side == "BUY" else self.kite.TRANSACTION_TYPE_SELL
        kite_product = self.kite.PRODUCT_MIS if product == "MIS" else self.kite.PRODUCT_NRML
        if order_type == "SL-M":
            kite_type = self.kite.ORDER_TYPE_SLM
        elif order_type == "SL":
            kite_type = self.kite.ORDER_TYPE_SL
        elif order_type == "MARKET":
            kite_type = self.kite.ORDER_TYPE_MARKET
        else:
            kite_type = self.kite.ORDER_TYPE_LIMIT
        kind = order_validity(order.exchange, order.validity)
        validity = self.kite.VALIDITY_IOC if kind == "IOC" else self.kite.VALIDITY_DAY
        send_price = price if kite_type in (self.kite.ORDER_TYPE_LIMIT, self.kite.ORDER_TYPE_SL) else None
        send_trigger = order.trigger_price if kite_type in (self.kite.ORDER_TYPE_SL, self.kite.ORDER_TYPE_SLM) else None
        self._pace()
        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=order.exchange,
                tradingsymbol=order.tradingsymbol,
                transaction_type=side,
                quantity=int(quantity),
                product=kite_product,
                order_type=kite_type,
                price=send_price,
                trigger_price=send_trigger,
                validity=validity,
                tag=order.tag[:20],
            )
        except kite_exc.TokenException as exc:
            raise TokenError(str(exc)) from exc
        except kite_exc.InputException as exc:
            raise BrokerError(str(exc)) from exc
        except Exception as exc:
            raise TransientError(str(exc)) from exc
        if order_type in ("SL-M", "SL"):
            return self._await_working(str(order_id))
        return self._await(str(order_id))

    def _open_long(self, exchange: str, tradingsymbol: str, product: str) -> tuple:
        """Bought quantity for this contract. A failed read does not sell."""
        self._pace()
        try:
            payload = self.kite.positions() or {}
        except kite_exc.TokenException as exc:
            raise TokenError(str(exc)) from exc
        except Exception as exc:
            raise BrokerError(
                f"Zerodha positions could not be read, so the sell was not sent. {exc}"
            ) from exc
        wanted = (exchange or "").upper()
        symbol = tradingsymbol or ""
        found = []
        for row in payload.get("net") or []:
            if str(row.get("exchange") or "").upper() != wanted:
                continue
            if str(row.get("tradingsymbol") or "") != symbol:
                continue
            qty = int(row.get("quantity") or 0)
            if qty <= 0:
                continue
            found.append((str(row.get("product") or "").upper(), qty))
        if not found:
            raise BrokerError(f"No bought {symbol} at Zerodha, so the sell was not sent.")
        for held, qty in found:
            if held == (product or "").upper() and held in ("MIS", "NRML"):
                return held, qty
        held, qty = max(found, key=lambda item: item[1])
        if held in ("MIS", "NRML"):
            return held, qty
        return product or "NRML", qty

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
        matches.sort(key=_tag_rank, reverse=True)
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


def order_validity(exchange: str, validity: str) -> str:
    """MCX and BFO reject IOC. NFO keeps the validity the engine asked for."""
    if (exchange or "").upper() in ("MCX", "BFO", "BSE"):
        return "DAY"
    return "IOC" if (validity or "").upper() == "IOC" else "DAY"


def stop_limit_price(trigger: float, tick: float = 0.05) -> float:
    """Sell limit under the trigger. Kite rejects a sell stop whose limit is above it."""
    tick = tick if tick and tick > 0 else 0.05
    steps = int((float(trigger) * 0.95) / tick)
    price = round(steps * tick, 2)
    if price <= 0 or price >= float(trigger):
        price = round(float(trigger) - tick, 2)
    return price


def _slm_blocked(message: str) -> bool:
    text = (message or "").lower()
    if "sl-m" in text or "stop loss market" in text or "stop-loss market" in text:
        return "not allowed" in text or "not permitted" in text or "blocked" in text
    return False


def _tag_rank(row: dict) -> tuple:
    status = str(row.get("status") or "").upper()
    filled = int(row.get("filled_quantity") or 0)
    if status == "COMPLETE" or filled > 0:
        band = 3
    elif status in ("OPEN", "TRIGGER PENDING", "MODIFY PENDING", "OPEN PENDING"):
        band = 2
    elif status == "REJECTED":
        band = 0
    else:
        band = 1
    return (band, filled)


def cash_from_margins(payload: dict, exchange: str) -> float:
    """Gross cash for this market. Equity for NFO/BFO, commodity for MCX.

    When that wallet is missing, the other wallet is used so a missing
    segment does not by itself refuse the trade. Utilised margin is ignored.
    """
    if not isinstance(payload, dict):
        raise BrokerError("Zerodha did not return a balance")
    primary = "commodity" if (exchange or "").upper() == "MCX" else "equity"
    secondary = "equity" if primary == "commodity" else "commodity"
    book = payload.get(primary)
    if not isinstance(book, dict) or not book:
        book = payload.get(secondary)
    if not isinstance(book, dict) or not book:
        raise BrokerError("Zerodha did not return a balance")
    available = book.get("available") if isinstance(book.get("available"), dict) else {}
    opening = _money(available.get("opening_balance"))
    payin = _money(available.get("intraday_payin"))
    if "opening_balance" in available or "intraday_payin" in available:
        if opening > 0 or payin > 0:
            return opening + payin
    if "cash" in available and _money(available.get("cash")) > 0:
        return _money(available.get("cash"))
    if "opening_balance" in available or "intraday_payin" in available:
        return max(0.0, opening + payin)
    utilised = book.get("utilised") if isinstance(book.get("utilised"), dict) else {}
    return max(0.0, _money(book.get("net")) + _money(utilised.get("debits")))


def _money(value) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


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
