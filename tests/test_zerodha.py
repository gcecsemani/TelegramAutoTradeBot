"""Zerodha order shape. No live broker calls."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kiteconnect.exceptions import InputException  # noqa: E402

from brokers.base import BrokerError, OrderRequest  # noqa: E402
from brokers import zerodha as zerodha_mod  # noqa: E402
from brokers.zerodha import ZerodhaBroker, stop_limit_price  # noqa: E402

_NFO_SLM = (
    "Stoploss Market orders (SL-M) are blocked for F&O contracts as they have been "
    "discontinued by the exchange. Try placing SL-M order with market protection enabled."
)
_BFO_SLM = (
    "Stoploss Market orders (SL-M) are blocked in BFO as they have been discontinued "
    "by the exchange. Try placing SL-M order with market protection enabled."
)
_NAKED_MARKET = (
    "Market orders without market protection are not allowed via API. "
    "Please set market protection or use a Limit order."
)


class _Kite:
    VARIETY_REGULAR = "regular"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"
    PRODUCT_MIS = "MIS"
    PRODUCT_NRML = "NRML"
    ORDER_TYPE_LIMIT = "LIMIT"
    ORDER_TYPE_MARKET = "MARKET"
    ORDER_TYPE_SL = "SL"
    ORDER_TYPE_SLM = "SL-M"
    VALIDITY_IOC = "IOC"
    VALIDITY_DAY = "DAY"

    def __init__(self):
        self.calls = []
        self.rows = []
        self.fail_positions = False
        self.block_slm = False
        self.raise_slm = None
        self.raise_market = None
        self.position_reads = 0
        self._empty_reads = 0
        self.day_rows = []
        self.modifies = []
        self.modify_error = None
        self.history = None

    def set_access_token(self, token):
        return None

    def positions(self):
        self.position_reads += 1
        if self.fail_positions:
            raise RuntimeError("positions down")
        if self._empty_reads:
            self._empty_reads -= 1
            return {"net": []}
        return {"net": list(self.rows), "day": list(self.day_rows)}

    def modify_order(self, **kwargs):
        self.modifies.append(kwargs)
        if self.modify_error and kwargs.get("order_type") == "SL-M":
            raise self.modify_error
        return kwargs["order_id"]

    def place_order(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["order_type"] == "SL-M" and self.raise_slm:
            raise self.raise_slm
        if kwargs["order_type"] == "MARKET" and self.raise_market and kwargs.get("market_protection") == -1:
            raise self.raise_market
        if kwargs["order_type"] == "SL-M" and self.block_slm:
            return "rejected"
        return "1"

    def order_history(self, order_id):
        if self.history is not None:
            return self.history
        if order_id == "rejected":
            return [{
                "order_id": order_id, "status": "REJECTED", "average_price": 0,
                "filled_quantity": 0, "status_message": "Stop loss market orders are blocked",
            }]
        sent = self.calls[-1]
        status = "TRIGGER PENDING" if sent["order_type"] in ("SL", "SL-M") else "COMPLETE"
        return [{
            "order_id": order_id, "status": status, "average_price": sent.get("price") or 10,
            "filled_quantity": sent["quantity"], "status_message": "ok",
        }]


def _order(**overrides) -> OrderRequest:
    base = dict(
        exchange="NFO", tradingsymbol="NIFTY26N0322800PE", side="SELL", quantity=130,
        product="MIS", order_type="MARKET", price=0, validity="DAY", tag="c123",
        trigger_price=0,
    )
    base.update(overrides)
    return OrderRequest(**base)


class ZerodhaOrderTests(unittest.TestCase):
    def setUp(self):
        zerodha_mod._POSITION_PAUSE = 0
        self.broker = ZerodhaBroker("key", "secret", "token")
        self.kite = _Kite()
        self.broker.kite = self.kite

    def test_a_sell_is_not_sent_when_there_is_no_long(self):
        with self.assertRaises(BrokerError):
            self.broker.place(_order())
        self.assertEqual(self.kite.calls, [])

    def test_a_sell_is_not_sent_when_positions_cannot_be_read(self):
        self.kite.fail_positions = True
        self.kite.rows = [{"exchange": "NFO", "tradingsymbol": "NIFTY26N0322800PE",
                           "quantity": 130, "product": "MIS"}]
        with self.assertRaises(BrokerError):
            self.broker.place(_order())
        self.assertEqual(self.kite.calls, [])

    def test_a_sell_is_capped_at_the_long_and_uses_its_product(self):
        self.kite.rows = [{"exchange": "NFO", "tradingsymbol": "NIFTY26N0322800PE",
                           "quantity": 65, "product": "NRML"}]
        view = self.broker.place(_order(product="MIS"))
        sent = self.kite.calls[0]
        self.assertEqual(sent["quantity"], 65)
        self.assertEqual(sent["product"], "NRML")
        self.assertEqual(sent["transaction_type"], "SELL")
        self.assertEqual(sent["market_protection"], -1)
        self.assertEqual(view.filled_quantity, 65)

    def test_an_mcx_buy_is_nrml_and_a_limit_rests_for_the_day(self):
        view = self.broker.place(_order(
            side="BUY", exchange="MCX", tradingsymbol="CRUDEOILM26NOV9300PE",
            product="MIS", order_type="LIMIT", price=480.5, quantity=2, validity="IOC",
        ))
        sent = self.kite.calls[0]
        self.assertEqual(sent["product"], "NRML")
        self.assertEqual(sent["order_type"], "LIMIT")
        self.assertEqual(sent["price"], 480.5)
        self.assertEqual(sent["validity"], "DAY")
        self.assertIsNone(sent["trigger_price"])
        self.assertNotIn("market_protection", sent)
        self.assertEqual(view.status, "COMPLETE")

    def test_a_bfo_buy_is_a_day_order(self):
        self.broker.place(_order(
            side="BUY", exchange="BFO", tradingsymbol="SENSEX26N0372800PE",
            order_type="LIMIT", price=300, validity="IOC",
        ))
        self.assertEqual(self.kite.calls[0]["validity"], "DAY")
        self.assertEqual(self.kite.calls[0]["price"], 300)

    def test_a_rejected_stop_market_is_sent_again_as_a_stop_limit(self):
        self.kite.block_slm = True
        self.kite.rows = [{"exchange": "NFO", "tradingsymbol": "NIFTY26N0322800PE",
                           "quantity": 130, "product": "MIS"}]
        view = self.broker.place(_order(order_type="SL-M", trigger_price=80))
        self.assertEqual(self.kite.calls[0]["order_type"], "SL-M")
        self.assertEqual(self.kite.calls[0]["market_protection"], -1)
        self.assertEqual(self.kite.calls[1]["order_type"], "SL")
        self.assertNotIn("market_protection", self.kite.calls[1])
        self.assertEqual(self.kite.calls[1]["trigger_price"], 80)
        self.assertEqual(self.kite.calls[1]["price"], stop_limit_price(80))
        self.assertLess(self.kite.calls[1]["price"], 80)
        self.assertEqual(view.status, "OPEN")

    def test_a_stop_waits_until_the_new_long_shows_in_positions(self):
        self.kite._empty_reads = 2
        self.kite.rows = [{"exchange": "NFO", "tradingsymbol": "NIFTY26N0322800PE",
                           "quantity": 65, "product": "MIS"}]
        self.broker.place(_order(order_type="SL-M", trigger_price=70, quantity=65))
        self.assertEqual(self.kite.position_reads, 3)
        self.assertEqual(len(self.kite.calls), 1)
        self.assertEqual(self.kite.calls[0]["quantity"], 65)

    def test_moving_a_stop_limit_keeps_the_limit_under_the_trigger(self):
        self.kite.history = [{
            "order_id": "SL1", "status": "TRIGGER PENDING", "order_type": "SL",
            "average_price": 0, "filled_quantity": 0, "status_message": "",
        }]
        self.broker.modify("SL1", 70, 65)
        sent = self.kite.modifies[0]
        self.assertEqual(sent["order_type"], "SL")
        self.assertEqual(sent["trigger_price"], 70)
        self.assertLess(sent["price"], 70)
        self.assertNotIn("market_protection", sent)

    def test_a_stop_the_exchange_blocks_is_sent_as_a_stop_limit(self):
        self.kite.raise_slm = InputException(_NFO_SLM)
        self.kite.rows = [{"exchange": "NFO", "tradingsymbol": "NIFTY26N0322800PE",
                           "quantity": "130.0", "product": "MIS"}]
        view = self.broker.place(_order(order_type="SL-M", trigger_price=80))
        self.assertEqual(self.kite.calls[1]["order_type"], "SL")
        self.assertEqual(self.kite.calls[1]["price"], stop_limit_price(80))
        self.assertEqual(view.status, "OPEN")

    def test_a_bfo_stop_the_exchange_blocks_is_sent_as_a_stop_limit(self):
        self.kite.raise_slm = InputException(_BFO_SLM)
        self.kite.rows = [{"exchange": "BFO", "tradingsymbol": "SENSEX26N1280200PE",
                           "quantity": 20, "product": "MIS"}]
        view = self.broker.place(_order(
            exchange="BFO", tradingsymbol="SENSEX26N1280200PE",
            order_type="SL-M", trigger_price=90, quantity=20,
        ))
        self.assertEqual(self.kite.calls[0]["market_protection"], -1)
        self.assertEqual(self.kite.calls[1]["order_type"], "SL")
        self.assertLess(self.kite.calls[1]["price"], 90)
        self.assertEqual(view.status, "OPEN")

    def test_a_market_sell_refused_without_protection_is_sent_again_with_it(self):
        self.kite.raise_market = InputException(_NAKED_MARKET)
        self.kite.rows = [{"exchange": "BFO", "tradingsymbol": "SENSEX26N1280200PE",
                           "quantity": 20, "product": "NRML"}]
        view = self.broker.place(_order(
            exchange="BFO", tradingsymbol="SENSEX26N1280200PE", quantity=40,
        ))
        self.assertEqual(self.kite.calls[0]["market_protection"], -1)
        self.assertEqual(self.kite.calls[1]["order_type"], "MARKET")
        self.assertEqual(self.kite.calls[1]["market_protection"], 5)
        self.assertEqual(self.kite.calls[1]["quantity"], 20)
        self.assertEqual(self.kite.calls[1]["product"], "NRML")
        self.assertEqual(view.status, "COMPLETE")

    def test_a_market_buy_is_sent_with_protection(self):
        self.broker.place(_order(side="BUY", order_type="MARKET", price=0, quantity=65))
        sent = self.kite.calls[0]
        self.assertEqual(sent["order_type"], "MARKET")
        self.assertEqual(sent["market_protection"], -1)
        self.assertIsNone(sent["price"])

    def test_a_long_in_the_day_book_is_enough_to_sell(self):
        self.kite.day_rows = [{"exchange": "NFO", "tradingsymbol": "nifty26n0322800pe",
                               "quantity": 0, "buy_quantity": 65, "sell_quantity": 0,
                               "product": "MIS"}]
        view = self.broker.place(_order(quantity=130))
        self.assertEqual(self.kite.calls[0]["quantity"], 65)
        self.assertEqual(view.filled_quantity, 65)

    def test_the_same_long_in_net_and_day_is_counted_once(self):
        row = {"exchange": "NFO", "tradingsymbol": "NIFTY26N0322800PE",
               "quantity": 65, "product": "MIS"}
        self.kite.rows = [row]
        self.kite.day_rows = [dict(row)]
        self.broker.place(_order(quantity=200))
        self.assertEqual(self.kite.calls[0]["quantity"], 65)

    def test_a_long_on_another_exchange_code_still_allows_the_sell(self):
        self.kite.rows = [{"exchange": "BFO", "tradingsymbol": "NIFTY26N0322800PE",
                           "quantity": 65, "product": "MIS"}]
        self.broker.place(_order(exchange="NFO", quantity=65))
        self.assertEqual(self.kite.calls[0]["quantity"], 65)
        self.assertEqual(self.kite.calls[0]["exchange"], "NFO")

    def test_moving_a_stop_market_keeps_protection_and_falls_back_to_a_limit(self):
        self.kite.history = [{
            "order_id": "SL1", "status": "TRIGGER PENDING", "order_type": "SL-M",
            "average_price": 0, "filled_quantity": 0, "status_message": "",
        }]
        self.kite.modify_error = InputException(_NFO_SLM)
        self.broker.modify("SL1", 70, 65)
        self.assertEqual(self.kite.modifies[0]["order_type"], "SL-M")
        self.assertEqual(self.kite.modifies[0]["market_protection"], -1)
        self.assertEqual(self.kite.modifies[1]["order_type"], "SL")
        self.assertLess(self.kite.modifies[1]["price"], 70)
        self.assertNotIn("market_protection", self.kite.modifies[1])


if __name__ == "__main__":
    unittest.main()
