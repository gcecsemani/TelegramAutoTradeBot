"""
bot.py
======
Reads your buy calls and follow-ups from the signal group, drives the paper
trader, and posts every update to the update group. Only the formats in
signal_parser.py are acted on; everything else is ignored.

    main thread     long-polls Telegram and handles each message in order
    monitor thread  SL, buy levels, targets and square-off, every second

A reply is matched to its trade by the message it replies to: your call in
the signal group, or any bot message about that trade in the update group.
A reply of Buy Above / Near / CMP on a message that only names the option
(a watchlist line) opens that trade.
"""

from __future__ import annotations

import csv
import json
import os
import threading
from typing import Optional

import config
from core import clock
from core.logging_setup import get
from data.instruments import InstrumentMaster
from data.kite_gateway import KiteGateway, TokenExpired
from notify.telegram import TelegramClient, TelegramError, esc, ref
from paper_trader import OPEN, PENDING, PaperTrader, money
from signal_parser import BuyCall, parse, parse_command

log = get("bot")

IGNORE_AFTER_SEC = 6 * 3600     # older calls from an offline period get no reply at all


class SignalBot:
    def __init__(self) -> None:
        self.tg = TelegramClient()
        self.gw = KiteGateway()
        self.instruments = InstrumentMaster(self.gw)
        self.trader = PaperTrader(self.gw, self.instruments, self.notify)
        self._stop = threading.Event()
        self._monitor: Optional[threading.Thread] = None
        self.stop_reason = "stopped"
        self.offset = self._load_offset()

    # ─── Telegram out (update group) ──────────────────────────────────────────

    def notify(self, text: str, reply_to: Optional[str] = None, position_id: Optional[str] = None) -> None:
        on_sent = None
        if position_id:
            on_sent = lambda sent_ref: self.trader.link(position_id, sent_ref)  # noqa: E731
        self.tg.send(text, reply_to, on_sent)

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> bool:
        problems = config.validate()
        for problem in problems:
            log.error("• %s", problem)
        if problems:
            return False
        try:
            if not self.gw.check_connection():
                log.error("Kite connection failed — run python3 login_zerodha.py")
                return False
            if not self.instruments.ensure_loaded():
                return False
        except TokenExpired:
            log.error("Kite access token expired — run python3 login_zerodha.py")
            return False

        restored = self.trader.load()
        access = self.tg.read_access()
        if access:
            log.error(access)
        self.tg.start()
        self._startup_message(restored, access)
        self._monitor = threading.Thread(target=self._monitor_loop, name="monitor", daemon=True)
        self._monitor.start()
        return True

    def _startup_message(self, restored: int, access: str) -> None:
        expiries = []
        for symbol in config.SYMBOLS:
            expiry = self.instruments.expiry(symbol)
            if expiry:
                expiries.append(f"{symbol} {expiry.strftime('%d %b')}")
        lines = [
            "🟢 <b>SIGNAL BOT ONLINE · PAPER</b>",
            f"{clock.now().strftime('%d %b %Y %H:%M')} IST · Kite {esc(self.gw.profile_name)}",
            "",
            "Nearest expiries: " + esc(", ".join(expiries)),
            f"Open / waiting trades restored: {restored}" if restored else "No open trades.",
        ]
        if access:
            lines += ["", f"⚠️ <b>I cannot read normal messages yet.</b>\n{esc(access)}"]
        lines += ["", f"Reading calls from group {esc(', '.join(sorted(self.tg.signal_chats)))}: "
                      f"Buy Above / Near / CMP, {config.TRADE['lots']} lots. "
                      "Tag a watchlist line (SENSEX 72800 PE) with Buy Above 120 / Buy Near 110 / Buy CMP to take it. "
                      "Tag a call (or my trade "
                      "message here) with Exit, Book half, SL …, Tgt …, Buy again, Buy again Above / Near / CMP; "
                      "send Exit all to close "
                      "everything.", "Here: /op open positions · /pnl today's P&amp;L · /trades the trades file."]
        self.notify("\n".join(lines))

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                self._poll_once()
        except KeyboardInterrupt:
            self.stop_reason = "stopped from the keyboard"
        finally:
            self.shutdown()

    def stop(self, reason: str) -> None:
        self.stop_reason = reason
        self._stop.set()

    def shutdown(self) -> None:
        self._stop.set()
        if self._monitor is not None:
            self._monitor.join(timeout=5.0)
        self.trader.save()
        open_count = len(self.trader.active())
        tail = f"\n{open_count} open / waiting trade(s) resume when I restart." if open_count else ""
        self.notify(f"🔴 <b>SIGNAL BOT STOPPED</b>\n{esc(self.stop_reason)}\n\n{self.trader.pnl_text()}{tail}")
        self.tg.stop()
        log.info("Stopped: %s", self.stop_reason)

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            busy = False
            try:
                busy = self.trader.tick()
            except TokenExpired:
                self._token_expired()
                return
            except Exception as exc:
                log.exception("monitor error: %s", exc)
            idle = not self.trader.active()
            self._stop.wait(config.ENGINE["monitor_interval_sec"] if busy or idle else 15.0)

    def _token_expired(self) -> None:
        log.critical("Kite access token expired")
        self.notify("🚨 <b>Kite login expired.</b> Run <code>python3 login_zerodha.py</code> and restart me. "
                    "Open trades are saved and resume on restart.")
        self.stop("Kite login expired")

    # ─── Telegram in ──────────────────────────────────────────────────────────

    def _load_offset(self) -> Optional[int]:
        try:
            with open(config.PATHS["telegram_offset"], "r", encoding="utf-8") as fh:
                return int(json.load(fh)["offset"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_offset(self) -> None:
        path = config.PATHS["telegram_offset"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"offset": self.offset}, fh)
        except OSError as exc:
            log.error("Could not save the Telegram offset: %s", exc)

    def _poll_once(self) -> None:
        try:
            updates = self.tg.get_updates(self.offset, config.ENGINE["telegram_poll_sec"])
        except TelegramError as exc:
            if exc.code == 409:
                log.error("Another program is reading this bot's messages (a second copy of the bot, "
                          "or a webhook). Stop it — retrying in 10s.")
            elif exc.code == 401:
                self.stop("Telegram rejected TELEGRAM_BOT_TOKEN")
                return
            else:
                log.error("Telegram getUpdates failed: %s", exc)
            self._stop.wait(10.0)
            return
        except Exception as exc:
            log.warning("Telegram poll failed: %s", exc)
            self._stop.wait(3.0)
            return

        for update in updates:
            self.offset = int(update["update_id"]) + 1
            try:
                self.handle_update(update)
            except TokenExpired:
                self._token_expired()
            except Exception as exc:
                log.exception("Could not handle update %s: %s", update.get("update_id"), exc)
            self._save_offset()
            if self._stop.is_set():
                return

    def handle_update(self, update: dict) -> None:
        edited = "edited_message" in update or "edited_channel_post" in update
        msg = (update.get("message") or update.get("edited_message")
               or update.get("channel_post") or update.get("edited_channel_post"))
        if not msg:
            return
        chat = str((msg.get("chat") or {}).get("id"))
        if msg.get("migrate_to_chat_id"):
            self.tg.migrated(chat, str(msg["migrate_to_chat_id"]))
            return
        in_signal = chat in self.tg.signal_chats
        if not in_signal and chat != self.tg.update_chat:
            return
        sender = msg.get("from") or {}
        if sender.get("is_bot"):
            return
        # Channel posts are published as the channel, so they have no user id.
        if (config.TELEGRAM_ALLOWED_USER_IDS and sender.get("id")
                and str(sender.get("id")) not in config.TELEGRAM_ALLOWED_USER_IDS):
            return
        text = msg.get("text") or msg.get("caption") or ""
        if not text.strip():
            return

        msg_ref = ref(chat, msg["message_id"])
        replied = msg.get("reply_to_message") or {}
        reply_ref = ref(chat, replied["message_id"]) if replied else None
        sent = clock.from_unix(msg.get("edit_date") or msg.get("date") or 0)
        age = (clock.now() - sent).total_seconds()
        fresh = age <= config.TRADE["max_signal_age_sec"]
        who = sender.get("first_name") or (msg.get("sender_chat") or {}).get("title") or "?"
        log.info("MSG %s%s%s from %s: %s", msg_ref, " (edited)" if edited else "",
                 f" ↩ {reply_ref}" if reply_ref else "", who,
                 text.replace("\n", " | "))

        if edited:
            if in_signal and not reply_ref:
                self._handle_edit(msg_ref, text, age)
            return

        command = parse_command(text)
        if command:
            if not in_signal and fresh:            # /op /pnl /trades answer in the update group only
                self._command(command, msg_ref)
            return

        tagged = replied.get("text") or replied.get("caption") or ""
        parsed = parse(text, is_reply=reply_ref is not None, tagged=tagged)
        log.info("   → %s%s", parsed.kind, f" ({parsed.error})" if parsed.error else "")

        if parsed.kind == "EXIT_ALL":
            if fresh:
                self.trader.exit_all(msg_ref)
            return
        if parsed.kind == "FOLLOW_UP":
            pos = self.trader.find_by_message(reply_ref)
            if pos is not None and parsed.follow_up.buy_again and not fresh:
                if age < IGNORE_AFTER_SEC:
                    self.notify(f"⌛ <b>Not bought again</b> — your message is {age / 60:.0f} min old "
                                f"(I was offline). Send it again if it is still valid.\n{_said(text)}", msg_ref)
            elif pos is not None:
                self.trader.follow_up(pos, parsed.follow_up, msg_ref)
            elif fresh and in_signal:
                self.notify("ℹ️ No trade is linked to the message you tagged. Tag the buy call itself "
                            f"or my ✅ BOUGHT message.\n{_said(text)}", msg_ref)
            return
        if not in_signal:
            return                      # the update group takes follow-ups and Exit all only
        if parsed.kind == "BUY":
            extra = []
            stored = text
            if parsed.from_tag:
                pos = self.trader.find_by_message(reply_ref)
                if pos is not None and pos.status in (OPEN, PENDING) and fresh:
                    what = "still open" if pos.status == OPEN else "still waiting to buy"
                    self.notify(f"ℹ️ {esc(pos.label)} is {what} on the message you tagged. "
                                f"Exit it first, or send the full call as its own message.\n{_said(text)}", msg_ref)
                    return
                stored = parsed.call.as_text()
                extra = [reply_ref]
            self._handle_call(parsed.call, msg_ref, stored, age, extra)
        elif parsed.kind == "INVALID" and fresh:
            self.notify(f"❓ <b>Not taken</b> — {esc(parsed.error)}\n{_said(text)}", msg_ref)

    def _handle_call(self, call: BuyCall, msg_ref: str, text: str, age: float, extra_refs=()) -> None:
        if age > config.TRADE["max_signal_age_sec"]:
            log.info("   call is %.0f s old — not taken", age)
            if age < IGNORE_AFTER_SEC:
                self.notify(f"⌛ <b>Not taken</b> — this call is {age / 60:.0f} min old "
                            f"(I was offline). Repost it if it is still valid.\n{_said(text)}", msg_ref)
            return
        self.trader.open_call(call, msg_ref, text, extra_refs=extra_refs)

    def _handle_edit(self, msg_ref: str, text: str, age: float) -> None:
        parsed = parse(text)
        pos = self.trader.find_by_message(msg_ref)
        if pos is not None:
            if parsed.kind == "BUY":
                self.trader.update_from_edit(pos, parsed.call, msg_ref, text)
            return
        # An edit that fixes a call the bot could not take is treated as a new call.
        if age > config.TRADE["max_signal_age_sec"]:
            return
        if parsed.kind == "BUY":
            self._handle_call(parsed.call, msg_ref, text, age)
        elif parsed.kind == "INVALID":
            self.notify(f"❓ <b>Not taken</b> — {esc(parsed.error)}\n{_said(text)}", msg_ref)


    def _command(self, command: str, msg_ref: str) -> None:
        log.info("   → /%s", command.lower())
        if command == "OP":
            self.notify(self.trader.open_positions_report(), msg_ref)
        elif command == "PNL":
            self.notify(self.trader.pnl_report(), msg_ref)
        elif command == "TRADES":
            path = config.PATHS["trade_journal"]
            try:
                with open(path, "rb") as fh:
                    content = fh.read()
                with open(path, newline="", encoding="utf-8") as fh:
                    rows = list(csv.DictReader(fh))
            except OSError:
                rows, content = [], b""
            if not rows:
                self.notify("📎 No closed trades yet — trades.csv is empty.", msg_ref)
                return
            total = sum(float(row.get("pnl") or 0) for row in rows)
            first, last = rows[0]["exit_time"][:10], rows[-1]["exit_time"][:10]
            self.tg.send_document(
                f"trades_{clock.datestamp()}.csv", content,
                f"📎 <b>trades.csv</b> · {len(rows)} closed trades · {esc(first)} → {esc(last)}\n"
                f"Net P&amp;L {money(total)}", msg_ref)


def _said(text: str) -> str:
    """Your message, shortened, so a notice in the update group shows what it answers."""
    line = " · ".join(part.strip() for part in text.strip().splitlines() if part.strip())
    return f"<i>“{esc(line[:120])}{'…' if len(line) > 120 else ''}”</i>"
