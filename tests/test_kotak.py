"""Kotak Neo login, contract names, and order translation. No live broker calls."""

import json
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytz                                           # noqa: E402

from brokers.base import BrokerError, OrderRequest    # noqa: E402
from brokers.kotak import (  # noqa: E402
    KotakBroker, parse_kite_symbol, resolve_trading_symbol, scrip_board_lot, scrip_file_url,
)


def _epoch(year, month, day) -> str:
    ist = pytz.timezone("Asia/Kolkata")
    return str(int(ist.localize(datetime(year, month, day, 0, 0)).timestamp()))


class _Response:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        return self._payload


class _HTTP:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def post(self, url, headers=None, json=None, data=None):
        self.calls.append(("POST", url, headers or {}, json, data))
        return self._take(url)

    def get(self, url, headers=None):
        self.calls.append(("GET", url, headers or {}, None, None))
        return self._take(url)

    def _take(self, url):
        for needle, response in self.routes:
            if needle in url:
                return response
        raise AssertionError(url)


def _order(**overrides) -> OrderRequest:
    base = dict(
        exchange="NFO", tradingsymbol="NIFTY26N0322800PE", side="BUY", quantity=65,
        product="MIS", order_type="LIMIT", price=102.5, validity="IOC", tag="c" + "a" * 19,
        trigger_price=0,
    )
    base.update(overrides)
    return OrderRequest(**base)


class SymbolTests(unittest.TestCase):
    def test_weekly_and_monthly_names_map_to_kotak_symbols(self):
        weekly = parse_kite_symbol("NIFTY26N0322800PE")
        self.assertEqual(weekly[:4], ("NIFTY", datetime(2026, 11, 3).date(), 22800.0, "PE"))
        self.assertFalse(weekly[4])
        monthly = parse_kite_symbol("CRUDEOIL26NOV5200CE")
        self.assertEqual(monthly[0], "CRUDEOIL")
        self.assertEqual((monthly[1].year, monthly[1].month, monthly[3]), (2026, 11, "CE"))
        self.assertTrue(monthly[4])

        rows = [
            {
                "pSymbolName": "BANKNIFTY", "pTrdSymbol": "BANKNIFTY-WRONG",
                "pOptionType": "PE", "pExpiryDate": _epoch(2026, 11, 3), "dStrikePrice;": "2280000",
            },
            {
                "pSymbolName": "NIFTY", "pTrdSymbol": "NIFTY03NOV2622800PE",
                "pOptionType": "PE", "pExpiryDate": _epoch(2026, 11, 3), "dStrikePrice;": "2280000",
            },
            {
                "pSymbolName": "CRUDEOIL", "pTrdSymbol": "CRUDEOIL26NOV5200CE",
                "pOptionType": "CE", "pExpiryDate": _epoch(2026, 11, 17), "dStrikePrice;": "520000",
            },
        ]
        self.assertEqual(resolve_trading_symbol(rows, "NIFTY26N0322800PE"), "NIFTY03NOV2622800PE")
        self.assertEqual(resolve_trading_symbol(rows, "CRUDEOIL26NOV5200CE"), "CRUDEOIL26NOV5200CE")
        self.assertEqual(resolve_trading_symbol(rows, "SENSEX26N0572800PE"), "")

    def test_mcx_board_lot_is_the_scrip_lot_not_the_lot_count(self):
        epoch = _epoch(2026, 11, 19)
        rows = [
            {
                "pSymbolName": "CRUDEOILM", "pTrdSymbol": "CRUDEOILM19NOV269300PE",
                "pOptionType": "PE", "pExpiryDate": epoch, "dStrikePrice;": "930000",
                "lLotSize": "10",
            },
            {
                "pSymbolName": "GOLD", "pTrdSymbol": "GOLD19NOV26120000CE",
                "pOptionType": "CE", "pExpiryDate": epoch, "dStrikePrice;": "1200000",
                "iLotSize": "1",
            },
        ]
        self.assertEqual(scrip_board_lot(rows, "CRUDEOILM26NOV9300PE"), 10)
        self.assertEqual(scrip_board_lot(rows, "GOLD26NOV12000CE"), 1)
        self.assertEqual(scrip_board_lot(rows, "NIFTY26N0322800PE"), 1)

    def test_scrip_file_url_reads_either_response_shape(self):
        top = {"filesPaths": [
            "https://files.example/nse_cm.csv",
            "https://files.example/nse_fo.csv",
        ]}
        nested = {"data": {"filesPaths": ["https://files.example/mcx_fo.csv"]}}
        self.assertEqual(scrip_file_url(top, "nse_fo"), "https://files.example/nse_fo.csv")
        self.assertEqual(scrip_file_url(nested, "mcx_fo"), "https://files.example/mcx_fo.csv")
        self.assertEqual(scrip_file_url(top, "bse_fo"), "")


class OrderTests(unittest.TestCase):
    def _broker(self, routes, symbols=None):
        http = _HTTP(routes)
        broker = KotakBroker(
            "consumer", "123456",
            json.dumps({"token": "trade", "sid": "sid", "base_url": "https://orders.example"}),
            "UCC1", "+919800000000", http=http, symbols=symbols,
        )
        return broker, http

    def test_a_buy_uses_the_kotak_symbol_and_the_tag(self):
        filled = _Response(200, {"data": [{
            "nOrdNo": "9001", "ordSt": "complete", "avgPrc": "102.40", "fldQty": 65, "GuiOrdId": "c" + "a" * 19,
        }]})
        broker, http = self._broker([
            ("/place", _Response(200, {"stat": "Ok", "nOrdNo": "9001"})),
            ("/user/orders", filled),
        ], symbols=lambda exchange, symbol: "NIFTY03NOV2622800PE")
        view = broker.place(_order())
        self.assertEqual(view.status, "COMPLETE")
        self.assertEqual(view.average_price, 102.4)
        self.assertEqual(view.filled_quantity, 65)
        sent = json.loads(http.calls[0][4]["jData"])
        self.assertEqual(sent["es"], "nse_fo")
        self.assertEqual(sent["ts"], "NIFTY03NOV2622800PE")
        self.assertEqual(sent["tt"], "B")
        self.assertEqual(sent["pt"], "L")
        self.assertEqual(sent["rt"], "IOC")
        self.assertEqual(sent["pr"], "102.50")
        self.assertEqual(sent["ig"], "c" + "a" * 19)
        self.assertEqual(sent["pf"], "N")
        headers = http.calls[0][2]
        self.assertEqual(headers.get("neo-fin-key"), "neotradeapi")
        self.assertEqual(headers.get("Auth"), "trade")
        self.assertEqual(headers.get("Sid"), "sid")
        self.assertEqual(headers.get("Authorization"), "consumer")

    def test_balance_uses_the_session_without_the_access_token(self):
        broker, http = self._broker([
            ("/limits", _Response(200, {"data": [{
                "Category": "FO", "Net": "150000", "MarginUsed": "0",
            }]})),
        ])
        self.assertEqual(broker.cash_balance("NFO"), 150000)
        headers = http.calls[0][2]
        self.assertEqual(headers.get("Auth"), "trade")
        self.assertEqual(headers.get("Sid"), "sid")
        self.assertEqual(headers.get("neo-fin-key"), "neotradeapi")
        self.assertNotIn("Authorization", headers)
        sent = json.loads(http.calls[0][4]["jData"])
        self.assertEqual(sent, {"seg": "ALL", "exch": "ALL", "prod": "ALL"})

    def test_board_lot_reads_the_mcx_scrip_file(self):
        epoch = _epoch(2026, 11, 19)
        csv_body = (
            "pSymbolName,pTrdSymbol,pOptionType,pExpiryDate,dStrikePrice;,lLotSize\n"
            f"CRUDEOILM,CRUDEOILM19NOV269300PE,PE,{epoch},930000,10\n"
        )

        class _CSV(_Response):
            def __init__(self):
                super().__init__(200, {})
                self.text = csv_body
                self.content = csv_body.encode()

        broker, _http = self._broker([
            ("file-paths", _Response(200, {"data": {"filesPaths": ["https://files.example/mcx_fo.csv"]}})),
            ("mcx_fo.csv", _CSV()),
        ])
        self.assertEqual(broker.board_lot("NFO", "NIFTY26N0322800PE"), 1)
        self.assertEqual(broker.board_lot("MCX", "CRUDEOILM26NOV9300PE"), 10)

    def test_mcx_buys_are_day_orders_and_stops_stay_working(self):
        working = _Response(200, {"data": {"nOrdNo": "9002", "ordSt": "trigger pending", "GuiOrdId": "tag"}})
        broker, http = self._broker([
            ("/place", _Response(200, {"stat": "Ok", "nOrdNo": "9002"})),
            ("/user/orders", working),
        ], symbols=lambda exchange, symbol: "CRUDEOIL-STOP")
        view = broker.place(_order(
            exchange="MCX", tradingsymbol="CRUDEOIL26NOV5200CE", order_type="SL-M",
            price=0, trigger_price=80, validity="IOC", tag="tag",
        ))
        self.assertEqual(view.status, "OPEN")
        sent = json.loads(http.calls[0][4]["jData"])
        self.assertEqual(sent["es"], "mcx_fo")
        self.assertEqual(sent["pt"], "SL-M")
        self.assertEqual(sent["rt"], "DAY")
        self.assertEqual(sent["tp"], "80.00")
        self.assertEqual(sent["pr"], "0")

    def test_recovery_finds_the_order_by_its_tag(self):
        book = _Response(200, {"data": [
            {"nOrdNo": "1", "ordSt": "rejected", "GuiOrdId": "other", "fldQty": 0, "rejRsn": "no"},
            {"nOrdNo": "2", "ordSt": "complete", "GuiOrdId": "mine-cabc", "fldQty": 65, "avgPrc": "10"},
        ]})
        broker, _http = self._broker([("/orders", book)])
        view = broker.find_by_tag("cabc")
        self.assertEqual(view.order_id, "2")
        self.assertEqual(view.status, "COMPLETE")

    def test_login_saves_the_trading_session(self):
        http = _HTTP([
            ("tradeApiLogin", _Response(200, {"data": {"token": "view", "sid": "view-sid", "ucc": "UCC1"}})),
            ("tradeApiValidate", _Response(200, {"data": {
                "token": "trade-token", "sid": "trade-sid", "baseUrl": "https://e22.example/",
                "greetingName": "Mani", "ucc": "UCC1",
            }})),
        ])
        broker = KotakBroker("consumer", "654321", "", "UCC1", "+919800000000", http=http)
        saved = broker.login("+919800000000", "UCC1", "123456")
        session = json.loads(saved["access_token"])
        self.assertEqual(session["token"], "trade-token")
        self.assertEqual(session["sid"], "trade-sid")
        self.assertEqual(session["base_url"], "https://e22.example")
        self.assertEqual(saved["user_name"], "Mani")
        self.assertEqual(http.calls[1][2]["Auth"], "view")
        self.assertEqual(http.calls[1][3]["mpin"], "654321")

    def test_a_book_error_after_accept_keeps_the_order_id(self):
        broker, _http = self._broker([
            ("/place", _Response(200, {"stat": "Ok", "nOrdNo": "9003"})),
            ("/user/orders", _Response(400, {"error": [{"message": "book down"}]})),
        ], symbols=lambda exchange, symbol: "NIFTY03NOV2622800PE")
        view = broker.place(_order())
        self.assertEqual((view.order_id, view.status), ("9003", "UNKNOWN"))

    def test_a_dead_session_is_refused_before_an_order(self):
        broker = KotakBroker("consumer", "654321", "", "UCC1", "+919800000000", http=_HTTP([]))
        with self.assertRaises(BrokerError):
            broker.place(_order())


if __name__ == "__main__":
    unittest.main()
