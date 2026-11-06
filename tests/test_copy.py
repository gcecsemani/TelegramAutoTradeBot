"""Sizing, fan-out, and the rule that a lost response is not a second order."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                               # noqa: E402
from brokers.base import BrokerError, OrderView, TransientError  # noqa: E402
from brokers.kotak import cash_from_limits  # noqa: E402
from brokers.zerodha import cash_from_margins, order_validity  # noqa: E402
from execution import store                 # noqa: E402
from execution import engine as copy_engine  # noqa: E402
from execution.engine import CopyEngine     # noqa: E402
from execution.events import CopyEvent      # noqa: E402
from execution.risk import buy_limit, lots_for_balance, order_quantity, plan_buy, sell_lots  # noqa: E402


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

    def test_balance_sets_lots_without_looking_at_open_positions(self):
        self.assertEqual(lots_for_balance(50000, 50000), 1)
        self.assertEqual(lots_for_balance(149999, 50000), 2)
        self.assertEqual(lots_for_balance(200000, 100000), 2)
        self.assertEqual(lots_for_balance(49999, 50000), 0)

    def test_broker_cash_ignores_margin_already_in_use(self):
        margins = {
            "equity": {
                "available": {"opening_balance": 150000, "intraday_payin": 0, "cash": 150000, "live_balance": 10000},
                "utilised": {"debits": 140000},
                "net": 10000,
            },
            "commodity": {
                "available": {"opening_balance": 80000, "intraday_payin": 20000, "cash": 100000, "live_balance": 5000},
                "utilised": {"debits": 95000},
                "net": 5000,
            },
        }
        self.assertEqual(cash_from_margins(margins, "NFO"), 150000)
        self.assertEqual(cash_from_margins(margins, "MCX"), 100000)
        equity_only = {"equity": margins["equity"]}
        self.assertEqual(cash_from_margins(equity_only, "MCX"), 150000)
        self.assertRaises(BrokerError, cash_from_margins, {}, "NFO")
        self.assertEqual(order_validity("NFO", "IOC"), "IOC")
        self.assertEqual(order_validity("MCX", "IOC"), "DAY")
        self.assertEqual(order_validity("BFO", "IOC"), "DAY")

        kotak = [
            {"Category": "FO", "Net": "40000", "MarginUsed": "110000"},
            {"Category": "COM", "Net": "20000", "MarginUsed": "80000"},
        ]
        self.assertEqual(cash_from_limits(kotak, "NFO"), 150000)
        self.assertEqual(cash_from_limits(kotak, "MCX"), 100000)
        self.assertEqual(cash_from_limits({"Net": "50000", "MarginUsed": "0"}, "NFO"), 50000)

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
        self.assertEqual(by_account[self.one]["limit_price"], 101.0)
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

    def test_lot_size_follows_cash_and_an_open_position_does_not_shrink_the_next(self):
        store.update_account(self.one, self.user, {"paper_balance": 150000})
        self.engine.handle(_event(position_id="pos-a"))
        first = store.buy_intent(self.one, "pos-a")
        self.assertEqual(first["status"], "FILLED")
        self.assertEqual(first["lots"], 3)

        self.engine.handle(_event(
            position_id="pos-b", symbol="CRUDEOIL", exchange="MCX",
            tradingsymbol="CRUDEOIL26NOV5200CE", lot_size=1, multiplier=100,
        ))
        second = store.buy_intent(self.one, "pos-b")
        self.assertEqual(second["status"], "FILLED")
        self.assertEqual(second["lots"], 1)
        self.assertEqual(store.get_leg(self.one, "pos-a")["lots_open"], 3)

        store.update_account(self.one, self.user, {"paper_balance": 40000})
        self.engine.handle(_event(position_id="pos-small"))
        skipped = store.buy_intent(self.one, "pos-small")
        self.assertEqual(skipped["status"], "SKIPPED")
        self.assertIn("50,000", skipped["reason"])

    def test_admin_slippage_is_percent_or_points_for_one_symbol(self):
        store.upsert_slippage("NIFTY", "POINTS", 1.5)
        self.engine.handle(_event(position_id="pos-points"))
        nifty = store.buy_intent(self.one, "pos-points")
        self.assertEqual(nifty["limit_price"], 101.5)
        self.engine.handle(_event(
            position_id="pos-crude", symbol="CRUDEOIL", exchange="MCX",
            tradingsymbol="CRUDEOIL26NOV5200CE", lot_size=1, multiplier=100,
        ))
        crude = store.buy_intent(self.one, "pos-crude")
        self.assertEqual(crude["limit_price"], 101.0)

    def test_a_short_name_uses_the_same_slippage_row(self):
        store.upsert_slippage("CRUDE", "POINTS", 2)
        self.assertEqual(store.effective_slippage("CRUDEOIL"),
                         {"slippage_mode": "POINTS", "slippage_value": 2.0})
        self.engine.handle(_event(
            position_id="pos-crude-alias", symbol="CRUDEOIL", exchange="MCX",
            tradingsymbol="CRUDEOIL26NOV5200CE", lot_size=1, multiplier=100,
        ))
        crude = store.buy_intent(self.one, "pos-crude-alias")
        self.assertEqual(crude["limit_price"], 102.0)
        self.assertEqual(store.effective_slippage("NIFTY")["slippage_value"], 1.0)

    def test_a_live_account_sizes_from_broker_cash(self):
        class Funds:
            def cash_balance(self, exchange):
                return 200000 if exchange == "NFO" else 0

            def place(self, order):
                if order.order_type == "SL-M":
                    return OrderView(f"sl-{order.tag}", "OPEN", 0, 0, "stop")
                return OrderView("OID", "COMPLETE", order.price or 100, order.quantity, "accepted")

            def find_by_tag(self, tag):
                return None

            def fetch(self, order_id):
                return None

            def cancel(self, order_id):
                return OrderView(order_id, "CANCELLED", 0, 0, "cancelled")

            def modify(self, order_id, trigger_price, quantity):
                return OrderView(order_id, "OPEN", 0, 0, "modified")

        config.LIVE_TRADING = True
        store.update_account(self.one, self.user, {"mode": "live", "paper_balance": 10000})
        store.set_token(self.one, "token", "AB1234")
        self.engine.broker_factory = lambda account: Funds()
        self.engine.handle(_event(position_id="pos-live-size"))
        row = store.buy_intent(self.one, "pos-live-size")
        self.assertEqual(row["status"], "FILLED")
        self.assertEqual(row["lots"], 4)
        self.engine.handle(_event(position_id="pos-live-again"))
        again = store.buy_intent(self.one, "pos-live-again")
        self.assertEqual(again["lots"], 4)

    def test_a_live_kotak_account_is_not_sent(self):
        class Live:
            def __init__(self):
                self.quantities = []

            def cash_balance(self, exchange):
                return 200000

            def place(self, order):
                self.quantities.append(order.quantity)
                return OrderView("OID", "COMPLETE", order.price or 480, order.quantity, "accepted")

        config.LIVE_TRADING = True
        store.update_account(self.one, self.user, {"mode": "live", "broker": "kotak"})
        store.set_token(self.one, "token", "AB1234")
        store.update_account(self.two, self.user, {
            "kill_switch": 0, "kill_reason": "", "paper_balance": 200000,
        })
        live = Live()
        paper = copy_engine.build_broker(store.get_account(self.two))
        self.engine.broker_factory = lambda account: live if account["id"] == self.one else paper
        self.engine.handle(_event(
            position_id="crude-mini", symbol="CRUDEOILM", exchange="MCX",
            tradingsymbol="CRUDEOILM26NOV9300PE", lot_size=1, multiplier=10,
            price=480.55, sl=333.05, exact=True,
        ))
        bought = store.buy_intent(self.one, "crude-mini")
        paper_buy = store.buy_intent(self.two, "crude-mini")
        self.assertEqual(bought["status"], "SKIPPED")
        self.assertIn("cannot place", bought["reason"])
        self.assertEqual(live.quantities, [])
        self.assertEqual(paper_buy["status"], "FILLED")

    def test_live_zerodha_buys_match_the_call_and_the_fill_sets_the_stop(self):
        class Tape:
            def __init__(self):
                self.orders = []

            def cash_balance(self, exchange):
                return 200000

            def place(self, order):
                self.orders.append(order)
                if order.order_type == "SL-M":
                    return OrderView(f"sl-{order.tag}", "OPEN", 0, 0, "stop")
                fill = order.price or 102
                return OrderView("OID", "COMPLETE", fill, order.quantity, "accepted")

            def find_by_tag(self, tag):
                return None

        config.LIVE_TRADING = True
        store.update_account(self.one, self.user, {"mode": "live", "broker": "zerodha"})
        store.set_token(self.one, "token", "AB1234")
        tape = Tape()
        self.engine.broker_factory = lambda account: tape

        self.engine.handle(_event(position_id="cmp", entry="CMP", sl=80))
        self.assertEqual(tape.orders[0].order_type, "MARKET")
        self.assertEqual(tape.orders[0].validity, "DAY")
        self.assertEqual(tape.orders[0].price, 0.0)
        self.assertEqual(tape.orders[1].order_type, "SL-M")
        self.assertEqual(tape.orders[1].side, "SELL")
        self.assertEqual(tape.orders[1].trigger_price, 80.0)

        tape.orders.clear()
        self.engine.handle(_event(position_id="above", entry="ABOVE", price=100, sl=80))
        self.assertEqual(tape.orders[0].order_type, "LIMIT")
        self.assertEqual(tape.orders[0].validity, "DAY")
        self.assertEqual(tape.orders[0].price, 101.0)

        tape.orders.clear()
        self.engine.handle(_event(position_id="near", entry="NEAR", price=100, sl=80))
        self.assertEqual(tape.orders[0].order_type, "LIMIT")
        self.assertEqual(tape.orders[0].validity, "DAY")
        self.assertEqual(tape.orders[0].price, 101.0)

        tape.orders.clear()
        self.engine.handle(_event(position_id="only", entry="ONLY", exact=True, price=100, sl=70))
        self.assertEqual(tape.orders[0].order_type, "LIMIT")
        self.assertEqual(tape.orders[0].price, 100.0)
        self.assertEqual(tape.orders[1].trigger_price, 70.0)

        tape.orders.clear()
        self.engine.handle(_event(position_id="default-sl", entry="CMP", sl=70, sl_default=True))
        stops = [order for order in tape.orders if order.order_type == "SL-M"]
        self.assertEqual(stops[0].trigger_price, 71.4)

        self.engine.handle(_event(
            action="SELL", position_id="cmp", lots=2, lots_before=2, price=110, reason="BOOK",
        ))
        sells = [order for order in tape.orders if order.side == "SELL" and order.order_type == "MARKET"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0].validity, "DAY")
        before = len(tape.orders)
        self.engine.handle(_event(
            position_id="mcx-buy", entry="ABOVE", symbol="CRUDEOIL", exchange="MCX",
            tradingsymbol="CRUDEOIL26NOV5200CE", lot_size=1, multiplier=100, price=100, sl=70,
        ))
        mcx = next(order for order in tape.orders[before:] if order.side == "BUY")
        self.assertEqual(mcx.product, "NRML")
        self.assertEqual(mcx.order_type, "LIMIT")
        self.assertEqual(mcx.validity, "DAY")
        posted = "\n".join(self.notes)
        self.assertIn("Copy BUY", posted)
        self.assertNotIn("· paper", posted)

    def test_an_exit_cancels_a_resting_limit_instead_of_selling_short(self):
        class Resting:
            def __init__(self):
                self.orders = []
                self.cancelled = []

            def cash_balance(self, exchange):
                return 200000

            def place(self, order):
                self.orders.append(order)
                return OrderView("BUY1", "OPEN", 0, 0, "working")

            def fetch(self, order_id):
                return OrderView(order_id, "OPEN", 0, 0, "working")

            def find_by_tag(self, tag):
                return None

            def cancel(self, order_id):
                self.cancelled.append(order_id)
                return OrderView(order_id, "CANCELLED", 0, 0, "cancelled")

        config.LIVE_TRADING = True
        store.update_account(self.one, self.user, {"mode": "live", "broker": "zerodha"})
        store.set_token(self.one, "token", "AB1234")
        resting = Resting()
        self.engine.broker_factory = lambda account: resting
        self.engine.handle(_event(position_id="rest", entry="ABOVE", price=100, sl=80))
        self.assertEqual(store.buy_intent(self.one, "rest")["status"], "WORKING")
        self.engine.handle(_event(
            action="SELL", position_id="rest", lots=2, lots_before=2, price=100, reason="EXIT",
        ))
        self.assertEqual(resting.cancelled, ["BUY1"])
        self.assertEqual(store.buy_intent(self.one, "rest")["status"], "CANCELLED")
        self.assertIsNone(store.get_leg(self.one, "rest"))
        self.assertTrue(all(order.side == "BUY" for order in resting.orders))

    def test_a_fill_already_through_the_stop_is_sold(self):
        class Through:
            def __init__(self):
                self.orders = []

            def cash_balance(self, exchange):
                return 200000

            def place(self, order):
                self.orders.append(order)
                fill = 50 if order.side == "BUY" else 50
                return OrderView("OID", "COMPLETE", fill, order.quantity, "filled")

            def fetch(self, order_id):
                return None

            def find_by_tag(self, tag):
                return None

            def cancel(self, order_id):
                return OrderView(order_id, "CANCELLED", 0, 0, "cancelled")

        config.LIVE_TRADING = True
        store.update_account(self.one, self.user, {"mode": "live", "broker": "zerodha"})
        store.set_token(self.one, "token", "AB1234")
        broker = Through()
        self.engine.broker_factory = lambda account: broker
        self.engine.handle(_event(position_id="through", entry="CMP", price=100, sl=80))
        self.assertEqual([order.order_type for order in broker.orders], ["MARKET", "MARKET"])
        self.assertEqual([order.side for order in broker.orders], ["BUY", "SELL"])
        self.assertEqual(store.get_leg(self.one, "through")["lots_open"], 0)

    def test_a_partial_fill_is_booked_and_the_rest_keeps_working(self):
        class Partial:
            def __init__(self):
                self.orders = []

            def cash_balance(self, exchange):
                return 200000

            def place(self, order):
                self.orders.append(order)
                if order.order_type == "SL-M":
                    return OrderView("SL1", "OPEN", 0, 0, "stop")
                return OrderView("BUY1", "OPEN", 100, 65, "partial")

        config.LIVE_TRADING = True
        store.update_account(self.one, self.user, {"mode": "live", "broker": "zerodha"})
        store.set_token(self.one, "token", "AB1234")
        broker = Partial()
        self.engine.broker_factory = lambda account: broker
        self.engine.handle(_event(position_id="part", entry="ABOVE", price=100, sl=80))
        buy = store.buy_intent(self.one, "part")
        leg = store.get_leg(self.one, "part")
        self.assertEqual(buy["status"], "WORKING")
        self.assertEqual(leg["lots_open"], 1)
        self.assertEqual(leg["quantity_open"], 65)
        self.assertEqual(leg["sl_status"], "WORKING")
        self.assertEqual(leg["sl_trigger"], 80)
        self.assertEqual(broker.orders[1].quantity, 65)

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

    def test_a_missing_order_is_not_sent_again(self):
        class Lost:
            def __init__(self):
                self.placed = 0

            def place(self, order):
                self.placed += 1
                raise TransientError("timed out")

            def find_by_tag(self, tag):
                return None

            def fetch(self, order_id):
                return None

        broker = Lost()
        self.engine.broker_factory = lambda account: broker
        copy_engine._LOOKUP_GRACE_SEC = 0
        try:
            self.engine.handle(_event(position_id="pos-lost"))
            self.engine.reconcile()
        finally:
            copy_engine._LOOKUP_GRACE_SEC = 20
        self.assertEqual(broker.placed, 1)
        row = store.buy_intent(self.one, "pos-lost")
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertEqual(row["reason"], "UNCONFIRMED")

    def test_a_partial_ioc_fill_is_booked_and_protected(self):
        class Partial:
            def place(self, order):
                if order.order_type == "SL-M":
                    return OrderView("SL1", "OPEN", 0, 0, "stop")
                return OrderView("B1", "CANCELLED", order.price or 100, order.quantity // 2, "partial")

            def find_by_tag(self, tag):
                return None

            def fetch(self, order_id):
                return None

            def cancel(self, order_id):
                return OrderView(order_id, "CANCELLED", 0, 0, "cancelled")

            def modify(self, order_id, trigger_price, quantity):
                return OrderView(order_id, "OPEN", 0, 0, "modified")

        self.engine.broker_factory = lambda account: Partial()
        self.engine.handle(_event(position_id="pos-partial"))
        leg = store.get_leg(self.one, "pos-partial")
        self.assertEqual(leg["lots_open"], 1)
        self.assertEqual(leg["quantity_open"], 65)
        self.assertEqual(leg["sl_status"], "WORKING")
        self.assertEqual(leg["sl_quantity"], 65)

    def test_an_exit_retries_when_the_stop_is_still_working(self):
        class Sticky:
            def __init__(self):
                self.cancels = 0
                self.market_sells = 0

            def place(self, order):
                if order.order_type == "SL-M":
                    return OrderView("SL1", "OPEN", 0, 0, "working")
                if order.side == "SELL":
                    self.market_sells += 1
                return OrderView("B1", "COMPLETE", order.price or 100, order.quantity, "filled")

            def cancel(self, order_id):
                self.cancels += 1
                if self.cancels == 1:
                    return OrderView(order_id, "OPEN", 0, 0, "still working")
                return OrderView(order_id, "CANCELLED", 0, 0, "cancelled")

            def fetch(self, order_id):
                return OrderView(order_id, "OPEN", 0, 0, "working")

            def modify(self, order_id, trigger_price, quantity):
                return OrderView(order_id, "OPEN", 0, 0, "modified")

            def find_by_tag(self, tag):
                return None

        broker = Sticky()
        self.engine.broker_factory = lambda account: broker
        self.engine.handle(_event(position_id="pos-sticky", sl=70))
        self.engine.handle(_event(action="SELL", position_id="pos-sticky", lots=2, lots_before=2,
                                   price=90, sl=70, reason="EXIT"))
        self.assertEqual(broker.market_sells, 0)
        self.assertEqual(store.get_leg(self.one, "pos-sticky")["lots_open"], 2)
        waiting = store.active_sell(self.one, "pos-sticky")
        self.assertEqual(waiting["status"], "WAITING")
        copy_engine._EXIT_RETRY_SEC = 0
        try:
            self.engine.reconcile()
        finally:
            copy_engine._EXIT_RETRY_SEC = 3
        self.assertEqual(broker.market_sells, 1)
        self.assertEqual(store.get_leg(self.one, "pos-sticky")["lots_open"], 0)

    def test_a_rejected_stop_is_placed_again(self):
        class FlakyStop:
            def __init__(self):
                self.stops = 0

            def place(self, order):
                if order.order_type == "SL-M":
                    self.stops += 1
                    if self.stops == 1:
                        raise BrokerError("trigger rejected")
                    return OrderView("SL1", "OPEN", 0, 0, "stop")
                return OrderView("B1", "COMPLETE", order.price or 100, order.quantity, "filled")

            def find_by_tag(self, tag):
                return None

            def fetch(self, order_id):
                return None

            def cancel(self, order_id):
                return OrderView(order_id, "CANCELLED", 0, 0, "cancelled")

            def modify(self, order_id, trigger_price, quantity):
                return OrderView(order_id, "OPEN", 0, 0, "modified")

        broker = FlakyStop()
        self.engine.broker_factory = lambda account: broker
        self.engine.handle(_event(position_id="pos-flaky", sl=70))
        self.assertEqual(store.get_leg(self.one, "pos-flaky")["sl_status"], "FAILED")
        copy_engine._STOP_RETRY_SEC = 0
        try:
            self.engine.reconcile()
        finally:
            copy_engine._STOP_RETRY_SEC = 15
        self.assertEqual(broker.stops, 2)
        self.assertEqual(store.get_leg(self.one, "pos-flaky")["sl_status"], "WORKING")


if __name__ == "__main__":
    unittest.main()
