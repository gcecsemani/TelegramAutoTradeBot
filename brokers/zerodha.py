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

_POSITION_PAUSE = 0.4

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
        try:
            view = self._submit(order, quantity, product, order.order_type, order.price, -1)
        except BrokerError as exc:
            recovered = self._recover(order, quantity, product, str(exc))
            if recovered is None:
                raise
            view = recovered
        if view.status == "REJECTED":
            recovered = self._recover(order, quantity, product, view.message)
            if recovered is not None:
                view = recovered
        return view

    def _recover(self, order: OrderRequest, quantity: int, product: str, message: str):
        """A refused market or stop-market is sent once in a form Zerodha accepts."""
        if order.order_type == "SL-M" and _slm_blocked(message):
            return self._stop_limit(order, quantity, product)
        if order.order_type == "MARKET" and _protection_required(message):
            return self._submit(order, quantity, product, "MARKET", order.price, 5)
        return None

    def _stop_limit(self, order: OrderRequest, quantity: int, product: str) -> OrderView:
        limit = stop_limit_price(order.trigger_price, order.tick_size)
        return self._submit(order, quantity, product, "SL", limit, None)

    def _submit(self, order: OrderRequest, quantity: int, product: str,
                order_type: str, price: float, protection) -> OrderView:
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
        # F&O market and stop-market orders are refused unless this is set.
        # -1 asks Zerodha to apply its own protection band. Limit orders omit it.
        if kite_type not in (self.kite.ORDER_TYPE_MARKET, self.kite.ORDER_TYPE_SLM):
            protection = None
        self._pace()
        params = {
            "variety": self.kite.VARIETY_REGULAR,
            "exchange": order.exchange,
            "tradingsymbol": order.tradingsymbol,
            "transaction_type": side,
            "quantity": int(quantity),
            "product": kite_product,
            "order_type": kite_type,
            "price": send_price,
            "trigger_price": send_trigger,
            "validity": validity,
            "tag": order.tag[:20],
        }
        if protection is not None:
            params["market_protection"] = protection
        try:
            order_id = self.kite.place_order(**params)
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
        """Bought quantity for this contract. A failed read does not sell.

        The position book can lag a fill by a moment, so a stop placed straight
        after a buy looks a few times before it gives up.
        """
        symbol = tradingsymbol or ""
        last_error = None
        for attempt in range(3):
            if attempt:
                time.sleep(_POSITION_PAUSE)
            self._pace()
            try:
                payload = self.kite.positions() or {}
            except kite_exc.TokenException as exc:
                raise TokenError(str(exc)) from exc
            except Exception as exc:
                last_error = exc
                continue
            found = _long_rows(payload, exchange, symbol)
            if found:
                return _pick_long(found, product)
        if last_error is not None:
            raise BrokerError(
                f"Zerodha positions could not be read, so the sell was not sent. {last_error}"
            )
        raise BrokerError(f"No bought {symbol} at Zerodha, so the sell was not sent.")

    def modify(self, order_id: str, trigger_price: float, quantity: int) -> OrderView:
        """Move a working stop. A stop-limit keeps a limit under the new trigger."""
        kind = "SL-M"
        try:
            rows = self._history(order_id)
            kind = str((rows[-1] if rows else {}).get("order_type") or "SL-M").upper()
        except (TokenError, TransientError, BrokerError):
            kind = "SL-M"
        order_type = self.kite.ORDER_TYPE_SL if kind == "SL" else self.kite.ORDER_TYPE_SLM
        params = {
            "variety": self.kite.VARIETY_REGULAR,
            "order_id": order_id,
            "quantity": int(quantity),
            "trigger_price": float(trigger_price),
            "order_type": order_type,
        }
        if order_type == self.kite.ORDER_TYPE_SL:
            params["price"] = stop_limit_price(float(trigger_price))
        else:
            params["market_protection"] = -1
        try:
            self._modify(params)
        except BrokerError as exc:
            if order_type != self.kite.ORDER_TYPE_SL and _slm_blocked(str(exc)):
                params["order_type"] = self.kite.ORDER_TYPE_SL
                params["price"] = stop_limit_price(float(trigger_price))
                params.pop("market_protection", None)
                self._modify(params)
            else:
                raise
        return OrderView(str(order_id), "OPEN", 0.0, 0, "stop modified")

    def _modify(self, params: dict) -> None:
        self._pace()
        try:
            self.kite.modify_order(**params)
        except kite_exc.TokenException as exc:
            raise TokenError(str(exc)) from exc
        except kite_exc.InputException as exc:
            raise BrokerError(str(exc)) from exc
        except Exception as exc:
            raise TransientError(str(exc)) from exc

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
        rows = self._history(order_id)
        if not rows:
            return OrderView(order_id, "UNKNOWN", 0.0, 0, "order history is empty")
        return _view(rows[-1])

    def _history(self, order_id: str) -> list:
        self._pace()
        try:
            return self.kite.order_history(order_id) or []
        except kite_exc.TokenException as exc:
            raise TokenError(str(exc)) from exc
        except Exception as exc:
            raise TransientError(str(exc)) from exc

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


def _long_rows(payload: dict, exchange: str, symbol: str) -> list:
    """Long rows for this contract, from the net book and then the day book.

    A fill can show in the day book before the net book, and the same long
    can be listed in both. Each exchange and product is counted once.
    """
    symbol = (symbol or "").upper()
    wanted = (exchange or "").upper()
    books = []
    if isinstance(payload, dict):
        books.extend(payload.get("net") or [])
        books.extend(payload.get("day") or [])
    exact = []
    same_name = []
    seen = set()
    for row in books:
        if not isinstance(row, dict):
            continue
        name = str(row.get("tradingsymbol") or row.get("trading_symbol") or "").strip().upper()
        if name != symbol:
            continue
        qty = _position_qty(row)
        if qty <= 0:
            continue
        held = str(row.get("product") or "").strip().upper()
        venue = str(row.get("exchange") or "").strip().upper()
        key = (venue, held)
        if key in seen:
            continue
        seen.add(key)
        item = (held, qty)
        same_name.append(item)
        if not wanted or venue == wanted:
            exact.append(item)
    return exact or same_name


def _position_qty(row: dict) -> int:
    """Net long. A zero quantity still counts when the buy and sell columns show a long."""
    qty = None
    for key in ("quantity", "qty", "net_quantity", "netqty"):
        raw = row.get(key)
        if raw in (None, ""):
            continue
        try:
            qty = int(float(raw))
        except (TypeError, ValueError):
            continue
        break
    buy = _as_int(row.get("buy_quantity")) or _as_int(row.get("day_buy_quantity"))
    sell = _as_int(row.get("sell_quantity")) or _as_int(row.get("day_sell_quantity"))
    derived = buy - sell
    if qty is None:
        return derived
    if qty > 0:
        return qty
    return derived if derived > 0 else qty


def _as_int(raw) -> int:
    try:
        return int(float(raw or 0))
    except (TypeError, ValueError):
        return 0


def _pick_long(found: list, product: str) -> tuple:
    for held, qty in found:
        if held == (product or "").upper() and held in ("MIS", "NRML"):
            return held, qty
    held, qty = max(found, key=lambda item: item[1])
    if held in ("MIS", "NRML"):
        return held, qty
    return product or "NRML", qty


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


def _protection_required(message: str) -> bool:
    return "market protection" in (message or "").lower()


def _slm_blocked(message: str) -> bool:
    text = (message or "").lower()
    names = ("sl-m", "stoploss market", "stop loss market", "stop-loss market")
    if not any(name in text for name in names):
        return False
    return any(word in text for word in ("not allowed", "not permitted", "blocked", "discontinued"))


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
