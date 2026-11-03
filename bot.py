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
(a watchlist line) opens that trade. The same reply on a call that is still
waiting replaces that entry: Buy CMP buys now, and Buy Near / Buy Above buy
when the new rule is met. An open trade is left unchanged.
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from typing import Optional

import config
from core import clock
from core.logging_setup import get
from core.quote_login import apply_if_changed
from data.instruments import InstrumentMaster
from data.kite_gateway import KiteGateway, TokenExpired
from execution.engine import CopyEngine
from notify.telegram import TelegramClient, TelegramError, esc, ref
from paper_trader import JOURNAL_COLUMNS, OPEN, PENDING, PaperTrader, money, one_lot_rows
from signal_parser import BuyCall, parse, parse_command, parse_entry_reply

log = get("bot")

IGNORE_AFTER_SEC = 6 * 3600     # older calls from an offline period get no reply at all


class SignalBot:
    def __init__(self) -> None:
        self.tg = TelegramClient()
        self.gw = KiteGateway()
        self.instruments = InstrumentMaster(self.gw)
        self.copy_engine = CopyEngine(
            notify=lambda text: self.notify(text, None, None),
            notify_account=self._notify_owner,
        )
        self.trader = PaperTrader(
            self.gw, self.instruments, self.notify, copy=self.copy_engine.handle,
            on_closed=self._publish_trades,
        )
        self._stop = threading.Event()
        self._monitor: Optional[threading.Thread] = None
        self._last_catch_up = 0.0
        self._quote_token = config.ZERODHA_ACCESS_TOKEN
        self._quotes_down = False
        self.stop_reason = "stopped"
        self.offset = self._load_offset()

    # ─── Telegram out (update group) ──────────────────────────────────────────

    def notify(self, text: str, reply_to: Optional[str] = None, position_id: Optional[str] = None) -> None:
        on_sent = None
        if position_id:
            on_sent = lambda sent_ref: self.trader.link(position_id, sent_ref)  # noqa: E731
        self.tg.send(text, reply_to, on_sent)

    def _notify_owner(self, account: dict, text: str) -> None:
        """A linked account's trade update goes to that owner's chat, not the group."""
        chat = str(account.get("owner_telegram") or "").strip()
        if not chat:
            log.warning("No Telegram chat saved for %s; trade update not sent",
                        account.get("label") or account.get("id"))
            return
        self.tg.send(text, chat_id=chat)

    def _private_chat(self, chat: str) -> None:
        self.tg.send(
            f"This chat id is <code>{esc(chat)}</code>.\n"
            "Paste it into <b>Telegram chat id</b> on your account in the portal. "
            "Trade updates for your linked account come here.",
            chat_id=chat,
        )

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
        self.copy_engine.recover(list(self.trader.positions.values()))
        self.copy_engine.start()
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
            esc(self.copy_engine.status_line()),
            "",
            "Nearest expiries: " + esc(", ".join(expiries)),
            f"Open / waiting trades restored: {restored}" if restored else "No open trades.",
        ]
        if access:
            lines += ["", f"⚠️ <b>I cannot read normal messages yet.</b>\n{esc(access)}"]
        book_lots = int(config.TRADE["lots"])
        lot_word = "lot" if book_lots == 1 else "lots"
        lines += ["", f"Reading calls from group {esc(', '.join(sorted(self.tg.signal_chats)))}: "
                      f"Buy Above / Near / CMP, {book_lots} {lot_word}. "
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
        self.copy_engine.stop()
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
            if self._quotes_down:
                if self._take_new_quote_token():
                    self._quotes_down = False
                    self.instruments.ensure_loaded()
                    self.notify("✅ Quote login is saved. Live prices are back.")
                self._stop.wait(5.0)
                continue
            busy = False
            try:
                self._take_new_quote_token()
                busy = self.trader.tick()
            except TokenExpired:
                self._quotes_paused()
                continue
            except Exception as exc:
                log.exception("monitor error: %s", exc)
            self._catch_up_copies()
            idle = not self.trader.active()
            self._stop.wait(config.ENGINE["monitor_interval_sec"] if busy or idle else 15.0)

    def _catch_up_copies(self) -> None:
        """A live leg whose signal trade is already closed gets another exit."""
        now = time.monotonic()
        if now - self._last_catch_up < 15:
            return
        self._last_catch_up = now
        try:
            self.copy_engine.catch_up(self.trader.snapshot())
        except Exception:
            log.exception("Could not catch up live exits")

    def _take_new_quote_token(self) -> bool:
        try:
            fresh = apply_if_changed(self.gw, self._quote_token)
        except Exception as exc:
            log.error("Could not read the quote token: %s", exc)
            return False
        if fresh == self._quote_token:
            return False
        try:
            ok = self.gw.check_connection()
        except TokenExpired:
            self._quote_token = fresh
            return False
        if not ok:
            return False
        self._quote_token = fresh
        return True

    def _quotes_paused(self) -> None:
        if self._take_new_quote_token():
            self._quotes_down = False
            return
        if self._quotes_down:
            return
        self._quotes_down = True
        log.critical("Kite quote token expired")
        self.notify("🚨 <b>Quote login expired.</b> Open <b>Quote login</b> in the portal, "
                    "sign in to Zerodha, and paste the redirect URL. I keep reading Telegram "
                    "and resume prices when that is saved. No restart.")

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
                self._quotes_paused()
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
        sender = msg.get("from") or {}
        if sender.get("is_bot"):
            return
        chat_type = (msg.get("chat") or {}).get("type") or ""
        if chat_type == "private" and not in_signal and chat != self.tg.update_chat:
            self._private_chat(chat)
            return
        if not in_signal and chat != self.tg.update_chat:
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
            # Answer in the update group. One shared group is both, so this still runs there.
            if chat == self.tg.update_chat and fresh:
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
        if self._retarget_if_waiting(text, parsed, reply_ref, msg_ref, age, fresh):
            return
        if not in_signal:
            return                      # the update group takes follow-ups and Exit all only
        if parsed.kind == "BUY":
            extra = []
            stored = text
            if parsed.from_tag:
                pos = self.trader.find_by_message(reply_ref)
                if pos is not None and pos.status == OPEN and fresh:
                    self.notify(f"ℹ️ {esc(pos.label)} is still open on the message you tagged. "
                                f"Exit it first, or send the full call as its own message.\n{_said(text)}", msg_ref)
                    return
                stored = parsed.call.as_text()
                extra = [reply_ref]
            self._handle_call(parsed.call, msg_ref, stored, age, extra)
        elif parsed.kind == "INVALID" and fresh:
            self.notify(f"❓ <b>Not taken</b> — {esc(parsed.error)}\n{_said(text)}", msg_ref)

    def _retarget_if_waiting(self, text: str, parsed, reply_ref: Optional[str], msg_ref: str,
                             age: float, fresh: bool) -> bool:
        """Buy Near / Above / CMP tagged onto a call that is still waiting
        replaces that entry. Returns True when the reply was handled."""
        if not reply_ref:
            return False
        pos = self.trader.find_by_message(reply_ref)
        if pos is None or pos.status != PENDING:
            return False
        if parsed.kind == "BUY" and parsed.from_tag:
            entry = parsed.call
        elif parsed.kind == "BUY":
            return False
        else:
            entry, _error = parse_entry_reply(text)
        if entry is None:
            return False
        if not fresh:
            if age < IGNORE_AFTER_SEC:
                self.notify(f"⌛ <b>Entry not changed</b> — your message is {age / 60:.0f} min old "
                            f"(I was offline). Send it again if it is still valid.\n{_said(text)}", msg_ref)
            return True
        self.trader.retarget_waiting(pos, entry, msg_ref)
        return True

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
            self._send_trades_csv(msg_ref)

    def _publish_trades(self, pos) -> None:
        self._send_trades_csv(getattr(pos, "signal_ref", None) or None)

    def _send_trades_csv(self, reply_to: Optional[str]) -> None:
        """Send the journal with every trade's profit or loss restated for 1 lot."""
        path = config.PATHS["trade_journal"]
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        except OSError:
            rows = []
        if not rows:
            self.notify("📎 No closed trades yet — trades.csv is empty.", reply_to)
            return
        scaled = one_lot_rows(rows)
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=JOURNAL_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(scaled)
        total = sum(float(row["pnl"]) for row in scaled)
        first, last = rows[0]["exit_time"][:10], rows[-1]["exit_time"][:10]
        self.tg.send_document(
            f"trades_{clock.datestamp()}.csv", buffer.getvalue().encode("utf-8"),
            f"📎 <b>trades.csv</b> · {len(scaled)} closed trades · {esc(first)} → {esc(last)}\n"
            f"P&amp;L for 1 lot {money(total)}", reply_to)


def _said(text: str) -> str:
    """Your message, shortened, so a notice in the update group shows what it answers."""
    line = " · ".join(part.strip() for part in text.strip().splitlines() if part.strip())
    return f"<i>“{esc(line[:120])}{'…' if len(line) > 120 else ''}”</i>"
