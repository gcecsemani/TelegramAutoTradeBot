"""
Offline tests for signal_parser: exactly the message formats the group uses.

    python3 -m unittest tests.test_parser -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import signal_parser  # noqa: E402
from signal_parser import parse  # noqa: E402

signal_parser.register_names(["BAJAJ-AUTO", "BANKNIFTY", "M&M", "RELIANCE"])


def call(text):
    parsed = parse(text)
    assert parsed.kind == "BUY", f"{text!r} -> {parsed.kind} {parsed.error}"
    c = parsed.call
    return (c.symbol, c.strike, c.opt_type, c.mode, c.level, c.sl, c.no_sl, c.targets)


def follow(text):
    parsed = parse(text, is_reply=True)
    assert parsed.kind == "FOLLOW_UP", f"{text!r} -> {parsed.kind}"
    f = parsed.follow_up
    return (f.exit, f.sl, f.sl_value, f.targets)


class BuyCallTests(unittest.TestCase):
    def test_sample_calls(self):
        cases = [
            ("NIFTY 22800PE\nBuy Above 100\nSl 80\nTgt 120 140 160++",
             ("NIFTY", 22800, "PE", "ABOVE", 100, 80, False, [120, 140, 160])),
            ("NIFTY 22800PE\nBuy Near 90\nSl 70\nTgt 110 130 150++",
             ("NIFTY", 22800, "PE", "NEAR", 90, 70, False, [110, 130, 150])),
            ("NIFTY 22800PE\nBuy CMP\nSl 70\nTgt 110 130 150++",
             ("NIFTY", 22800, "PE", "CMP", None, 70, False, [110, 130, 150])),
            ("NIFTY 22800PE\nBuy Above 100", ("NIFTY", 22800, "PE", "ABOVE", 100, None, False, [])),
            ("NIFTY 22800PE\nBuy Near 90", ("NIFTY", 22800, "PE", "NEAR", 90, None, False, [])),
            ("NIFTY 22800PE\nBuy CMP", ("NIFTY", 22800, "PE", "CMP", None, None, False, [])),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(call(text), expected)

    def test_same_layout_other_instruments(self):
        self.assertEqual(call("SENSEX 73000CE\nBuy Near 210\nSl 180")[:5], ("SENSEX", 73000, "CE", "NEAR", 210))
        self.assertEqual(call("CRUDEOIL 10000CE\nBuy CMP")[0], "CRUDEOIL")
        self.assertEqual(call("NATURALGAS 300PE\nBuy Above 14.5\nSl 11")[4:6], (14.5, 11))
        for text in ("NG 305ce\nBuy CMP", "ng 305 ce\nBuy CMP", "NG305CE\nBuy CMP"):
            self.assertEqual(call(text)[:3], ("NATURALGAS", 305, "CE"), text)
        self.assertEqual(call("NG mini 305ce\nBuy CMP")[0], "NATGASMINI")
        self.assertEqual(call("GOLDM 143000PE\nBuy Near 1450")[0], "GOLDM")
        self.assertEqual(call("SILVERM 195000 PE\nBuy CMP")[0], "SILVERM")
        self.assertEqual(call("BANK NIFTY 54200PE\nBuy CMP")[0], "BANKNIFTY")
        self.assertEqual(call("Bajaj auto 10000 PE\nBuy Near 105")[0], "BAJAJ-AUTO")
        self.assertEqual(call("nifty 22800 pe\nbuy above 100\nsl 80\ntgt 120 140 160++")[3], "ABOVE")

    def test_no_sl_in_a_call(self):
        self.assertTrue(call("NIFTY 22800PE\nBuy CMP\nHero zero")[6])
        self.assertTrue(call("NIFTY 22800PE\nBuy CMP\nSl 0")[6])

    def test_everything_else_is_ignored(self):
        for text in ("good morning", "NIFTY 22800PE", "Nifty looks weak", "Buy CMP", "SL 70", "Book",
                     "Tgt 110", "Sell NIFTY 22800PE", "NIFTY fut buy cmp"):
            self.assertEqual(parse(text).kind, "NONE", text)

    def test_broken_calls_are_explained(self):
        for text, words in (("NIFTY 22800PE\nBuy 100", "Buy Above"),
                            ("Nifty 22700 Pe buy @ 85 only", "Buy Above"),
                            ("NIFTY 22800PE\nBuy CMP\nSell 22900PE", "one option"),
                            ("XYZ 22800PE\nBuy CMP", "not recognised")):
            parsed = parse(text)
            self.assertEqual(parsed.kind, "INVALID", text)
            self.assertIn(words, parsed.error, text)


class FollowUpTests(unittest.TestCase):
    def test_sl(self):
        self.assertEqual(follow("SL 70")[1:3], ("PRICE", 70))
        for text in ("SL to cost", "SL at cost", "SL at CTC", "SL CTC"):
            self.assertEqual(follow(text)[1], "COST", text)
        for n in (1, 2, 3):
            self.assertEqual(follow(f"SL to t{n}")[1:3], ("TARGET", n))
        for text in ("No sl", "Hero zero", "Hero to Zero"):
            self.assertEqual(follow(text)[1], "NONE", text)

    def test_exits(self):
        for text in ("Exit", "Book", "Ignore", "Avoid", "Cancel", "Close", "Sell", "SL hit", "Book full", "Book fully"):
            self.assertEqual(follow(text)[0], "FULL", text)
        for text in ("Book half", "Book 50", "Book 50%"):
            self.assertEqual(follow(text)[0], "HALF", text)

    def test_targets(self):
        self.assertEqual(follow("Tgt 110")[3], [110])
        self.assertEqual(follow("Tgt 110 120 130")[3], [110, 120, 130])
        self.assertEqual(follow("Target 110")[3], [110])
        self.assertEqual(follow("Target 110 120 130")[3], [110, 120, 130])

    def test_buy_again(self):
        for text in ("Buy again", "buy again", "BUY AGAIN 🔥"):
            parsed = parse(text, is_reply=True)
            self.assertEqual((parsed.kind, parsed.follow_up.buy_again, parsed.follow_up.entry_mode),
                             ("FOLLOW_UP", True, ""), text)
        cases = (
            ("Buy again above 100", "ABOVE", 100),
            ("buy again near 100", "NEAR", 100),
            ("Buy again CMP", "CMP", None),
            ("Buy again above: 100.5", "ABOVE", 100.5),
            ("Buy again near - 90", "NEAR", 90),
        )
        for text, mode, level in cases:
            parsed = parse(text, is_reply=True)
            self.assertEqual((parsed.kind, parsed.follow_up.entry_mode, parsed.follow_up.entry_level),
                             ("FOLLOW_UP", mode, level), text)
        for text in ("Buy again above", "Buy again near", "Buy again above 0"):
            self.assertEqual(parse(text, is_reply=True).kind, "INVALID", text)
        self.assertEqual(parse("Buy again CMP").kind, "NONE")      # untagged: ignored

    def test_commands(self):
        from signal_parser import parse_command
        self.assertEqual([parse_command(c) for c in ("/op", "/OP", "/pnl@NiftyTradeSignalBot", "/trades")],
                         ["OP", "OP", "PNL", "TRADES"])
        for text in ("/op now", "op", "/status", "pnl"):
            self.assertIsNone(parse_command(text), text)

    def test_exit_all(self):
        self.assertEqual(parse("Exit all").kind, "EXIT_ALL")
        self.assertEqual(parse("exit all", is_reply=True).kind, "EXIT_ALL")

    def test_other_replies_ignored(self):
        for text in ("nice", "good move", "don't exit", "what now?", "Hold"):
            self.assertEqual(parse(text, is_reply=True).kind, "NONE", text)


WATCH = "Sensex 72800 PE |  | ADD TO WATCHLIST"


class TaggedBuyTests(unittest.TestCase):
    def test_entry_comes_from_the_reply_and_the_option_from_the_tag(self):
        cases = (
            ("Buy CMP", "CMP", None),
            ("buy above 120", "ABOVE", 120),
            ("Buy Near - 110", "NEAR", 110),
            ("Buy above: 120.5", "ABOVE", 120.5),
        )
        for text, mode, level in cases:
            parsed = parse(text, is_reply=True, tagged=WATCH)
            self.assertEqual((parsed.kind, parsed.from_tag, parsed.call.symbol, parsed.call.strike,
                              parsed.call.opt_type, parsed.call.mode, parsed.call.level),
                             ("BUY", True, "SENSEX", 72800, "PE", mode, level), text)

    def test_sl_and_targets_on_the_reply(self):
        parsed = parse("Buy CMP\nSl 80\nTgt 120 140", is_reply=True, tagged=WATCH)
        call = parsed.call
        self.assertEqual((parsed.kind, call.sl, call.no_sl, call.targets), ("BUY", 80, False, [120, 140]))
        self.assertEqual(call.as_text(), "SENSEX 72800 PE\nBuy CMP\nSl 80\nTgt 120 140")

    def test_above_and_near_need_a_price(self):
        for text in ("Buy above", "Buy near", "BUY ABOVE", "Buy 100"):
            parsed = parse(text, is_reply=True, tagged=WATCH)
            self.assertEqual(parsed.kind, "INVALID", text)
            self.assertIn("Buy Above", parsed.error, text)

    def test_tag_must_name_one_option(self):
        parsed = parse("Buy CMP", is_reply=True, tagged="good morning")
        self.assertEqual(parsed.kind, "INVALID")
        self.assertIn("no option", parsed.error)
        parsed = parse("Buy CMP", is_reply=True, tagged="NIFTY 22800PE and 22900CE")
        self.assertIn("one option", parsed.error)
        self.assertEqual(parse("Buy CMP", is_reply=True).kind, "NONE")

    def test_follow_ups_still_win_when_it_is_not_an_entry(self):
        self.assertEqual(parse("SL 70", is_reply=True, tagged=WATCH).kind, "FOLLOW_UP")
        self.assertEqual(parse("Exit", is_reply=True, tagged=WATCH).kind, "FOLLOW_UP")
        again = parse("Buy again CMP", is_reply=True, tagged=WATCH)
        self.assertEqual((again.kind, again.follow_up.entry_mode), ("FOLLOW_UP", "CMP"))


if __name__ == "__main__":
    unittest.main()
