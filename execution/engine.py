"""
execution/engine.py
===================
Fans one signal-book fill out to every enabled account.

Each account is independent: one rejection, a dead token or a kill switch
does not stop the others. The intent row is claimed before the broker call.
If the process dies after the call and before the fill is recorded, recovery
looks the order up by tag and will not send it again.

Buys are limit + IOC at the account's slippage cap, so a late copy does not
chase. When that buy fills, a sell stop-market rests at the broker at the
signal stop (or the 30% default). A later stop message modifies it. An exit
cancels that stop first, and does not sell again when the stop has already filled.
"""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, List, Optional

import config
from brokers.base import BrokerError, OrderRequest, OrderView, TokenError, TransientError
from brokers.registry import READY, label
from core import clock
from core.crypto import decrypt
from core.logging_setup import get
from execution import store
from execution.events import CopyEvent
from execution.risk import plan_buy, round_tick, sell_lots

log = get("copy")

Notify = Callable[[str], None]
_OPEN = {"NEW", "SENDING", "UNKNOWN", "WORKING", "WAITING", "AUTH"}
_TERMINAL_BUY_FAIL = {"MISSED", "FAILED", "REJECTED", "SKIPPED"}


@dataclass
class Result:
    label: str
    status: str
    detail: str
    notify: bool = True


def order_tag(idempotency_key: str) -> str:
    return "c" + hashlib.sha1(idempotency_key.encode()).hexdigest()[:19]


def build_broker(account: dict):
    if account.get("mode") != "live":
        from brokers.paper import PaperBroker
        return PaperBroker()
    if account.get("broker") == "zerodha":
        from brokers.zerodha import ZerodhaBroker
        return ZerodhaBroker(
            decrypt(account.get("api_key_enc") or ""),
            decrypt(account.get("api_secret_enc") or ""),
            decrypt(account.get("access_token_enc") or ""),
        )
    from brokers.stub import UnavailableBroker
    return UnavailableBroker(label(account.get("broker") or "broker"))


def buy_block_reason(account: dict) -> str:
    if not account.get("enabled"):
        return "account disabled"
    if account.get("kill_switch"):
        return account.get("kill_reason") or "kill switch is on"
    if account.get("mode") == "live":
        if not config.LIVE_TRADING:
            return "LIVE_TRADING is off on this server"
        if account.get("broker") not in READY:
            return f"{label(account.get('broker') or '')} cannot place orders yet"
        if not (account.get("access_token_enc") or ""):
            return "access token is missing"
        if account.get("token_date") != clock.today().isoformat():
            return "access token needs today's login"
    elif account.get("mode") != "paper":
        return "unknown account mode"
    return ""


class CopyEngine:
    def __init__(self, notify: Optional[Notify] = None, broker_factory=None) -> None:
        self.notify = notify or (lambda text: None)
        self.broker_factory = broker_factory or build_broker
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._guard = threading.Lock()
        self._locks: dict = {}
        try:
            store.init()
        except Exception as exc:
            log.error("Copy store is unavailable: %s", exc)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="copy-recovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def status_line(self) -> str:
        try:
            rows = store.copy_accounts()
        except Exception as exc:
            return f"Copy store unavailable ({exc})."
        if not rows:
            return "No linked accounts yet — the signal book is paper only."
        live = sum(1 for row in rows if row["mode"] == "live")
        need = sum(1 for row in rows if buy_block_reason(row))
        armed = "on" if config.LIVE_TRADING else "off"
        return (f"Copying to {len(rows)} account(s), {live} marked live. "
                f"Server live switch is {armed}. {need} cannot buy until they are fixed.")

    def handle(self, event: CopyEvent) -> None:
        try:
            accounts = store.copy_accounts()
        except Exception:
            log.exception("Could not read copy accounts")
            return
        if not accounts:
            return
        workers = min(16, len(accounts))
        results: List[Result] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="copy") as pool:
            futures = [pool.submit(self._safe, account, event) for account in accounts]
            for future in futures:
                results.append(future.result())
        self._report(event, results)

    def recover(self, positions: list) -> None:
        """Finish intents left mid-flight, and catch a crash around a fill."""
        try:
            self.reconcile()
            self._recover_book(positions)
        except Exception:
            log.exception("Copy recovery failed")

    def reconcile(self) -> None:
        for intent in store.inflight():
            try:
                self._reconcile_one(intent)
            except Exception:
                log.exception("Could not reconcile intent %s", intent.get("id"))

    def flatten_account(self, account_id: int) -> int:
        """Market-sell every open leg on this account. Returns how many were sent."""
        account = store.get_account(account_id)
        if account is None:
            return 0
        sent = 0
        for leg in store.open_legs(account_id):
            if store.active_sell(account_id, leg["position_id"]):
                continue
            event = CopyEvent(
                action="SELL", position_id=leg["position_id"], symbol="",
                exchange=leg["exchange"], tradingsymbol=leg["tradingsymbol"],
                tick_size=0.05, multiplier=int(leg["multiplier"]), lot_size=0,
                lots=int(leg["lots_open"]), lots_before=int(leg["lots_open"]),
                price=0.0, sl=None, reason="FLATTEN", signal_ref="",
            )
            result = self._sell(account, event, recover_key=f"FLATTEN:{leg['lots_open']}")
            if result.status in ("FILLED", "SENDING", "UNKNOWN", "WORKING"):
                sent += 1
        return sent

    # ─── per account ──────────────────────────────────────────────────────────

    def _safe(self, account: dict, event: CopyEvent) -> Result:
        try:
            with self._account_lock(account["id"]):
                if event.action == "BUY":
                    return self._buy(account, event)
                if event.action == "SL":
                    return self._update_stop(account, event)
                return self._sell(account, event)
        except Exception as exc:
            log.exception("Copy failed for account %s", account.get("id"))
            return Result(self._name(account), "ERROR", str(exc))

    def _buy(self, account: dict, event: CopyEvent) -> Result:
        name = self._name(account)
        reason = buy_block_reason(account)
        rule = store.effective_rule(account["id"], event.symbol)
        plan = None
        if not reason:
            plan = plan_buy(
                lots=int(rule["lots"]),
                min_lots=int(rule["min_lots"]),
                max_lots=int(rule["max_lots"]),
                slippage_mode="POINTS" if event.exact else rule["slippage_mode"],
                slippage_value=0.0 if event.exact else float(rule["slippage_value"]),
                price=event.price,
                sl=event.sl,
                tick=event.tick_size,
                multiplier=event.multiplier,
                lot_size=event.lot_size,
                max_loss_trade=_num(account.get("max_loss_trade")),
                max_loss_day=_num(account.get("max_loss_day")),
                day_realized=store.day_realized(account["id"]),
                default_sl_pct=float(config.TRADE["default_sl_pct"]),
            )
            if not plan.ok:
                reason = plan.reason
        key = f"{event.position_id}:{account['id']}:BUY"
        row = _intent_row(key, account, event, "BUY", plan.lots if plan and plan.ok else 0,
                          plan.quantity if plan and plan.ok else 0,
                          plan.limit_price if plan and plan.ok else 0.0,
                          "LIMIT", reason or "ENTRY")
        if not store.claim_intent(row):
            return Result(name, "DONE", "already handled", notify=False)
        intent = store.intent_by_key(key)
        if reason:
            store.update_intent(intent["id"], status="SKIPPED", reason=reason)
            return Result(name, "SKIPPED", reason)
        return self._send(account, intent, "BUY")

    def _sell(self, account: dict, event: CopyEvent, recover_key: str = "") -> Result:
        name = self._name(account)
        leg = store.get_leg(account["id"], event.position_id)
        buy = store.buy_intent(account["id"], event.position_id)
        if (leg is None or int(leg["lots_open"]) <= 0) and buy and buy["status"] in _OPEN:
            key = f"{event.position_id}:{account['id']}:SELL:WAIT"
            row = _intent_row(key, account, event, "SELL", event.lots, 0, 0.0, "MARKET", event.reason)
            row["status"] = "WAITING"
            if store.claim_intent(row):
                return Result(name, "WAITING", "entry is still in flight")
            return Result(name, "DONE", "already handled", notify=False)
        if leg is None or int(leg["lots_open"]) <= 0:
            return Result(name, "SKIPPED", "nothing open on this account", notify=False)
        pulled = self._pull_stop(account, leg)
        leg = store.get_leg(account["id"], event.position_id) or leg
        if pulled == "unknown":
            return Result(name, "WAITING", "stop order was not confirmed, so the exit was not sent")
        if pulled == "working":
            return Result(name, "WAITING", "the stop is still working, so no second sell was sent")
        if leg is None or int(leg["lots_open"]) <= 0:
            if pulled == "filled":
                return Result(name, "FILLED", "stop already closed this account")
            return Result(name, "SKIPPED", "nothing open on this account", notify=False)
        lots = sell_lots(int(leg["lots_open"]), event.lots, event.lots_before)
        per = max(1, int(int(leg["quantity_open"]) / int(leg["lots_open"])))
        quantity = lots * per
        suffix = recover_key or str(int(leg["lots_open"]))
        key = f"{event.position_id}:{account['id']}:SELL:{suffix}:{lots}"
        row = _intent_row(key, account, event, "SELL", lots, quantity, event.price, "MARKET", event.reason)
        row["multiplier"] = int(leg["multiplier"])
        row["exchange"] = leg["exchange"]
        row["tradingsymbol"] = leg["tradingsymbol"]
        row["product"] = leg["product"]
        if not store.claim_intent(row):
            return Result(name, "DONE", "already handled", notify=False)
        intent = store.intent_by_key(key)
        return self._send(account, intent, "SELL")

    def _send(self, account: dict, intent: dict, side: str) -> Result:
        name = self._name(account)
        store.update_intent(intent["id"], status="SENDING", attempts=int(intent["attempts"]) + 1)
        request = OrderRequest(
            exchange=intent["exchange"],
            tradingsymbol=intent["tradingsymbol"],
            side=side,
            quantity=int(intent["quantity"]),
            product=intent["product"] or "MIS",
            order_type=intent["order_type"],
            price=float(intent["limit_price"] or 0),
            validity="IOC" if side == "BUY" else "DAY",
            tag=intent["tag"],
        )
        try:
            broker = self.broker_factory(account)
            view = broker.place(request)
        except TokenError as exc:
            store.update_intent(intent["id"], status="AUTH", last_error=str(exc), reason="login required")
            return Result(name, "AUTH", "login required")
        except BrokerError as exc:
            store.update_intent(intent["id"], status="REJECTED", last_error=str(exc), reason=str(exc))
            return Result(name, "REJECTED", str(exc))
        except TransientError as exc:
            store.update_intent(intent["id"], status="UNKNOWN", last_error=str(exc))
            return Result(name, "UNKNOWN", "sent, confirmation pending")
        return self._apply(account, intent, view, side)

    def _apply(self, account: dict, intent: dict, view: OrderView, side: str) -> Result:
        name = self._name(account)
        fields = {
            "broker_order_id": view.order_id,
            "avg_price": view.average_price,
            "filled_qty": view.filled_quantity,
            "last_error": view.message,
        }
        if side == "SL" and view.status == "COMPLETE" and view.filled_quantity > 0:
            return self._book_stop_fill(account, intent, view)
        if side == "SL" and view.status == "OPEN":
            self._remember_stop(account, intent, view)
            return Result(name, "WORKING", "stop is working", notify=False)
        if view.status == "COMPLETE" and view.filled_quantity > 0:
            price = view.average_price or float(intent["limit_price"] or 0)
            if price <= 0 and side == "SELL":
                leg = store.get_leg(account["id"], intent["position_id"])
                price = float(leg["avg_entry"]) if leg else 0.0
            if price <= 0:
                store.update_intent(intent["id"], status="UNKNOWN", last_error="no fill price", **fields)
                return Result(name, "UNKNOWN", "no fill price")
            per = max(1, int(intent["quantity"]) // max(1, int(intent["lots"] or 1)))
            lots = max(1, view.filled_quantity // per) if per else int(intent["lots"])
            lots = min(lots, int(intent["lots"] or lots))
            pnl = store.apply_fill(
                account["id"], intent["position_id"], side=side, lots=lots,
                quantity=view.filled_quantity, price=price, multiplier=int(intent["multiplier"] or 1),
                tradingsymbol=intent["tradingsymbol"], exchange=intent["exchange"],
                product=intent["product"],
            )
            store.update_intent(intent["id"], status="FILLED", reason=intent.get("reason") or side, **fields)
            self._check_day_loss(account)
            detail = f"{lots} lots @ {price:,.2f}"
            if side == "SELL":
                detail += f" · pnl {pnl:,.0f}"
                self._rearm_open(account, intent["position_id"])
            if side == "BUY" and float(intent.get("stop_trigger") or 0) > 0:
                self._arm_stop(account, intent, float(intent["stop_trigger"]), view.filled_quantity)
                fresh = store.get_leg(account["id"], intent["position_id"]) or {}
                if fresh.get("sl_status") == "WORKING":
                    detail += f" · stop {float(fresh['sl_trigger']):,.2f}"
                elif fresh.get("sl_status") == "FAILED":
                    detail += " · stop was rejected"
            return Result(name, "FILLED", detail)
        if view.status == "REJECTED" or (view.status == "CANCELLED" and view.filled_quantity <= 0):
            status = "MISSED" if side == "BUY" and view.status == "CANCELLED" else "REJECTED"
            reason = view.message or ("outside the slippage limit" if status == "MISSED" else "rejected")
            store.update_intent(intent["id"], status=status, reason=reason, **fields)
            if side == "SELL":
                self._rearm_open(account, intent["position_id"])
            return Result(name, status, reason)
        if view.status == "OPEN":
            store.update_intent(intent["id"], status="WORKING", **fields)
            return Result(name, "WORKING", "order is working")
        store.update_intent(intent["id"], status="UNKNOWN", **fields)
        return Result(name, "UNKNOWN", view.message or "status not confirmed")

    def _update_stop(self, account: dict, event: CopyEvent) -> Result:
        name = self._name(account)
        leg = store.get_leg(account["id"], event.position_id)
        if leg is None or int(leg["lots_open"]) <= 0:
            return Result(name, "SKIPPED", "nothing open on this account", notify=False)
        trigger = round_tick(float(event.sl or 0), event.tick_size or 0.05)
        if trigger <= 0:
            pulled = self._pull_stop(account, leg)
            if pulled == "filled":
                return Result(name, "FILLED", "stop filled while it was being removed")
            if pulled == "unknown":
                return Result(name, "UNKNOWN", "could not confirm the stop removal")
            store.set_leg_stop(account["id"], event.position_id, "", 0, 0, "", "")
            return Result(name, "CANCELLED", "stop removed")
        if leg.get("sl_status") == "WORKING" and leg.get("sl_order_id"):
            same_price = abs(float(leg.get("sl_trigger") or 0) - trigger) < 0.001
            same_qty = int(leg.get("sl_quantity") or 0) == int(leg["quantity_open"])
            if same_price and same_qty:
                return Result(name, "DONE", "stop already at that price", notify=False)
            broker = self.broker_factory(account)
            if not hasattr(broker, "modify"):
                return Result(name, "REJECTED", "this broker cannot modify a stop")
            try:
                broker.modify(leg["sl_order_id"], trigger, int(leg["quantity_open"]))
            except BrokerError as exc:
                return Result(name, "REJECTED", f"stop not moved: {exc}")
            except TransientError:
                return Result(name, "UNKNOWN", "stop move was not confirmed")
            store.set_leg_stop(
                account["id"], event.position_id, leg["sl_order_id"], trigger,
                int(leg["quantity_open"]), leg.get("sl_tag") or "", "WORKING",
            )
            existing = store.intent_by_tag(leg.get("sl_tag") or "")
            if existing:
                store.update_intent(existing["id"], limit_price=trigger, reason="SL UPDATE")
            return Result(name, "UPDATED", f"stop {trigger:,.2f}")
        self._arm_stop(account, {
            "position_id": event.position_id, "symbol": event.symbol, "exchange": event.exchange,
            "tradingsymbol": event.tradingsymbol, "product": leg["product"],
            "multiplier": leg["multiplier"],
        }, trigger, int(leg["quantity_open"]))
        fresh = store.get_leg(account["id"], event.position_id) or {}
        if fresh.get("sl_status") == "WORKING":
            return Result(name, "WORKING", f"stop {trigger:,.2f}")
        return Result(name, "REJECTED", "stop was not placed")

    def _arm_stop(self, account: dict, intent: dict, trigger: float, quantity: int) -> None:
        if trigger <= 0 or quantity <= 0:
            return
        leg = store.get_leg(account["id"], intent["position_id"])
        if leg is None or int(leg["lots_open"]) <= 0:
            return
        if leg.get("sl_status") == "WORKING" and leg.get("sl_order_id"):
            return
        trigger = round_tick(float(trigger), 0.05)
        per = max(1, int(leg["quantity_open"]) // max(1, int(leg["lots_open"])))
        lots = max(1, int(quantity) // per)
        key = f"{intent['position_id']}:{account['id']}:STOP:{quantity}:{trigger}"
        event = CopyEvent(
            action="SL", position_id=intent["position_id"], symbol=intent.get("symbol") or "",
            exchange=intent["exchange"], tradingsymbol=intent["tradingsymbol"],
            tick_size=0.05, multiplier=int(intent.get("multiplier") or leg["multiplier"] or 1),
            lot_size=0, lots=lots, lots_before=lots, price=trigger, sl=trigger,
            reason="STOP", signal_ref="",
        )
        row = _intent_row(key, account, event, "SL", lots, quantity, trigger, "SL-M", "STOP")
        row["exchange"] = leg["exchange"]
        row["tradingsymbol"] = leg["tradingsymbol"]
        row["product"] = leg["product"]
        if not store.claim_intent(row):
            existing = store.intent_by_key(key)
            if existing is None or existing["status"] in ("WORKING", "FILLED", "SENDING", "UNKNOWN", "NEW", "AUTH"):
                return
            key = f"{key}:again:{existing['id']}"
            row = _intent_row(key, account, event, "SL", lots, quantity, trigger, "SL-M", "STOP")
            row["exchange"] = leg["exchange"]
            row["tradingsymbol"] = leg["tradingsymbol"]
            row["product"] = leg["product"]
            if not store.claim_intent(row):
                return
        claimed = store.intent_by_key(key)
        if claimed is None:
            return
        request = OrderRequest(
            exchange=leg["exchange"], tradingsymbol=leg["tradingsymbol"], side="SELL",
            quantity=int(quantity), product=leg["product"] or "MIS", order_type="SL-M",
            price=0.0, validity="DAY", tag=claimed["tag"], trigger_price=trigger,
        )
        try:
            view = self.broker_factory(account).place(request)
        except TokenError as exc:
            store.update_intent(claimed["id"], status="AUTH", last_error=str(exc), reason="login required")
            store.set_leg_stop(account["id"], intent["position_id"], "", trigger, 0, claimed["tag"], "FAILED")
            return
        except BrokerError as exc:
            store.update_intent(claimed["id"], status="REJECTED", last_error=str(exc), reason=str(exc))
            store.set_leg_stop(account["id"], intent["position_id"], "", trigger, 0, claimed["tag"], "FAILED")
            self.notify(f"⚠️ Stop rejected · {_esc(self._name(account))} · {_esc(leg['tradingsymbol'])}\n{ _esc(str(exc))}")
            return
        except TransientError as exc:
            store.update_intent(claimed["id"], status="UNKNOWN", last_error=str(exc))
            store.set_leg_stop(account["id"], intent["position_id"], "", trigger, quantity, claimed["tag"], "UNKNOWN")
            return
        store.update_intent(claimed["id"], broker_order_id=view.order_id, limit_price=trigger)
        if view.status == "COMPLETE" and view.filled_quantity > 0:
            self._book_stop_fill(account, store.intent_by_key(key) or claimed, view)
            return
        if view.status in ("REJECTED", "CANCELLED"):
            store.update_intent(claimed["id"], status="REJECTED", reason=view.message or "stop rejected",
                                broker_order_id=view.order_id, last_error=view.message)
            store.set_leg_stop(account["id"], intent["position_id"], "", trigger, 0, claimed["tag"], "FAILED")
            self.notify(f"⚠️ Stop rejected · {_esc(self._name(account))} · {_esc(leg['tradingsymbol'])}\n"
                        f"{_esc(view.message or 'rejected')}")
            return
        self._remember_stop(account, store.intent_by_key(key) or claimed, view)

    def _remember_stop(self, account: dict, intent: dict, view: OrderView) -> None:
        order_id = view.order_id or intent.get("broker_order_id") or ""
        trigger = float(intent.get("limit_price") or intent.get("stop_trigger") or 0)
        store.update_intent(intent["id"], status="WORKING", broker_order_id=order_id, limit_price=trigger,
                            reason="STOP")
        store.set_leg_stop(
            account["id"], intent["position_id"], order_id, trigger,
            int(intent.get("quantity") or 0), intent.get("tag") or "", "WORKING",
        )

    def _rearm_open(self, account: dict, position_id: str) -> None:
        leg = store.get_leg(account["id"], position_id)
        if leg is None or int(leg["lots_open"]) <= 0:
            return
        if leg.get("sl_status") == "WORKING" and leg.get("sl_order_id"):
            return
        trigger = float(leg.get("sl_trigger") or 0)
        if trigger <= 0:
            return
        self._arm_stop(account, {
            "position_id": position_id, "symbol": "", "exchange": leg["exchange"],
            "tradingsymbol": leg["tradingsymbol"], "product": leg["product"],
            "multiplier": leg["multiplier"],
        }, trigger, int(leg["quantity_open"]))

    def _pull_stop(self, account: dict, leg: dict) -> str:
        """clear, filled, unknown, or none. A filled stop is booked onto the leg."""
        if leg.get("sl_status") not in ("WORKING", "UNKNOWN") or not (leg.get("sl_order_id") or leg.get("sl_tag")):
            return "none"
        trigger = float(leg.get("sl_trigger") or 0)
        order_id = leg.get("sl_order_id") or ""
        broker = self.broker_factory(account)
        view = None
        try:
            if order_id and hasattr(broker, "cancel"):
                view = broker.cancel(order_id)
            elif order_id:
                view = OrderView(order_id, "CANCELLED", 0.0, 0, "cancel is not supported")
            else:
                return "unknown"
        except BrokerError:
            try:
                view = broker.fetch(order_id) if hasattr(broker, "fetch") else None
            except (TransientError, BrokerError, TokenError):
                return "unknown"
        except (TransientError, TokenError):
            try:
                view = broker.fetch(order_id) if hasattr(broker, "fetch") else None
            except (TransientError, BrokerError, TokenError):
                return "unknown"
        if view is None or view.status == "OPEN":
            return "unknown" if view is None else "working"
        if view.status == "COMPLETE" and view.filled_quantity > 0:
            intent = store.intent_by_tag(leg.get("sl_tag") or "")
            if intent is None:
                return "unknown"
            self._book_stop_fill(account, intent, view)
            return "filled"
        intent = store.intent_by_tag(leg.get("sl_tag") or "")
        if intent and intent["status"] not in ("FILLED", "REJECTED", "CANCELLED"):
            store.update_intent(intent["id"], status="CANCELLED", reason="removed before exit")
        store.set_leg_stop(account["id"], leg["position_id"], "", trigger, 0, "", "")
        return "clear"

    def _book_stop_fill(self, account: dict, intent: dict, view: OrderView) -> Result:
        name = self._name(account)
        fresh_intent = store.intent_by_id(intent["id"]) or intent
        if fresh_intent.get("status") == "FILLED":
            return Result(name, "FILLED", "stop already booked", notify=False)
        leg = store.get_leg(account["id"], intent["position_id"])
        price = view.average_price or float(intent.get("limit_price") or 0)
        if leg is None or int(leg["lots_open"]) <= 0:
            store.update_intent(intent["id"], status="FILLED", reason="stop filled",
                                broker_order_id=view.order_id, avg_price=price, filled_qty=view.filled_quantity)
            return Result(name, "FILLED", "stop filled", notify=False)
        if price <= 0:
            price = float(leg["avg_entry"])
        per = max(1, int(leg["quantity_open"]) // max(1, int(leg["lots_open"])))
        lots = min(int(leg["lots_open"]), max(1, view.filled_quantity // per))
        qty = min(int(leg["quantity_open"]), int(view.filled_quantity))
        pnl = store.apply_fill(
            account["id"], intent["position_id"], side="SELL", lots=lots, quantity=qty, price=price,
            multiplier=int(leg["multiplier"] or intent.get("multiplier") or 1),
            tradingsymbol=leg["tradingsymbol"], exchange=leg["exchange"], product=leg["product"],
        )
        store.update_intent(intent["id"], status="FILLED", reason="STOP FILLED",
                            broker_order_id=view.order_id, avg_price=price, filled_qty=view.filled_quantity)
        kept = store.get_leg(account["id"], intent["position_id"])
        trigger = float((kept or leg).get("sl_trigger") or intent.get("limit_price") or 0)
        if kept and int(kept["lots_open"]) > 0 and trigger > 0:
            store.set_leg_stop(account["id"], intent["position_id"], "", trigger, 0, "", "")
            self._arm_stop(account, intent, trigger, int(kept["quantity_open"]))
        else:
            store.set_leg_stop(account["id"], intent["position_id"], view.order_id, trigger, 0,
                               intent.get("tag") or "", "FILLED")
        self._check_day_loss(account)
        return Result(name, "FILLED", f"stop filled {lots} lots @ {price:,.2f} · pnl {pnl:,.0f}")

    def _check_day_loss(self, account: dict) -> None:
        if account.get("kill_switch"):
            return
        limit = _num(account.get("max_loss_day"))
        if not account.get("auto_kill") or limit is None:
            return
        realized = store.day_realized(account["id"])
        if realized > -limit:
            return
        store.set_kill(account["id"], True, "max loss per day")
        account["kill_switch"] = 1
        self.notify(
            f"🛑 <b>Kill switch</b> · {_esc(self._name(account))}\n"
            f"Day P&amp;L is {realized:,.0f}, past the {limit:,.0f} loss limit. New buys are stopped."
        )
        if account.get("flatten_on_kill"):
            self.flatten_account(account["id"])

    def _reconcile_one(self, intent: dict) -> None:
        age = _age(intent.get("updated_at") or "")
        if intent["status"] == "NEW":
            if age < 10:
                return
            if int(intent.get("quantity") or 0) <= 0:
                store.update_intent(intent["id"], status="SKIPPED", reason=intent.get("reason") or "not sent")
                return
        if intent["status"] in ("SENDING", "WORKING") and age < 15:
            return
        account = store.get_account(intent["account_id"])
        if account is None:
            return
        if intent["status"] == "WAITING":
            self._release_waiting(account, intent)
            return
        with self._account_lock(account["id"]):
            fresh = store.intent_by_id(intent["id"]) or intent
            if fresh["status"] not in _OPEN:
                return
            if fresh["status"] == "AUTH" and account.get("token_date") != clock.today().isoformat():
                return
            self._resolve_broker_state(account, fresh)

    def _release_waiting(self, account: dict, intent: dict) -> None:
        buy = store.buy_intent(account["id"], intent["position_id"])
        leg = store.get_leg(account["id"], intent["position_id"])
        if buy and buy["status"] in _OPEN:
            return
        if buy and buy["status"] in _TERMINAL_BUY_FAIL:
            store.update_intent(intent["id"], status="SKIPPED", reason="entry did not fill")
            return
        if leg is None or int(leg["lots_open"]) <= 0:
            store.update_intent(intent["id"], status="SKIPPED", reason="nothing open")
            return
        store.update_intent(intent["id"], status="SKIPPED", reason="replaced by the exit")
        event = CopyEvent(
            action="SELL", position_id=intent["position_id"], symbol=intent["symbol"],
            exchange=leg["exchange"], tradingsymbol=leg["tradingsymbol"],
            tick_size=0.05, multiplier=int(leg["multiplier"]), lot_size=0,
            lots=int(leg["lots_open"]), lots_before=int(leg["lots_open"]),
            price=0.0, sl=None, reason=intent.get("reason") or "EXIT", signal_ref="",
        )
        with self._account_lock(account["id"]):
            self._sell(account, event, recover_key="AFTER-ENTRY")

    def _resolve_broker_state(self, account: dict, intent: dict) -> None:
        try:
            broker = self.broker_factory(account)
            view = None
            if intent.get("broker_order_id"):
                view = broker.fetch(intent["broker_order_id"])
            if view is None or view.status == "UNKNOWN":
                view = broker.find_by_tag(intent["tag"]) or view
        except TokenError as exc:
            store.update_intent(intent["id"], status="AUTH", last_error=str(exc))
            return
        except TransientError:
            return
        except BrokerError as exc:
            store.update_intent(intent["id"], status="REJECTED", last_error=str(exc), reason=str(exc))
            return
        if view is not None and view.status != "UNKNOWN":
            self._apply(account, intent, view, intent["action"])
            return
        attempts = int(intent["attempts"])
        if attempts >= 3:
            store.update_intent(intent["id"], status="FAILED", reason="gave up after 3 tries")
            self.notify(f"⚠️ Copy failed · {_esc(self._name(account))} · {_esc(intent['tradingsymbol'])}\n"
                        "No matching broker order after 3 tries. Check the portal.")
            return
        fresh = dict(intent)
        fresh["attempts"] = attempts
        self._send(account, fresh, intent["action"])

    def _recover_book(self, positions: list) -> None:
        accounts = store.copy_accounts()
        if not accounts:
            return
        now = clock.now()
        for pos in positions:
            status = getattr(pos, "status", "")
            age = _age(getattr(pos, "entry_time", "") or getattr(pos, "created_at", ""))
            for account in accounts:
                if status == "OPEN" and age <= 180 and not store.buy_intent(account["id"], pos.id):
                    self._safe(account, _event_from_position(pos, "BUY", pos.lots, pos.lots, "RECOVERY"))
                leg = store.get_leg(account["id"], pos.id)
                master_open = pos.lots if status == "OPEN" else 0
                if leg and int(leg["lots_open"]) > 0 and master_open <= 0 and not store.active_sell(account["id"], pos.id):
                    self._safe(account, _event_from_position(pos, "SELL", int(leg["lots_open"]), int(leg["lots_open"]), "RECOVERY"))

    def _report(self, event: CopyEvent, results: List[Result]) -> None:
        shown = [item for item in results if item.notify]
        if not shown:
            return
        verb = "BUY" if event.action == "BUY" else "SELL"
        lines = [f"<b>Copy {verb}</b> · {_esc(event.tradingsymbol)} · {event.reason}"]
        for item in shown:
            lines.append(f"• {_esc(item.label)} · {item.status} · {_esc(item.detail)}")
        self.notify("\n".join(lines))

    def _account_lock(self, account_id: int) -> threading.Lock:
        with self._guard:
            if account_id not in self._locks:
                self._locks[account_id] = threading.RLock()
            return self._locks[account_id]

    def _loop(self) -> None:
        while not self._stop.wait(2.0):
            try:
                self.reconcile()
            except Exception:
                log.exception("Copy recovery pass failed")

    @staticmethod
    def _name(account: dict) -> str:
        return f"{account.get('label') or 'account'} · {label(account.get('broker') or '')} · {account.get('mode')}"


def _intent_row(key, account, event: CopyEvent, action, lots, quantity, limit_price, order_type, reason) -> dict:
    return {
        "idempotency_key": key,
        "account_id": account["id"],
        "position_id": event.position_id,
        "action": action,
        "symbol": event.symbol,
        "exchange": event.exchange,
        "tradingsymbol": event.tradingsymbol,
        "lots": lots,
        "quantity": quantity,
        "multiplier": event.multiplier or 1,
        "order_type": order_type,
        "limit_price": limit_price,
        "stop_trigger": float(event.sl or 0),
        "product": account.get("product") or "MIS",
        "tag": order_tag(key),
        "status": "NEW",
        "reason": reason,
    }


def _event_from_position(pos, action: str, lots: int, lots_before: int, reason: str) -> CopyEvent:
    return CopyEvent(
        action=action, position_id=pos.id, symbol=pos.symbol, exchange=pos.exchange,
        tradingsymbol=pos.tradingsymbol, tick_size=pos.tick_size or 0.05,
        multiplier=pos.multiplier, lot_size=getattr(pos, "lot_size", 0) or 0,
        lots=lots, lots_before=lots_before, price=pos.entry_price or pos.last_ltp or 0.0,
        sl=pos.sl, reason=reason, signal_ref=pos.signal_ref,
        exact=getattr(pos, "mode", "") == "ONLY",
    )


def _num(value) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def _age(iso: str) -> float:
    dt = clock.parse_iso(iso)
    if dt is None:
        return 1e9
    return (clock.now() - dt).total_seconds()


def _esc(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
