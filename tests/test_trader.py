"""
End-to-end paper trading with a fake Kite feed, a fake Telegram and a frozen
clock. Messages go through SignalBot.handle_update exactly as they would live.

    python3 -m unittest tests.test_trader -v
"""

import csv
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from core import clock  # noqa: E402
from data.instruments import InstrumentMaster  # noqa: E402

TODAY = date(2026, 10, 27)            # Tuesday
SIGNAL = "-1000000000001"
UPDATE = "-1000000000002"
PE = "NFO:NIFTY26OCT22800PE"


def _rows():
    rows = []
    token = 1

    def add(symbol, exchange, ts, expiry, strike, kind, lot, tick=0.05):
        nonlocal token
        token += 1
        rows.append({"symbol": symbol, "token": token, "tradingsymbol": ts, "exchange": exchange,
                     "expiry": expiry.isoformat(), "strike": strike, "type": kind,
                     "lot_size": lot, "tick_size": tick})

    for strike in range(22500, 23050, 50):
        for kind in ("CE", "PE"):
            add("NIFTY", "NFO", f"NIFTY26OCT{strike}{kind}", TODAY, float(strike), kind, 65)
    for kind in ("CE", "PE"):
        add("SENSEX", "BFO", f"SENSEX26OCT73000{kind}", date(2026, 10, 29), 73000.0, kind, 20)
        add("CRUDEOIL", "MCX", f"CRUDEOIL26NOV10000{kind}", date(2026, 11, 17), 10000.0, kind, 1, 0.1)
        add("BAJAJ-AUTO", "NFO", f"BAJAJ-AUTO26OCT10000{kind}", TODAY, 10000.0, kind, 75)
    return rows


class FakeGateway:
    def __init__(self):
        self.prices = {}            # key -> (ltp, bid, ask)
        self.profile_name = "Test"

    def set(self, key, ltp, spread=0.0):
        self.prices[key] = (ltp, ltp - spread / 2, ltp + spread / 2)

    def quote(self, keys):
        out = {}
        for key in keys:
            if key in self.prices:
                ltp, bid, ask = self.prices[key]
                out[key] = {"last_price": ltp,
                            "depth": {"buy": [{"price": bid}], "sell": [{"price": ask}]}}
        return out


class FakeTelegram:
    """Every bot message lands in the update group, like the real client."""

    def __init__(self):
        self.sent = []
        self.next_id = 100        # same numbers as the signal group on purpose: ids restart per group
        self.signal_chats = {SIGNAL}
        self.update_chat = UPDATE

    def send(self, text, reply_to=None, on_sent=None):
        self.next_id += 1
        self.sent.append({"id": self.next_id, "text": text, "reply_to": reply_to})
        if on_sent:
            on_sent(f"{UPDATE}:{self.next_id}")

    def send_document(self, filename, content, caption="", reply_to=None):
        self.next_id += 1
        self.sent.append({"id": self.next_id, "text": caption, "reply_to": reply_to,
                          "file": filename, "content": content})

    def last(self):
        return self.sent[-1]["text"]


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["PORTAL_DB"] = os.path.join(self.tmp, "portal.db")
        config.ZERODHA_API_KEY = config.ZERODHA_API_KEY or "test"
        config.TELEGRAM_BOT_TOKEN = config.TELEGRAM_BOT_TOKEN or "test"
        config.TELEGRAM_SIGNAL_CHAT_ID = config.TELEGRAM_SIGNAL_CHAT_ID or "1"
        config.TELEGRAM_UPDATE_CHAT_ID = config.TELEGRAM_UPDATE_CHAT_ID or "2"
        self._paths = dict(config.PATHS)
        for key in ("positions", "telegram_offset", "trade_journal", "ledger_daily", "ledger_monthly"):
            config.PATHS[key] = os.path.join(self.tmp, os.path.basename(config.PATHS[key]))
        self._now = clock.now
        self.set_time(10, 0)

        from bot import SignalBot
        self.bot = SignalBot()
        self.gw = FakeGateway()
        self.tg = FakeTelegram()
        self.bot.tg = self.tg
        self.bot.gw = self.gw
        self.bot.instruments = InstrumentMaster(self.gw)
        self.bot.instruments._index(_rows())
        self.bot.instruments._loaded_for = TODAY
        self.bot.trader.gw = self.gw
        self.bot.trader.instruments = self.bot.instruments
        self.trader = self.bot.trader
        self.msg_id = 100

    def tearDown(self):
        clock.now = self._now
        config.PATHS.clear()
        config.PATHS.update(self._paths)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_time(self, hour, minute, day=TODAY):
        fixed = clock.IST.localize(datetime(day.year, day.month, day.day, hour, minute))
        clock.now = lambda: fixed

    def post(self, text, reply_to=None, chat=SIGNAL, reply_text=None):
        """Post in the signal group (default) or the update group; reply_to is
        a message id in that same group. reply_text is that message's text."""
        self.msg_id += 1
        msg = {"message_id": self.msg_id, "date": clock.now().timestamp(),
               "chat": {"id": int(chat)}, "from": {"id": 1, "first_name": "Mani"}, "text": text}
        if reply_to:
            replied = {"message_id": reply_to}
            if reply_text is not None:
                replied["text"] = reply_text
            msg["reply_to_message"] = replied
        self.bot.handle_update({"update_id": self.msg_id, "message": msg})
        return self.msg_id

    def price(self, ltp, spread=0.0, key=PE):
        self.gw.set(key, ltp, spread)
        self.trader.tick()

    @staticmethod
    def sref(message_id):
        return f"{SIGNAL}:{message_id}"

    def only(self):
        positions = list(self.trader.positions.values())
        self.assertEqual(len(positions), 1)
        return positions[0]


class EntryTests(Harness):
    def test_buy_cmp_takes_two_lots_at_the_ask(self):
        self.gw.set(PE, 95.0, spread=1)
        self.post("NIFTY 22800PE\nBuy CMP\nSl 70\nTgt 110 130 150++")
        pos = self.only()
        self.assertEqual((pos.status, pos.entry_price, pos.lots, pos.multiplier, pos.sl, pos.targets),
                         ("OPEN", 95.5, 2, 65, 70.0, [110.0, 130.0, 150.0]))
        self.assertIn("BOUGHT NIFTY 22800 PE", self.tg.last())
        self.assertIn("2 lots (130 qty)", self.tg.last())

    def test_buy_above_waits_for_the_cross(self):
        self.gw.set(PE, 96.0)
        self.post("NIFTY 22800PE\nBuy Above 100\nSl 80\nTgt 120 140 160++")
        pos = self.only()
        self.assertEqual(pos.status, "PENDING")
        self.assertIn("between 100 and 102.50", self.tg.last())
        self.price(99.9)
        self.assertEqual(pos.status, "PENDING")
        self.price(100.5)
        self.assertEqual((pos.status, pos.entry_price, pos.sl), ("OPEN", 100.5, 80.0))

    def test_buy_above_not_bought_more_than_2_5_pct_over(self):
        self.gw.set(PE, 96.0)
        self.post("NIFTY 22800PE\nBuy Above 100")
        pos = self.only()
        self.price(104.0)                                   # jumped past 102.50
        self.assertEqual(pos.status, "PENDING")
        self.price(102.4)                                   # back inside the band
        self.assertEqual((pos.status, pos.entry_price), ("OPEN", 102.4))

    def test_buy_above_already_in_band_buys_now(self):
        self.gw.set(PE, 101.0)
        self.post("NIFTY 22800PE\nBuy Above 100")
        self.assertEqual((self.only().status, self.only().entry_price), ("OPEN", 101.0))

    def test_buy_above_already_far_above_waits(self):
        self.gw.set(PE, 110.0)
        self.post("NIFTY 22800PE\nBuy Above 100")
        self.assertEqual(self.only().status, "PENDING")

    def test_buy_near_waits_until_at_most_2_5_pct_over(self):
        self.gw.set(PE, 95.0)
        self.post("NIFTY 22800PE\nBuy Near 90\nSl 70")
        pos = self.only()
        self.assertEqual(pos.status, "PENDING")
        self.assertIn("at or under 92.25", self.tg.last())
        self.price(92.5)
        self.assertEqual(pos.status, "PENDING")
        self.price(92.2)
        self.assertEqual((pos.status, pos.entry_price), ("OPEN", 92.2))

    def test_buy_near_below_the_level_buys_now(self):
        self.gw.set(PE, 80.0)
        self.post("NIFTY 22800PE\nBuy Near 90\nSl 70")
        self.assertEqual((self.only().status, self.only().entry_price), ("OPEN", 80.0))

    def test_price_already_under_sl_is_refused(self):
        self.gw.set(PE, 65.0)
        self.post("NIFTY 22800PE\nBuy Near 90\nSl 70")
        self.assertEqual(self.trader.positions, {})
        self.assertIn("already at or below your SL 70", self.tg.last())

    def test_waiting_call_whose_price_falls_through_sl_is_dropped(self):
        self.gw.set(PE, 95.0)
        self.post("NIFTY 22800PE\nBuy Near 90\nSl 70")
        self.price(68.0)
        self.assertEqual(self.only().status, "CANCELLED")

    def test_default_sl_when_none_given(self):
        self.gw.set(PE, 100.0)
        self.post("NIFTY 22800PE\nBuy CMP")
        self.assertEqual(self.only().sl, 70.0)
        self.assertIn("30% default", self.tg.last())

    def test_other_instruments(self):
        self.gw.set("MCX:CRUDEOIL26NOV10000CE", 345.0)
        self.post("CRUDEOIL 10000CE\nBuy CMP\nSl 320")
        self.gw.set("NFO:BAJAJ-AUTO26OCT10000PE", 104.0)
        self.post("Bajaj auto 10000 PE\nBuy Near 105\nSl 70")
        qty = sorted((p.symbol, p.lots * p.multiplier) for p in self.trader.positions.values())
        self.assertEqual(qty, [("BAJAJ-AUTO", 150), ("CRUDEOIL", 200)])

    def test_closed_market_unlisted_stale_and_broken(self):
        self.set_time(8, 0)
        self.post("NIFTY 22800PE\nBuy CMP")
        self.assertIn("closed", self.tg.last())
        self.set_time(10, 0)
        self.post("NIFTY 22825PE\nBuy CMP")
        self.assertIn("not listed", self.tg.last())
        self.post("NIFTY 22800PE\nBuy 100")
        self.assertIn("Buy Above 100, Buy Near 90, Buy Near 100-105, Buy only 100 or Buy CMP", self.tg.last())
        self.msg_id += 1
        self.bot.handle_update({"update_id": 1, "message": {
            "message_id": self.msg_id, "date": clock.now().timestamp() - 900, "chat": {"id": int(SIGNAL)},
            "from": {"id": 1}, "text": "NIFTY 22800PE\nBuy CMP"}})
        self.assertIn("min old", self.tg.last())
        self.assertEqual(self.trader.positions, {})

    def test_other_messages_are_ignored(self):
        for text in ("good morning", "NIFTY 22800PE", "SL 70", "Book", "/status", "Sell NIFTY 22800PE"):
            self.post(text)
        self.assertEqual((self.tg.sent, self.trader.positions), ([], {}))


class FollowUpTests(Harness):
    def open_trade(self, text="NIFTY 22800PE\nBuy CMP\nSl 80\nTgt 110 130 150++", ltp=100.0):
        self.gw.set(PE, ltp)
        call = self.post(text)
        return call, self.only()

    def test_book_half_then_exit(self):
        call, pos = self.open_trade()
        self.gw.set(PE, 112.0)
        self.post("Book half", reply_to=call)
        self.assertEqual((pos.status, pos.lots), ("OPEN", 1))
        self.assertIn("BOOKED 1 OF 2 LOTS", self.tg.last())
        self.gw.set(PE, 120.0)
        self.post("Exit", reply_to=call)
        self.assertEqual(pos.status, "CLOSED")
        self.assertEqual(pos.gross, (12 + 20) * 65)
        self.assertIn("+₹2,020", self.tg.last())                 # 2080 − 3 orders × ₹20
        with open(config.PATHS["trade_journal"]) as fh:
            row = list(csv.DictReader(fh))[0]
        self.assertEqual((row["lots"], row["exit_price"], row["exit_reason"]),
                         ("2", "116.0", "BOOK HALF + EXIT (your reply)"))

    def test_book_50_forms_and_last_lot(self):
        for text in ("Book 50", "Book 50%"):
            self.trader.positions.clear()
            call, pos = self.open_trade()
            self.post(text, reply_to=call)
            self.assertEqual(pos.lots, 1, text)
            self.post("Book half", reply_to=call)
            self.assertEqual(pos.status, "CLOSED", text)
            self.assertIn("Only 1 lot was open", self.tg.last())

    def test_every_exit_word_exits_both_lots_in_profit_or_loss(self):
        for word, ltp in (("Exit", 60.0), ("Book", 130.0), ("Ignore", 99.0), ("Avoid", 101.0), ("Cancel", 90.0),
                          ("Close", 85.0), ("Sell", 140.0), ("SL hit", 81.0), ("Book full", 120.0),
                          ("Book fully", 120.0)):
            self.trader.positions.clear()
            call, pos = self.open_trade()
            self.gw.set(PE, ltp)
            self.post(word, reply_to=call)
            self.assertEqual((pos.status, pos.lots, pos.exits[-1]["lots"], pos.avg_exit),
                             ("CLOSED", 0, 2, ltp), word)

    def test_exit_on_a_waiting_call_drops_it(self):
        self.gw.set(PE, 110.0)
        call = self.post("NIFTY 22800PE\nBuy Near 90")
        self.post("Avoid", reply_to=call)
        self.assertEqual(self.only().status, "CANCELLED")
        self.price(85.0)
        self.assertEqual(self.only().status, "CANCELLED")

    def test_sl_follow_ups(self):
        call, pos = self.open_trade()
        self.gw.set(PE, 135.0)
        self.post("SL 90", reply_to=call)
        self.assertEqual(pos.sl, 90.0)
        for text in ("SL to cost", "SL at cost", "SL at CTC", "SL CTC"):
            pos.sl = 80.0
            self.post(text, reply_to=call)
            self.assertEqual(pos.sl, 100.0, text)
        self.post("SL to t1", reply_to=call)
        self.assertEqual(pos.sl, 110.0)
        self.post("SL to t2", reply_to=call)
        self.assertEqual(pos.sl, 130.0)
        self.post("SL to t3", reply_to=call)                     # 150 is above the live 135
        self.assertEqual(pos.sl, 130.0)
        self.assertIn("not below the live premium", self.tg.last())
        for text in ("No sl", "Hero zero", "Hero to Zero"):
            pos.sl, pos.no_sl = 90.0, False
            self.post(text, reply_to=call)
            self.assertEqual((pos.sl, pos.no_sl), (None, True), text)
        self.price(10.0)
        self.assertEqual(pos.status, "OPEN")                      # no SL, nothing fires

    def test_sl_to_a_missing_target(self):
        call, pos = self.open_trade("NIFTY 22800PE\nBuy CMP\nSl 80")
        self.gw.set(PE, 120.0)
        self.post("SL to t1", reply_to=call)
        self.assertEqual(pos.sl, 80.0)
        self.assertIn("has no target 1", self.tg.last())

    def test_targets_and_hold_ignored(self):
        call, pos = self.open_trade("NIFTY 22800PE\nBuy CMP\nSl 80")
        self.post("Tgt 110", reply_to=call)
        self.assertEqual(pos.targets, [110.0])
        self.post("Target 110 120 130", reply_to=call)
        self.assertEqual(pos.targets, [110.0, 120.0, 130.0])
        count = len(self.tg.sent)
        self.post("Hold", reply_to=call)
        self.assertEqual((len(self.tg.sent), pos.status, pos.sl), (count, "OPEN", 80.0))

    def test_waiting_call_follow_ups(self):
        self.gw.set(PE, 110.0)
        call = self.post("NIFTY 22800PE\nBuy Near 90")
        pos = self.only()
        self.post("SL 70", reply_to=call)
        self.post("Tgt 120 140", reply_to=call)
        self.post("SL to cost", reply_to=call)
        self.assertIn("not bought yet", self.tg.last())
        self.price(91.0)
        self.assertEqual((pos.status, pos.sl, pos.targets), ("OPEN", 70.0, [120.0, 140.0]))

    def test_sl_hit_closes_both_lots(self):
        call, pos = self.open_trade()
        self.price(79.5)
        self.assertEqual((pos.status, pos.close_reason, pos.exits[0]["lots"]), ("CLOSED", "SL HIT", 2))
        self.post("Exit", reply_to=call)
        self.assertIn("already closed", self.tg.last())

    def test_targets_only_alert(self):
        call, pos = self.open_trade()
        self.price(111.0)
        self.assertIn("Target 1 110 reached", self.tg.last())
        self.price(131.0)
        self.assertIn("Target 2 130 reached", self.tg.last())
        self.assertEqual((pos.status, pos.sl, pos.lots), ("OPEN", 80.0, 2))
        count = len(self.tg.sent)
        self.price(132.0)
        self.assertEqual(len(self.tg.sent), count)

    def test_square_off(self):
        call, pos = self.open_trade()
        self.set_time(15, 20)
        self.price(105.0)
        self.assertEqual((pos.status, pos.close_reason), ("CLOSED", "SQUARE-OFF"))

    def test_exit_all(self):
        self.gw.set(PE, 100.0)
        self.post("NIFTY 22800PE\nBuy CMP")
        self.gw.set("MCX:CRUDEOIL26NOV10000CE", 345.0)
        self.post("CRUDEOIL 10000CE\nBuy CMP")
        self.gw.set("BFO:SENSEX26OCT73000CE", 300.0)
        self.post("SENSEX 73000CE\nBuy Near 200")
        self.post("Exit all")
        statuses = sorted(p.status for p in self.trader.positions.values())
        self.assertEqual(statuses, ["CANCELLED", "CLOSED", "CLOSED"])
        self.assertIn("Exit all done", self.tg.last())
        self.assertIn("2 closed, 1 waiting", self.tg.last())

    def test_tagged_message_with_no_trade(self):
        self.post("Exit", reply_to=5)
        self.assertIn("No trade is linked", self.tg.last())

    def test_buy_on_a_tagged_watchlist(self):
        key = "BFO:SENSEX26OCT73000PE"
        watch_text = "Sensex 73000 PE |  | ADD TO WATCHLIST"
        self.gw.set(key, 250.0)
        watch = self.post(watch_text)
        self.assertEqual(list(self.trader.positions), [])
        reply = self.post("Buy CMP\nSl 200\nTgt 300 340", reply_to=watch, reply_text=watch_text)
        pos = self.only()
        self.assertEqual((pos.status, pos.symbol, pos.strike, pos.opt_type, pos.mode, pos.entry_price,
                          pos.sl, pos.targets),
                         ("OPEN", "SENSEX", 73000.0, "PE", "CMP", 250.0, 200.0, [300.0, 340.0]))
        self.assertIn(self.sref(watch), pos.refs)
        self.assertIn(self.sref(reply), pos.refs)
        self.assertIn("BOUGHT", self.tg.last())
        self.post("SL 220", reply_to=watch)
        self.assertEqual(pos.sl, 220.0)

    def test_tagged_buy_above_waits_and_near_buys(self):
        key = "BFO:SENSEX26OCT73000PE"
        watch_text = "SENSEX 73000PE"
        self.gw.set(key, 250.0)
        watch = self.post(watch_text)
        self.post("Buy Above 260", reply_to=watch, reply_text=watch_text)
        pos = self.only()
        self.assertEqual((pos.status, pos.mode, pos.level), ("PENDING", "ABOVE", 260.0))
        self.post("Exit", reply_to=watch)
        self.assertEqual(pos.status, "CANCELLED")
        self.gw.set(key, 250.0)
        self.post("Buy Near 255", reply_to=watch, reply_text=watch_text)
        opened = self.trader.find_by_message(self.sref(watch))
        self.assertEqual((opened.status, opened.mode, opened.level, opened.entry_price),
                         ("OPEN", "NEAR", 255.0, 250.0))

    def test_tagged_buy_while_open_is_refused(self):
        key = "BFO:SENSEX26OCT73000PE"
        watch_text = "Sensex 73000 PE"
        self.gw.set(key, 250.0)
        watch = self.post(watch_text)
        self.post("Buy CMP", reply_to=watch, reply_text=watch_text)
        self.post("Buy Near 240", reply_to=watch, reply_text=watch_text)
        self.assertEqual(len(self.trader.positions), 1)
        self.assertIn("still open", self.tg.last())

    def test_tagged_buy_without_a_price_or_an_option_is_explained(self):
        watch = self.post("Sensex 73000 PE")
        self.post("Buy above", reply_to=watch, reply_text="Sensex 73000 PE")
        self.assertIn("Buy Above", self.tg.last())
        other = self.post("good morning")
        self.post("Buy CMP", reply_to=other, reply_text="good morning")
        self.assertIn("no option", self.tg.last())
        self.assertEqual(list(self.trader.positions), [])

    def test_restart_keeps_the_trade_and_its_links(self):
        call, pos = self.open_trade()
        from paper_trader import PaperTrader
        fresh = PaperTrader(self.gw, self.bot.instruments, self.bot.notify)
        self.assertEqual(fresh.load(), 1)
        self.bot.trader = fresh
        self.post("Book half", reply_to=call)
        self.assertEqual(fresh.find_by_message(self.sref(call)).lots, 1)

    def test_edit_updates_sl_and_targets(self):
        call, pos = self.open_trade()
        msg = {"message_id": call, "date": clock.now().timestamp(), "edit_date": clock.now().timestamp(),
               "chat": {"id": int(SIGNAL)}, "from": {"id": 1},
               "text": "NIFTY 22800PE\nBuy CMP\nSl 85\nTgt 115 135"}
        self.bot.handle_update({"update_id": 999, "edited_message": msg})
        self.assertEqual((pos.sl, pos.targets), (85.0, [115.0, 135.0]))


class BuyAgainTests(Harness):
    CALL = "NIFTY 22800PE\nBuy Above 100\nSl 80\nTgt 120 140"

    def first_trade_stopped_out(self):
        self.gw.set(PE, 101.0)
        call = self.post(self.CALL)
        self.price(79.0)
        return call, self.only()

    def test_buy_again_repeats_the_call(self):
        call, first = self.first_trade_stopped_out()
        self.assertEqual(first.status, "CLOSED")
        again = self.post("Buy again", reply_to=call)
        second = self.trader.find_by_message(self.sref(again))
        self.assertIsNot(second, first)
        self.assertEqual((second.tradingsymbol, second.mode, second.level, second.sl, second.targets,
                          second.status, second.lots),
                         (first.tradingsymbol, "ABOVE", 100.0, 80.0, [120.0, 140.0], "PENDING", 2))
        self.assertIn("WAITING", self.tg.last())
        self.price(100.5)
        self.assertEqual((second.status, second.entry_price), ("OPEN", 100.5))

    def test_tags_on_the_original_call_reach_the_new_trade(self):
        call, first = self.first_trade_stopped_out()
        self.post("Buy again", reply_to=call)
        self.price(101.0)
        self.post("Book half", reply_to=call)
        second = self.trader.find_by_message(self.sref(call))
        self.assertIsNot(second, first)
        self.assertEqual((second.status, second.lots), ("OPEN", 1))

    def test_buy_again_while_open_is_refused(self):
        self.gw.set(PE, 101.0)
        call = self.post(self.CALL)
        self.post("Buy again", reply_to=call)
        self.assertEqual(len(self.trader.positions), 1)
        self.assertIn("still open", self.tg.last())

    def test_buy_again_on_the_bot_message_in_the_update_group(self):
        call, first = self.first_trade_stopped_out()
        bot_msg = self.tg.sent[-1]["id"]                  # the SL HIT message
        self.gw.set(PE, 101.0)
        self.post("Buy again", reply_to=bot_msg, chat=UPDATE)
        self.assertEqual(sorted(p.status for p in self.trader.positions.values()), ["CLOSED", "OPEN"])

    def test_buy_again_after_an_edit_uses_the_edited_call(self):
        call, first = self.first_trade_stopped_out()
        msg = {"message_id": call, "date": clock.now().timestamp(), "edit_date": clock.now().timestamp(),
               "chat": {"id": int(SIGNAL)}, "from": {"id": 1}, "text": "NIFTY 22800PE\nBuy CMP\nSl 60"}
        self.bot.handle_update({"update_id": 999, "edited_message": msg})
        self.gw.set(PE, 90.0)
        self.post("Buy again", reply_to=call)
        second = self.trader.find_by_message(self.sref(call))
        self.assertEqual((second.status, second.mode, second.sl), ("OPEN", "CMP", 60.0))

    def test_buy_again_above_uses_the_new_level(self):
        call, first = self.first_trade_stopped_out()
        self.gw.set(PE, 101.0)
        self.post("Buy again above 110", reply_to=call)
        second = self.trader.find_by_message(self.sref(call))
        self.assertIsNot(second, first)
        self.assertEqual((second.status, second.mode, second.level, second.sl, second.targets),
                         ("PENDING", "ABOVE", 110.0, 80.0, [120.0, 140.0]))
        self.assertIn("Buy Above 110", second.signal_text)
        self.assertIn("WAITING", self.tg.last())
        self.price(110.5)
        self.assertEqual((second.status, second.entry_price), ("OPEN", 110.5))

    def test_buy_again_near_buys_inside_the_band(self):
        call, first = self.first_trade_stopped_out()
        self.gw.set(PE, 101.0)
        self.post("Buy again near 100", reply_to=call)
        second = self.trader.find_by_message(self.sref(call))
        self.assertIsNot(second, first)
        self.assertEqual((second.status, second.mode, second.level, second.entry_price, second.sl),
                         ("OPEN", "NEAR", 100.0, 101.0, 80.0))

    def test_buy_again_cmp_buys_now(self):
        call, first = self.first_trade_stopped_out()
        self.gw.set(PE, 95.0)
        self.post("Buy again CMP", reply_to=call)
        second = self.trader.find_by_message(self.sref(call))
        self.assertIsNot(second, first)
        self.assertEqual((second.status, second.mode, second.level, second.entry_price, second.sl, second.targets),
                         ("OPEN", "CMP", None, 95.0, 80.0, [120.0, 140.0]))
        self.assertIn("Buy CMP", second.signal_text)


class CommandTests(Harness):
    def test_op_lists_open_and_waiting(self):
        self.gw.set(PE, 100.0)
        self.post("NIFTY 22800PE\nBuy CMP\nSl 80\nTgt 120")
        self.gw.set("BFO:SENSEX26OCT73000CE", 300.0)
        self.post("SENSEX 73000CE\nBuy Near 200")
        self.price(110.0)
        self.post("/op", chat=UPDATE)
        report = self.tg.last()
        self.assertIn("OPEN POSITIONS (2)", report)
        self.assertIn("NIFTY 22800 PE", report)
        self.assertIn("+₹1,300", report)                     # (110 − 100) × 130
        self.assertIn("waiting · Buy Near 200", report)

    def test_pnl(self):
        self.gw.set(PE, 100.0)
        call = self.post("NIFTY 22800PE\nBuy CMP\nSl 80")
        self.gw.set(PE, 110.0)
        self.post("Book half", reply_to=call)
        self.post("/pnl", chat=UPDATE)
        report = self.tg.last()
        self.assertIn("booked +₹610", report)                # 650 − 2 orders × ₹20
        self.assertIn("open +₹650", report)
        self.assertIn("Total +₹1,260", report)

    def test_trades_sends_the_csv(self):
        self.post("/trades", chat=UPDATE)
        self.assertIn("No closed trades yet", self.tg.last())
        self.gw.set(PE, 100.0)
        call = self.post("NIFTY 22800PE\nBuy CMP\nSl 80")
        self.gw.set(PE, 110.0)
        self.post("Exit", reply_to=call)
        self.post("/trades", chat=UPDATE)
        sent = self.tg.sent[-1]
        self.assertTrue(sent["file"].startswith("trades_") and sent["file"].endswith(".csv"))
        self.assertIn(b"NIFTY26OCT22800PE", sent["content"])
        self.assertIn("1 closed trades", sent["text"])

    def test_commands_only_in_the_update_group(self):
        for command in ("/op", "/pnl", "/trades"):
            self.post(command)
        self.assertEqual(self.tg.sent, [])


class TwoGroupTests(Harness):
    CALL = "NIFTY 22800PE\nBuy CMP\nSl 80"

    def test_updates_point_at_the_call(self):
        self.gw.set(PE, 100.0)
        call = self.post(self.CALL)
        self.assertEqual(self.tg.sent[-1]["reply_to"], self.sref(call))

    def test_calls_in_the_update_group_are_ignored(self):
        self.gw.set(PE, 100.0)
        self.post(self.CALL, chat=UPDATE)
        self.assertEqual((self.trader.positions, self.tg.sent), ({}, []))

    def test_follow_up_on_the_bot_message_in_the_update_group(self):
        self.gw.set(PE, 100.0)
        self.post(self.CALL)
        self.post("Book half", reply_to=self.tg.sent[-1]["id"], chat=UPDATE)
        self.assertEqual(self.only().lots, 1)

    def test_same_message_number_in_both_groups_does_not_mix_trades(self):
        self.gw.set(PE, 100.0)
        self.gw.set("NFO:NIFTY26OCT22800CE", 120.0)
        self.tg.next_id = 104
        first = self.post(self.CALL)                         # trade A, signal msg 101
        trade_a = self.trader.find_by_message(self.sref(first))
        self.assertEqual(self.tg.sent[-1]["id"], 105)        # BOUGHT A is update msg 105
        self.msg_id = 104
        second = self.post("NIFTY 22800CE\nBuy CMP\nSl 90")
        trade_b = self.trader.find_by_message(self.sref(second))
        self.assertEqual(second, 105)                        # trade B is signal msg 105
        self.post("Exit", reply_to=105, chat=UPDATE)
        self.assertEqual((trade_a.status, trade_b.status), ("CLOSED", "OPEN"))
        self.post("Exit", reply_to=105)
        self.assertEqual(trade_b.status, "CLOSED")


if __name__ == "__main__":
    unittest.main()
