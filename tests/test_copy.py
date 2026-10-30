"""Sizing, fan-out, and the rule that a lost response is not a second order."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                               # noqa: E402
from brokers.base import OrderView, TransientError  # noqa: E402
from execution import store                 # noqa: E402
from execution.engine import CopyEngine     # noqa: E402
from execution.events import CopyEvent      # noqa: E402
from execution.risk import buy_limit, order_quantity, plan_buy, sell_lots  # noqa: E402


def _event(**overrides) -> CopyEvent:
    base = dict(
        action="BUY", position_id="pos1", symbol="NIFTY", exchange="NFO",
        tradingsymbol="NIFTY26N0322800PE", tick_size=0.05, multiplier=65, lot_size=65,
        lots=2, lots_before=2, price=100.0, sl=80.0, reason="ENTRY", signal_ref="1:1",
    )
    base.update(overrides)
    return CopyEvent(**base)


class RiskTests(unittest.TestCase):
    def test_order_quantity_keeps_mcx_multiplier_out_of_the_order(self):
        self.assertEqual(order_quantity(2, 65, 65), 130)
        self.assertEqual(order_quantity(2, 1, 100), 2)

    def test_slippage_caps_the_buy(self):
        self.assertEqual(buy_limit(100, "PERCENT", 2.5, 0.05), 102.5)
        self.assertEqual(buy_limit(100, "POINTS", 1.5, 0.05), 101.5)

    def test_max_loss_cuts_lots_and_refuses_below_the_minimum(self):
        plan = plan_buy(
            lots=4, min_lots=1, max_lots=4, slippage_mode="PERCENT", slippage_value=0,
            price=100, sl=80, tick=0.05, multiplier=65, lot_size=65,
            max_loss_trade=5000, max_loss_day=None, day_realized=0, default_sl_pct=30,
        )
        self.assertTrue(plan.ok)
        self.assertEqual(plan.lots, 3)          # 20 × 65 = 1300; 5000 // 1300 = 3
        self.assertEqual(plan.quantity, 195)

        refused = plan_buy(
            lots=4, min_lots=4, max_lots=4, slippage_mode="POINTS", slippage_value=0,
            price=100, sl=80, tick=0.05, multiplier=65, lot_size=65,
            max_loss_trade=5000, max_loss_day=None, day_realized=0, default_sl_pct=30,
        )
        self.assertFalse(refused.ok)

    def test_a_spent_day_blocks_the_buy(self):
        plan = plan_buy(
            lots=1, min_lots=1, max_lots=2, slippage_mode="PERCENT", slippage_value=0,
            price=100, sl=80, tick=0.05, multiplier=65, lot_size=65,
            max_loss_trade=None, max_loss_day=1000, day_realized=-1000, default_sl_pct=30,
        )
        self.assertFalse(plan.ok)

    def test_sell_fraction_matches_the_master(self):
        self.assertEqual(sell_lots(4, 1, 2), 2)
        self.assertEqual(sell_lots(1, 1, 2), 1)
        self.assertEqual(sell_lots(3, 3, 3), 3)


class FanoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["PORTAL_DB"] = os.path.join(self.tmp, "portal.db")
        self._live = config.LIVE_TRADING
        config.LIVE_TRADING = False
        store.init()
        self.notes = []
        self.engine = CopyEngine(notify=self.notes.append)
        self.user = store.create_user("owner@example.com", "hash")
        self.one = store.create_account(self.user, "zerodha", "One", "", "k", "s")
        self.two = store.create_account(self.user, "zerodha", "Two", "", "k", "s")
        store.set_kill(self.two, True, "manual kill switch")

    def tearDown(self):
        config.LIVE_TRADING = self._live
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_one_account_fills_and_a_killed_account_does_not(self):
        self.engine.handle(_event())
        buys = [row for row in store.recent_intents(self.user) if row["action"] == "BUY"]
        by_account = {row["account_id"]: row for row in buys}
        self.assertEqual(by_account[self.one]["status"], "FILLED")
        self.assertEqual(by_account[self.one]["quantity"], 130)
        self.assertEqual(by_account[self.one]["limit_price"], 102.5)
        self.assertEqual(by_account[self.two]["status"], "SKIPPED")
        self.assertIn("kill switch", by_account[self.two]["reason"])

        self.engine.handle(_event())
        filled = [row for row in store.recent_intents(self.user)
                  if row["account_id"] == self.one and row["status"] == "FILLED"]
        self.assertEqual(len(filled), 1)
        self.assertEqual(store.get_leg(self.one, "pos1")["lots_open"], 2)

    def test_book_half_sells_half_the_account_lots(self):
        self.engine.handle(_event())
        self.engine.handle(_event(action="SELL", lots=1, lots_before=2, price=120, reason="BOOK HALF"))
        leg = store.get_leg(self.one, "pos1")
        self.assertEqual(leg["lots_open"], 1)
        self.assertGreater(store.day_realized(self.one), 0)

    def test_a_day_loss_trips_the_kill_switch(self):
        store.update_account(self.one, self.user, {"max_loss_day": 200, "auto_kill": 1})
        self.engine.handle(_event(sl=99))
        self.engine.handle(_event(action="SELL", lots=2, lots_before=2, price=90, sl=99, reason="SL HIT"))
        self.assertEqual(store.get_account(self.one)["kill_switch"], 1)
        self.assertLess(store.day_realized(self.one), -100)
        self.engine.handle(_event(position_id="pos-after-kill"))
        blocked = store.buy_intent(self.one, "pos-after-kill")
        self.assertEqual(blocked["status"], "SKIPPED")

    def test_live_mode_does_nothing_until_the_server_switch_is_on(self):
        store.update_account(self.one, self.user, {"mode": "live"})
        self.engine.handle(_event(position_id="pos-live"))
        row = store.buy_intent(self.one, "pos-live")
        self.assertEqual(row["status"], "SKIPPED")
        self.assertIn("LIVE_TRADING", row["reason"])

    def test_a_timeout_is_recovered_from_the_tag_without_a_second_order(self):
        class Once:
            def __init__(self):
                self.placed = 0
                self.saved = None

            def place(self, order):
                if order.order_type == "SL-M":
                    return OrderView("SL1", "OPEN", 0, 0, "stop")
                self.placed += 1
                self.saved = OrderView("OID1", "COMPLETE", order.price or 100, order.quantity, "accepted")
                raise TransientError("timed out after the broker accepted it")

            def find_by_tag(self, tag):
                return self.saved

            def fetch(self, order_id):
                return None

            def cancel(self, order_id):
                return OrderView(order_id, "CANCELLED", 0, 0, "cancelled")

            def modify(self, order_id, trigger_price, quantity):
                return OrderView(order_id, "OPEN", 0, 0, "modified")

        broker = Once()
        self.engine.broker_factory = lambda account: broker
        self.engine.handle(_event(position_id="pos-timeout"))
        self.assertEqual(broker.placed, 1)
        self.assertEqual(store.buy_intent(self.one, "pos-timeout")["status"], "UNKNOWN")

        self.engine.reconcile()
        self.assertEqual(broker.placed, 1)
        intent = store.buy_intent(self.one, "pos-timeout")
        self.assertEqual(intent["status"], "FILLED")
        self.assertEqual(intent["broker_order_id"], "OID1")
        self.assertEqual(store.get_leg(self.one, "pos-timeout")["lots_open"], 2)
        self.assertEqual(store.get_leg(self.one, "pos-timeout")["sl_status"], "WORKING")

    def test_an_exact_price_buy_does_not_add_slippage(self):
        self.engine.handle(_event(position_id="pos-exact", price=100, exact=True))
        self.assertEqual(store.buy_intent(self.one, "pos-exact")["limit_price"], 100)

    def test_a_fill_rests_a_stop_and_a_later_message_moves_it(self):
        self.engine.handle(_event(sl=80))
        leg = store.get_leg(self.one, "pos1")
        self.assertEqual(leg["sl_status"], "WORKING")
        self.assertEqual(leg["sl_trigger"], 80)
        self.assertEqual(leg["sl_quantity"], 130)

        self.engine.handle(_event(action="SL", sl=70, reason="SL UPDATE"))
        leg = store.get_leg(self.one, "pos1")
        self.assertEqual(leg["sl_trigger"], 70)
        self.assertEqual(leg["sl_status"], "WORKING")
        self.assertEqual(leg["sl_order_id"], store.get_leg(self.one, "pos1")["sl_order_id"])

        self.engine.handle(_event(action="SL", sl=None, reason="SL UPDATE"))
        leg = store.get_leg(self.one, "pos1")
        self.assertEqual(leg["sl_status"], "")
        self.assertEqual(leg["sl_order_id"], "")
        self.assertEqual(leg["lots_open"], 2)

    def test_no_stop_is_placed_for_hero_zero(self):
        self.engine.handle(_event(position_id="pos-zero", sl=None))
        leg = store.get_leg(self.one, "pos-zero")
        self.assertEqual(leg["lots_open"], 2)
        self.assertEqual(leg["sl_status"], "")

    def test_book_half_leaves_a_stop_on_the_remaining_lots(self):
        self.engine.handle(_event())
        self.engine.handle(_event(action="SELL", lots=1, lots_before=2, price=120, reason="BOOK HALF"))
        leg = store.get_leg(self.one, "pos1")
        self.assertEqual(leg["lots_open"], 1)
        self.assertEqual(leg["sl_status"], "WORKING")
        self.assertEqual(leg["sl_quantity"], 65)
        self.assertEqual(leg["sl_trigger"], 80)

    def test_a_stop_that_already_filled_is_not_sold_again(self):
        class StopFilled:
            def __init__(self):
                self.market_sells = 0

            def place(self, order):
                if order.order_type == "SL-M":
                    return OrderView("SL1", "OPEN", 0, 0, "working")
                if order.side == "SELL":
                    self.market_sells += 1
                return OrderView("B1", "COMPLETE", order.price or 100, order.quantity, "filled")

            def cancel(self, order_id):
                return OrderView(order_id, "COMPLETE", 70, 130, "stop filled")

            def fetch(self, order_id):
                return OrderView(order_id, "COMPLETE", 70, 130, "stop filled")

            def modify(self, order_id, trigger_price, quantity):
                return OrderView(order_id, "OPEN", 0, 0, "modified")

            def find_by_tag(self, tag):
                return None

        broker = StopFilled()
        self.engine.broker_factory = lambda account: broker
        self.engine.handle(_event(position_id="pos-stopped", sl=70))
        self.engine.handle(_event(action="SELL", position_id="pos-stopped", lots=2, lots_before=2,
                                   price=70, sl=70, reason="SL HIT"))
        self.assertEqual(broker.market_sells, 0)
        self.assertEqual(store.get_leg(self.one, "pos-stopped")["lots_open"], 0)


if __name__ == "__main__":
    unittest.main()
