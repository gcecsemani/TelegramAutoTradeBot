"""
notify/telegram.py
==================
Two groups:

    signal group    your calls and exit / SL replies are read from here.
                    The bot never posts in it.
    update group    every bot message goes here: entries, exits, SL changes,
                    refusals, errors.

Reading is getUpdates long polling. Sending runs on a background worker, so
a slow Telegram call never delays a stop-loss check.

Message ids restart in every group, so a message is referred to as
"chat_id:message_id" (see ref()). A bot update about your call is sent as a
cross-group reply to it when Telegram allows that, and each sent message's
ref is reported back so a reply to the bot's message finds the trade too.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import config
from core.logging_setup import get

log = get("telegram")

MAX_LEN = 4000


def esc(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def ref(chat_id: Any, message_id: Any) -> str:
    return f"{chat_id}:{message_id}"


class TelegramError(RuntimeError):
    def __init__(self, code: int, description: str, parameters: Optional[dict] = None) -> None:
        super().__init__(f"{code} {description}")
        self.code = code
        self.description = description
        self.parameters = parameters or {}


class TelegramClient:
    def __init__(self) -> None:
        self.token = config.TELEGRAM_BOT_TOKEN
        self.signal_chats: Set[str] = {config.TELEGRAM_SIGNAL_CHAT_ID}
        self.update_chat: str = config.TELEGRAM_UPDATE_CHAT_ID
        self._cross_replies = True
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=500)
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._sent_times: List[float] = []

    # ─── Raw API ──────────────────────────────────────────────────────────────

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 0,
             document: Optional[Tuple[str, bytes]] = None) -> Any:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        if document is None:
            data = urllib.parse.urlencode(params or {}).encode("utf-8")
            request = urllib.request.Request(url, data=data, method="POST")
        else:
            boundary = uuid.uuid4().hex
            parts = []
            for key, value in (params or {}).items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                             f"{value}\r\n".encode("utf-8"))
            filename, content = document
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
                         f'filename="{filename}"\r\nContent-Type: text/csv\r\n\r\n'.encode("utf-8")
                         + content + b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode("utf-8"))
            request = urllib.request.Request(url, data=b"".join(parts), method="POST")
            request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        try:
            with urllib.request.urlopen(request, timeout=timeout or config.NOTIFY["timeout_sec"]) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                raise TelegramError(exc.code, str(exc)) from exc
        if not body.get("ok"):
            raise TelegramError(int(body.get("error_code") or 0), str(body.get("description")),
                                body.get("parameters"))
        return body.get("result")

    # ─── Reading ──────────────────────────────────────────────────────────────

    def get_updates(self, offset: Optional[int], timeout: int) -> List[dict]:
        params: Dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": json.dumps(
                ["message", "edited_message", "channel_post", "edited_channel_post"]
            ),
        }
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", params, timeout=timeout + 15) or []

    def read_access(self) -> str:
        """'' when the bot can read the signal group and post to the update
        group, otherwise what to fix."""
        problems = []
        try:
            me = self.call("getMe")
            for chat in sorted(self.signal_chats | {self.update_chat}):
                try:
                    member = self.call("getChatMember", {"chat_id": chat, "user_id": me["id"]})
                    status = member.get("status")
                except TelegramError as exc:
                    status = f"not reachable ({exc.description})"
                if status in ("left", "kicked") or str(status).startswith("not reachable"):
                    problems.append(f"@{me.get('username')} is not in group {chat} ({status}). Add it.")
                elif chat in self.signal_chats and not me.get("can_read_all_group_messages") \
                        and status not in ("administrator", "creator"):
                    problems.append(
                        f"@{me.get('username')} has privacy mode ON, so it cannot read calls in {chat}. "
                        "Fix: in @BotFather send /setprivacy → pick the bot → Disable, then remove the "
                        "bot from the group and add it again (or make it a group admin).")
        except Exception as exc:
            problems.append(f"Could not check the bot's group access: {exc}")
        return "\n".join(problems)

    # ─── Sending (always to the update group) ─────────────────────────────────

    def start(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="telegram-send", daemon=True)
            self._worker.start()

    def stop(self, drain_timeout: float = 8.0) -> None:
        if self._worker is None:
            return
        deadline = time.monotonic() + drain_timeout
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.2)
        self._stop.set()
        self._worker.join(timeout=3.0)
        self._worker = None

    def send(self, text: str, reply_to: Optional[str] = None,
             on_sent: Optional[Callable[[str], None]] = None) -> None:
        """reply_to is a ref() in either group; on_sent gets the new message's ref."""
        try:
            self._queue.put_nowait({"text": text, "reply_to": reply_to, "on_sent": on_sent})
        except queue.Full:
            log.error("telegram queue full — message dropped: %s", text[:80])

    def send_document(self, filename: str, content: bytes, caption: str = "",
                      reply_to: Optional[str] = None) -> None:
        """A file to the update group, e.g. the trades CSV."""
        try:
            self._queue.put_nowait({"text": caption, "reply_to": reply_to, "on_sent": None,
                                    "document": (filename, content)})
        except queue.Full:
            log.error("telegram queue full — %s dropped", filename)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._throttle()
            message_id = self._post(item["text"], item["reply_to"], item.get("document"))
            if message_id and item["on_sent"]:
                try:
                    item["on_sent"](ref(self.update_chat, message_id))
                except Exception as exc:
                    log.error("on_sent callback failed: %s", exc)
            self._queue.task_done()

    def _throttle(self) -> None:
        now = time.monotonic()
        self._sent_times = [t for t in self._sent_times if now - t < 60.0]
        if len(self._sent_times) >= config.NOTIFY["max_msgs_per_min"]:
            time.sleep(max(0.0, 60.0 - (now - self._sent_times[0]) + 0.1))
        self._sent_times.append(time.monotonic())

    def _reply_parameters(self, reply_to: Optional[str]) -> Optional[str]:
        if not reply_to:
            return None
        chat, _, message_id = reply_to.rpartition(":")
        params: Dict[str, Any] = {"message_id": int(message_id), "allow_sending_without_reply": True}
        if chat != self.update_chat:
            if not self._cross_replies:
                return None
            params["chat_id"] = int(chat)
        return json.dumps(params)

    def _post(self, text: str, reply_to: Optional[str],
              document: Optional[Tuple[str, bytes]] = None) -> Optional[int]:
        for attempt in range(config.NOTIFY["retry_attempts"]):
            params: Dict[str, Any] = {"chat_id": self.update_chat, "parse_mode": "HTML"}
            if document is None:
                params.update({"text": text[:MAX_LEN], "disable_web_page_preview": "true"})
            elif text:
                params["caption"] = text[:1000]
            reply = self._reply_parameters(reply_to)
            if reply:
                params["reply_parameters"] = reply
            try:
                result = self.call("sendDocument" if document else "sendMessage", params,
                                   timeout=60 if document else 0, document=document)
                return int(result.get("message_id") or 0) or None
            except TelegramError as exc:
                migrated = exc.parameters.get("migrate_to_chat_id")
                if migrated:
                    self.migrated(self.update_chat, str(migrated))
                    continue
                if exc.code == 429:
                    time.sleep(float(exc.parameters.get("retry_after") or 3))
                    continue
                if exc.code == 400 and reply and "chat_id" in reply:
                    log.info("Cross-group replies are not allowed here (%s) — posting without them",
                             exc.description)
                    self._cross_replies = False
                    continue
                log.error("Telegram send failed: %s", exc)
                if 400 <= exc.code < 500:
                    return None
            except Exception as exc:
                log.error("Telegram send error: %s", exc)
            time.sleep(1.0 + attempt)
        return None

    def migrated(self, old_id: str, new_id: str) -> None:
        if old_id in self.signal_chats:
            self.signal_chats.add(new_id)
            log.critical("The signal group became a supergroup. Put TELEGRAM_SIGNAL_CHAT_ID=%s in .env", new_id)
        if old_id == self.update_chat:
            self.update_chat = new_id
            log.critical("The update group became a supergroup. Put TELEGRAM_UPDATE_CHAT_ID=%s in .env", new_id)
