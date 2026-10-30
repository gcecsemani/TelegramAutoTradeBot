"""
main.py
=======
    python3 main.py                          signal book (paper). Copies go to portal accounts
    python3 -m portal                        register, link a broker, daily token, risk limits
    python3 main.py --check                  check .env, Kite, instruments and Telegram access
    python3 main.py --parse "Nifty 22600 CE Buy near - 180 SL - 165"
                                             show how a message is read (offline)
    python3 main.py --parse "book" --reply
                                             same, as a reply to a trade
    python3 main.py --parse "Buy CMP" --reply --tagged "Sensex 72800 PE"
                                             a buy reply on a watchlist line
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from dataclasses import asdict


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram signal → paper trading bot")
    parser.add_argument("--check", action="store_true", help="validate setup, then exit")
    parser.add_argument("--parse", metavar="TEXT", help="show how TEXT is read, then exit")
    parser.add_argument("--reply", action="store_true", help="with --parse: treat TEXT as a reply")
    parser.add_argument("--tagged", default="", help="with --parse --reply: text of the message being tagged")
    parser.add_argument("--log-level", help="DEBUG / INFO / WARNING")
    return parser.parse_args()


_args = _parse_args()
if _args.log_level:
    os.environ["LOG_LEVEL"] = _args.log_level

import config                                                     # noqa: E402
from core import clock                                            # noqa: E402
from core.logging_setup import setup                              # noqa: E402

log = setup("main")


def run_parse(text: str, reply: bool, tagged: str = "") -> int:
    from signal_parser import parse

    parsed = parse(text, is_reply=reply, tagged=tagged)
    print(json.dumps(asdict(parsed), indent=2))
    return 0


def run_check() -> int:
    from data.instruments import InstrumentMaster
    from data.kite_gateway import KiteGateway, TokenExpired
    from notify.telegram import TelegramClient

    problems = config.validate()
    for problem in problems:
        log.error("• %s", problem)
    if problems:
        return 1

    ok = True
    try:
        gateway = KiteGateway()
        if not gateway.check_connection():
            log.error("Kite connection failed — run python3 login_zerodha.py")
            return 1
    except TokenExpired:
        log.error("Kite access token expired — run python3 login_zerodha.py")
        return 1

    instruments = InstrumentMaster(gateway)
    if not instruments.ensure_loaded():
        return 1
    for symbol, cfg in config.SYMBOLS.items():
        expiry = instruments.expiry(symbol)
        if expiry is None:
            log.error("%-10s no option contracts", symbol)
            ok = False
            continue
        sample = instruments.option(symbol, instruments.strikes(symbol)[0], "CE")
        log.info("%-10s %s  expiry %s  qty per lot %s  session %s",
                 symbol, cfg["exchange"], expiry.strftime("%d-%b"),
                 sample.multiplier if sample else "?", clock.session_text(cfg["exchange"]))

    telegram = TelegramClient()
    access = telegram.read_access()
    if access:
        log.error("Telegram: %s", access)
        ok = False
    else:
        log.info("Telegram: the bot can read the group")
    return 0 if ok else 1


def main() -> int:
    if _args.parse is not None:
        return run_parse(_args.parse, _args.reply, _args.tagged)
    log.info("Telegram signal bot · PAPER · %s", clock.now().strftime("%Y-%m-%d %H:%M:%S"))
    if _args.check:
        return run_check()

    from bot import SignalBot

    bot = SignalBot()
    if not bot.start():
        log.critical("Bot failed to start")
        return 1

    def _terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
