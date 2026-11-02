"""
brokers/kotak.py
================
Kotak Neo orders for one user account.

Login is the Trade API TOTP flow: the saved token is the consumer key, the
saved secret is the MPIN, and each morning the portal sends a fresh
authenticator code. The trading session (token, sid, base URL) is what later
orders reuse.

place() does not retry. A timeout after the request left this process is
UNKNOWN, so recovery can look the order up by its tag instead of sending a
second one. Kotak echoes that tag as GuiOrdId.

Contracts are Kotak's own names. A Zerodha symbol such as NIFTY26N0322800PE
is translated through the day's scrip file before the order is sent.
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

from brokers.base import BrokerError, OrderRequest, OrderView, TokenError, TransientError
from core.clock import IST
from core.logging_setup import get
from data.rate_limiter import TokenBucket

log = get("kotak")

_SESSION = "https://mis.kotaksecurities.com"
_LOGIN = _SESSION + "/login/1.0/tradeApiLogin"
_VALIDATE = _SESSION + "/login/1.0/tradeApiValidate"
_FIN_KEY = "neotradeapi"
_SOURCE = "NEOTRADEAPI"

_SEGMENT = {
    "NFO": "nse_fo",
    "BFO": "bse_fo",
    "MCX": "mcx_fo",
    "NSE": "nse_cm",
    "BSE": "bse_cm",
}

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_WEEK_MONTH = {str(n): n for n in range(1, 10)}
_WEEK_MONTH.update({"O": 10, "N": 11, "D": 12})

_WEEKLY = re.compile(r"^([A-Z]+?)(\d{2})([1-9OND])(\d{2})(\d+(?:\.\d+)?)(CE|PE)$")
_MONTHLY = re.compile(
    r"^([A-Z]+?)(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d+(?:\.\d+)?)(CE|PE)$"
)

_COMPLETE = {"complete", "traded", "filled"}
_REJECTED = {"rejected"}
_CANCELLED = {"cancelled", "canceled"}

_scrip_lock = threading.Lock()
_scrip_cache: dict = {}

Symbols = Callable[[str, str], str]


class KotakBroker:
    name = "kotak"

    def __init__(self, consumer_key: str, mpin: str, session: str, ucc: str, mobile: str,
                 http=None, symbols: Optional[Symbols] = None) -> None:
        if not consumer_key:
            raise BrokerError("Kotak Trade API token is missing")
        self.consumer_key = consumer_key.strip()
        self.mpin = (mpin or "").strip()
        self.ucc = (ucc or "").strip()
        self.mobile = (mobile or "").strip()
        self._http = http or httpx.Client(timeout=20.0)
        self._symbols = symbols
        self._bucket = TokenBucket(5.0, 5)
        self.token = ""
        self.sid = ""
        self.base_url = ""
        if session:
            self._restore(session)

    def login(self, mobile: str, ucc: str, totp: str) -> dict:
        """Trade-API login. Returns the session blob the portal encrypts."""
        mobile = (mobile or self.mobile).strip()
        ucc = (ucc or self.ucc).strip()
        totp = (totp or "").strip()
        if not mobile.startswith("+") or not mobile[1:].isdigit():
            raise BrokerError("Mobile number must include the country code, like +9198…")
        if not ucc:
            raise BrokerError("UCC is missing")
        if not re.fullmatch(r"\d{6}", totp):
            raise BrokerError("Enter the 6-digit code from the authenticator app")
        if not self.mpin:
            raise BrokerError("MPIN is missing. Save it as the API secret on this account")
        view = self._call(
            "POST", _LOGIN,
            {"Authorization": self.consumer_key, "neo-fin-key": _FIN_KEY},
            json_body={"mobileNumber": mobile, "ucc": ucc, "totp": totp},
            order_sent=False,
        )
        data = _data(view)
        if not data.get("token") or not data.get("sid"):
            raise BrokerError(_message(view) or "Kotak did not return a view session")
        trade = self._call(
            "POST", _VALIDATE,
            {
                "Authorization": self.consumer_key,
                "neo-fin-key": _FIN_KEY,
                "sid": data.get("sid"),
                "Auth": data.get("token"),
            },
            json_body={"mpin": self.mpin},
            order_sent=False,
        )
        session = _data(trade)
        if not session.get("token") or not session.get("sid") or not session.get("baseUrl"):
            raise BrokerError(_message(trade) or "Kotak did not return a trading session")
        self.token = str(session["token"])
        self.sid = str(session["sid"])
        self.base_url = str(session["baseUrl"]).rstrip("/")
        self.ucc = str(session.get("ucc") or ucc)
        self.mobile = mobile
        return {
            "access_token": json.dumps({
                "token": self.token, "sid": self.sid, "base_url": self.base_url,
            }),
            "user_id": self.ucc,
            "user_name": session.get("greetingName") or self.ucc,
        }

    def board_lot(self, exchange: str, tradingsymbol: str) -> int:
        """Units Kotak accepts for one MCX lot. A lot count such as 2 is rejected."""
        if _segment(exchange) != "mcx_fo":
            return 1
        rows = _scrip_rows(self._http, self.consumer_key, "mcx_fo", self.base_url)
        return scrip_board_lot(rows, tradingsymbol)

    def cash_balance(self, exchange: str) -> float:
        """Cash used only to choose a lot size.

        Net plus margin already in use. An open position is added back, so it
        does not shrink the next trade.
        """
        self._require_session()
        payload = self._call(
            "POST", self._url("quick/user/limits"), self._trade_headers(form=True),
            form={"seg": "ALL", "exch": "ALL", "prod": "ALL"}, order_sent=False,
        )
        return cash_from_limits(payload, exchange)

    def place(self, order: OrderRequest) -> OrderView:
        self._require_session()
        segment = _segment(order.exchange)
        symbol = self._trading_symbol(order)
        order_type = _order_type(order.order_type)
        validity = "DAY" if segment == "mcx_fo" else order.validity
        price = "0" if order_type in ("MKT", "SL-M") else _px(order.price)
        trigger = _px(order.trigger_price) if order_type == "SL-M" else "0"
        if segment == "mcx_fo" and order.order_type == "SL-M":
            # MCX options reject a stop-market. A sell stop-limit sits under the trigger.
            order_type = "SL"
            price = _px(sell_stop_limit(order.trigger_price))
            trigger = _px(order.trigger_price)
        body = {
            "am": "NO",
            "dq": "0",
            "es": segment,
            "mp": "0",
            "pc": "MIS" if order.product == "MIS" else "NRML",
            "pf": "N",
            "pr": price,
            "pt": order_type,
            "qt": str(int(order.quantity)),
            "rt": "IOC" if validity == "IOC" else "DAY",
            "tp": trigger,
            "ts": symbol,
            "tt": "B" if order.side == "BUY" else "S",
            "ig": order.tag[:20],
            "os": _SOURCE,
        }
        payload = self._call(
            "POST", self._url("quick/order/rule/ms/place"),
            self._trade_headers(form=True, access_token=True), form=body,
        )
        order_id = str(payload.get("nOrdNo") or _data(payload).get("nOrdNo") or "")
        if not order_id or str(payload.get("stat") or "Ok").lower() not in ("ok", "success", ""):
            if not order_id:
                raise BrokerError(_message(payload) or "Kotak did not accept the order")
        if order.order_type == "SL-M":
            return self._await_working(order_id)
        return self._await(order_id)

    def modify(self, order_id: str, trigger_price: float, quantity: int) -> OrderView:
        self._require_session()
        price_type = "SL-M"
        price = "0"
        if _mcx_order(self._book_row(order_id, order_sent=False)):
            price_type = "SL"
            price = _px(sell_stop_limit(trigger_price))
        body = {
            "mp": "0",
            "dq": "0",
            "vd": "DAY",
            "pr": price,
            "pt": price_type,
            "am": "NO",
            "tp": _px(trigger_price),
            "qt": str(int(quantity)),
            "no": str(order_id),
            "os": _SOURCE,
        }
        payload = self._call(
            "POST", self._url("quick/order/vr/modify"),
            self._trade_headers(form=True, access_token=True), form=body,
        )
        if _already_done(payload):
            return self.fetch(order_id)
        if str(payload.get("stat") or "Ok").lower() not in ("ok", "success", ""):
            raise BrokerError(_message(payload) or "Kotak did not modify the stop")
        return OrderView(str(order_id), "OPEN", 0.0, 0, "stop modified")

    def cancel(self, order_id: str) -> OrderView:
        self._require_session()
        payload = self._call(
            "POST", self._url("quick/order/cancel"),
            self._trade_headers(form=True, access_token=True),
            form={"on": str(order_id), "am": "NO"},
        )
        if _already_done(payload):
            return self.fetch(order_id)
        try:
            return self.fetch(order_id)
        except TransientError:
            return OrderView(str(order_id), "CANCELLED", 0.0, 0, "cancel sent")

    def _book_row(self, order_id: str, order_sent: bool = True):
        book = self._call(
            "GET", self._url("quick/user/orders"), self._trade_headers(form=False),
            order_sent=order_sent,
        )
        wanted = str(order_id)
        return next((item for item in _rows(book) if str(item.get("nOrdNo") or "") == wanted), None)

    def fetch(self, order_id: str) -> OrderView:
        self._require_session()
        wanted = str(order_id)
        row = self._book_row(wanted)
        if row is None:
            payload = self._call(
                "POST", self._url("quick/order/history"), self._trade_headers(form=True),
                form={"nOrdNo": wanted},
            )
            rows = _rows(payload)
            row = rows[-1] if rows else None
        if row is None:
            return OrderView(wanted, "UNKNOWN", 0.0, 0, "order book is empty")
        return _view(row)

    def find_by_tag(self, tag: str) -> Optional[OrderView]:
        self._require_session()
        payload = self._call("GET", self._url("quick/user/orders"), self._trade_headers(form=False))
        matches = [row for row in _rows(payload) if tag and tag in str(row.get("GuiOrdId") or "")]
        if not matches:
            return None
        matches.sort(key=lambda row: int(float(row.get("fldQty") or 0)), reverse=True)
        return _view(matches[0])

    def _trading_symbol(self, order: OrderRequest) -> str:
        if self._symbols is not None:
            found = self._symbols(order.exchange, order.tradingsymbol)
        else:
            rows = _scrip_rows(self._http, self.consumer_key, _segment(order.exchange), self.base_url)
            found = resolve_trading_symbol(rows, order.tradingsymbol)
        if not found:
            raise BrokerError(f"Kotak has no contract for {order.tradingsymbol}")
        return found

    def _restore(self, session: str) -> None:
        try:
            saved = json.loads(session)
        except (TypeError, ValueError) as exc:
            raise TokenError("Kotak session is not usable. Log in again") from exc
        self.token = str(saved.get("token") or "")
        self.sid = str(saved.get("sid") or "")
        self.base_url = str(saved.get("base_url") or "").rstrip("/")

    def _require_session(self) -> None:
        if not self.token or not self.sid or not self.base_url:
            raise TokenError("Kotak login is missing. Open Daily login and enter today's code")

    def _trade_headers(self, form: bool, access_token: bool = False) -> dict:
        """Session headers for orders, limits and the order book.

        Place on e22 needs the access token and neo-fin-key together with
        Auth and Sid. Either one alone comes back 401. Limits and the order
        book stay on the session only.
        """
        headers = {
            "Sid": self.sid,
            "Auth": self.token,
            "neo-fin-key": _FIN_KEY,
        }
        if access_token:
            headers["Authorization"] = self.consumer_key
        if form:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            headers["accept"] = "application/json"
        return headers

    def _url(self, path: str) -> str:
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    def _call(self, method: str, url: str, headers: dict, json_body=None, form=None,
              order_sent: bool = True):
        self._pace()
        try:
            if method == "GET":
                response = self._http.get(url, headers=headers)
            elif form is not None:
                response = self._http.post(url, headers=headers, data={"jData": json.dumps(form)})
            else:
                response = self._http.post(url, headers=headers, json=json_body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if order_sent:
                raise TransientError(str(exc)) from exc
            raise BrokerError(str(exc)) from exc
        return _decode(response, order_sent)

    def _await_working(self, order_id: str) -> OrderView:
        try:
            view = self.fetch(order_id)
        except BrokerError:
            return OrderView(order_id, "UNKNOWN", 0.0, 0, "stop placed; status not confirmed")
        except TransientError:
            return OrderView(order_id, "UNKNOWN", 0.0, 0, "stop placed; status not confirmed")
        if view.status == "UNKNOWN":
            return OrderView(order_id, "OPEN", 0.0, 0, view.message or "stop placed")
        return view

    def _await(self, order_id: str) -> OrderView:
        """The order id already exists. A later book error must not throw it away."""
        last = OrderView(order_id, "UNKNOWN", 0.0, 0, "placed; status not confirmed")
        for _ in range(5):
            try:
                last = self.fetch(order_id)
            except BrokerError:
                return OrderView(order_id, "UNKNOWN", 0.0, 0, "placed; status not confirmed")
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


_MCX_LIMITS = {"COM", "COMM", "COMMODITY", "MCX"}
_FO_LIMITS = {"FO", "FNO", "NFO", "DERIVATIVE", "DERIVATIVES"}


def cash_from_limits(payload, exchange: str) -> float:
    """Gross cash for this market. Margin already in use is added back."""
    rows = _limit_rows(payload)
    if not rows:
        raise BrokerError("Kotak did not return a balance")
    row = _limit_row(rows, exchange)
    return max(0.0, _money(row.get("Net") if row.get("Net") not in (None, "") else row.get("net"))
               + _money(_first(row, "MarginUsed", "marginUsed")))


def _limit_rows(payload) -> list:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict) and _has_money(data):
        return [data]
    if _has_money(payload):
        return [payload]
    return []


def _has_money(row: dict) -> bool:
    return any(key in row for key in ("Net", "net", "MarginUsed", "marginUsed"))


def _limit_row(rows: list, exchange: str) -> dict:
    wanted = _MCX_LIMITS if (exchange or "").upper() == "MCX" else _FO_LIMITS

    def label(row: dict) -> str:
        return str(row.get("Category") or row.get("category") or row.get("seg") or "").upper().strip()

    for row in rows:
        if label(row) in wanted:
            return row
    for row in rows:
        if label(row) in ("", "ALL"):
            return row
    return rows[0]


def _first(row: dict, *names: str):
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return 0


def _money(value) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def parse_kite_symbol(tradingsymbol: str):
    """(name, date, strike, CE/PE, monthly) for a Zerodha weekly or monthly option.

    A monthly symbol only names the month, so its date is the first of that month
    and the caller matches any expiry inside the month.
    """
    text = (tradingsymbol or "").upper().strip()
    weekly = _WEEKLY.fullmatch(text)
    if weekly:
        name, yy, month, day, strike, opt = weekly.groups()
        try:
            when = datetime(2000 + int(yy), _WEEK_MONTH[month], int(day)).date()
        except ValueError:
            return None
        return name, when, float(strike), opt, False
    monthly = _MONTHLY.fullmatch(text)
    if monthly:
        name, yy, month, strike, opt = monthly.groups()
        when = datetime(2000 + int(yy), _MONTHS[month], 1).date()
        return name, when, float(strike), opt, True
    return None


def resolve_trading_symbol(rows: list, tradingsymbol: str) -> str:
    """Kotak pTrdSymbol for a Zerodha contract, or '' when the scrip file has no row."""
    row = resolve_contract(rows, tradingsymbol)
    if not row:
        return ""
    return str(_col(row, "pTrdSymbol") or "")


def resolve_contract(rows: list, tradingsymbol: str):
    """Scrip row for a Zerodha contract, or None when the file has no match."""
    wanted = (tradingsymbol or "").upper().strip()
    if not wanted:
        return None
    parsed = parse_kite_symbol(wanted)
    for row in rows:
        symbol = str(_col(row, "pTrdSymbol") or "").upper()
        if symbol == wanted:
            return row
    if parsed is None:
        return None
    name, when, strike, opt, monthly = parsed
    for row in rows:
        if str(_col(row, "pSymbolName") or "").upper().strip() != name:
            continue
        if str(_col(row, "pOptionType") or "").upper().strip() != opt:
            continue
        row_strike = _row_strike(row)
        if row_strike is None or abs(row_strike - strike) > 0.001:
            continue
        dates = _row_dates(row)
        if monthly:
            if any(item.year == when.year and item.month == when.month for item in dates):
                return row
        elif when in dates:
            return row
    return None


def scrip_board_lot(rows: list, tradingsymbol: str) -> int:
    """Kotak order size for one lot. MCX quotes this as barrels or units, not the lot count."""
    row = resolve_contract(rows, tradingsymbol)
    if not row:
        return 1
    return _lot_size(row)


def _lot_size(row: dict) -> int:
    for name in ("lLotSize", "iLotSize"):
        raw = _col(row, name)
        if raw in (None, ""):
            continue
        try:
            size = int(float(str(raw).strip()))
        except (TypeError, ValueError):
            continue
        if size > 0:
            return size
    return 1


def scrip_file_url(payload, segment: str) -> str:
    """The CSV URL for one segment. Kotak returns either a top-level list or one inside data."""
    files = []
    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        raw = data.get("filesPaths") or payload.get("filesPaths") or []
        files = raw if isinstance(raw, list) else []
    needle = (segment or "").lower()
    for item in files:
        if isinstance(item, str) and needle in item.lower():
            return item
        if isinstance(item, dict):
            for value in item.values():
                if isinstance(value, str) and needle in value.lower():
                    return value
    return ""


def _scrip_rows(http, consumer_key: str, segment: str, base_url: str) -> list:
    day = datetime.now(IST).date().isoformat()
    host = (base_url or _SESSION).rstrip("/")
    key = (host, segment, day)
    with _scrip_lock:
        cached = _scrip_cache.get(key)
        if cached is not None:
            return cached
    response = http.get(
        host + "/script-details/1.0/masterscrip/file-paths",
        headers={"Authorization": consumer_key, "accept": "application/json"},
    )
    payload = _decode(response, order_sent=False)
    url = scrip_file_url(payload, segment)
    if not url:
        raise BrokerError(f"Kotak contract list has no file for {segment}")
    downloaded = http.get(url)
    if getattr(downloaded, "status_code", 200) >= 400:
        raise BrokerError("Kotak contract list could not be downloaded")
    text = downloaded.text if isinstance(getattr(downloaded, "text", None), str) else downloaded.content.decode("utf-8", "replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    with _scrip_lock:
        _scrip_cache[key] = rows
    return rows


def _segment(exchange: str) -> str:
    segment = _SEGMENT.get((exchange or "").upper())
    if not segment:
        raise BrokerError(f"Kotak does not take exchange {exchange}")
    return segment


def sell_stop_limit(trigger: float, buffer: float = 0.05) -> float:
    """Limit under a sell trigger. Kotak MCX options refuse a stop-market."""
    trigger = round(float(trigger or 0), 2)
    if trigger <= 0:
        return 0.0
    tick = 0.05
    price = round(round((trigger * (1 - buffer)) / tick) * tick, 2)
    if price >= trigger:
        price = round(trigger - tick, 2)
    if price <= 0:
        price = tick
    return price


def _mcx_order(row) -> bool:
    if not isinstance(row, dict):
        return False
    segment = str(row.get("exSeg") or row.get("es") or row.get("exc") or "").lower()
    return "mcx" in segment


def _order_type(kind: str) -> str:
    if kind == "SL-M":
        return "SL-M"
    if kind == "MARKET":
        return "MKT"
    return "L"


def _px(price: float) -> str:
    return f"{float(price or 0):.2f}"


def _restore_dates_from_epoch(raw: str):
    stamp = int(float(raw))
    ist = datetime.fromtimestamp(stamp, IST).date()
    utc = datetime.fromtimestamp(stamp, timezone.utc).date()
    return {ist, utc}


def _row_dates(row: dict):
    raw = str(_col(row, "pExpiryDate") or "").strip()
    if not raw or raw.upper() == "NA":
        return set()
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return _restore_dates_from_epoch(raw)
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return {datetime.strptime(raw, fmt).date()}
        except ValueError:
            continue
    return set()


def _row_strike(row: dict):
    raw = _col(row, "dStrikePrice;")
    if raw in (None, ""):
        raw = _col(row, "dStrikePrice")
    if raw in (None, ""):
        return None
    try:
        return float(str(raw).strip()) / 100.0
    except ValueError:
        return None


def _col(row: dict, name: str):
    if name in row:
        return row[name]
    wanted = name.replace(";", "").lower()
    for key, value in row.items():
        if str(key).replace(";", "").strip().lower() == wanted:
            return value
    return None


def _data(payload: dict) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def _rows(payload) -> list:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        nested = data.get("data")
        if isinstance(nested, list):
            return [row for row in nested if isinstance(row, dict)]
        if data.get("nOrdNo") or data.get("ordSt"):
            return [data]
    if payload.get("nOrdNo") or payload.get("ordSt"):
        return [payload]
    return []


def _already_done(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    return int(payload.get("stCode") or 0) == 1021


def _message(payload) -> str:
    if not isinstance(payload, dict):
        return str(payload or "")
    error = payload.get("error") or payload.get("Error")
    if isinstance(error, list) and error:
        item = error[0]
        if isinstance(item, dict):
            return str(item.get("message") or item)
        return str(item)
    if error:
        return str(error)
    fault = payload.get("fault")
    if isinstance(fault, dict):
        return str(fault.get("description") or fault.get("message") or fault)
    return str(
        payload.get("errMsg") or payload.get("emsg") or payload.get("message")
        or payload.get("description") or payload.get("Error Message") or payload.get("rejRsn") or ""
    )


def _decode(response, order_sent: bool):
    status = int(getattr(response, "status_code", 0) or 0)
    try:
        payload = response.json()
    except Exception as exc:
        payload = {}
        if status == 0:
            raise TransientError("empty Kotak response") from exc
    if status in (401, 403):
        detail = _message(payload) or "Kotak session expired"
        raw = ""
        try:
            raw = (response.text or "")[:400]
        except Exception:
            raw = ""
        log.warning("Kotak HTTP %s: %s", status, raw or detail)
        raise TokenError(detail)
    if status == 429 or status >= 500:
        if order_sent:
            raise TransientError(_message(payload) or f"Kotak HTTP {status}")
        raise BrokerError(_message(payload) or f"Kotak HTTP {status}")
    if status >= 400:
        raise BrokerError(_message(payload) or f"Kotak HTTP {status}")
    if isinstance(payload, dict) and payload.get("error") and not payload.get("data"):
        raise BrokerError(_message(payload))
    return payload


def _view(row: dict) -> OrderView:
    raw = str(row.get("ordSt") or row.get("status") or "").strip().lower()
    if raw in _COMPLETE:
        status = "COMPLETE"
    elif raw in _REJECTED:
        status = "REJECTED"
    elif raw in _CANCELLED:
        status = "CANCELLED"
    elif raw:
        status = "OPEN"
    else:
        status = "UNKNOWN"
    try:
        filled = int(float(row.get("fldQty") or 0))
    except (TypeError, ValueError):
        filled = 0
    try:
        average = float(row.get("avgPrc") or 0)
    except (TypeError, ValueError):
        average = 0.0
    return OrderView(
        order_id=str(row.get("nOrdNo") or row.get("order_id") or ""),
        status=status,
        average_price=average,
        filled_quantity=filled,
        message=str(row.get("rejRsn") or row.get("stat") or ""),
    )
