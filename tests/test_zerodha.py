"""Zerodha order shape. No live broker calls."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brokers.base import BrokerError, OrderRequest  # noqa: E402
from brokers.zerodha import ZerodhaBroker, stop_limit_price  # noqa: E402


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

    def set_access_token(self, token):
        return None

    def positions(self):
        if self.fail_positions:
            raise RuntimeError("positions down")
        return {"net": list(self.rows)}

    def place_order(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["order_type"] == "SL-M" and self.block_slm:
            return "rejected"
        return "1"

    def order_history(self, order_id):
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
        self.assertEqual(self.kite.calls[1]["order_type"], "SL")
        self.assertEqual(self.kite.calls[1]["trigger_price"], 80)
        self.assertEqual(self.kite.calls[1]["price"], stop_limit_price(80))
        self.assertLess(self.kite.calls[1]["price"], 80)
        self.assertEqual(view.status, "OPEN")


if __name__ == "__main__":
    unittest.main()
