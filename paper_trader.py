"""
paper_trader.py
===============
Paper positions opened from Telegram calls: 2 lots, nearest expiry.

    entry   Buy CMP      buys now at the live ask.
            Buy Near X   buys once the ask is at most X + 2.5%. Lower is fine.
            Buy Above X  buys once the premium is at or above X and the ask is
                         at most X + 2.5%. If it has run further, it waits for
                         the premium to come back into that band.
            A call still waiting is dropped at the session square-off.
    stop    exits every open lot at the bid when the LTP trades at or below
            the SL. No SL in the call → 30% under the fill until you send one.
            "No sl" / "Hero zero" → no stop.
    exit    your Exit / Book … sells every open lot at the bid; Book half sells
            half of them (1 of 2).
    close   whatever is open is squared off at the session square-off.

Every change is saved to state/positions.json, so a restart keeps open trades
and your replies still find them.
"""

from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

import config
from core import clock
from core.ledger import PerformanceLedger
from core.logging_setup import get
from data.instruments import Contract, InstrumentMaster
from data.kite_gateway import KiteGateway
from notify.telegram import esc
from signal_parser import BUY_RE, BuyCall, FollowUp, normalize, parse_call

log = get("trader")

PENDING, OPEN, CLOSED, CANCELLED = "PENDING", "OPEN", "CLOSED", "CANCELLED"

# notify(text, reply_to_ref, position_id) — refs are "chat_id:message_id"
Notify = Callable[[str, Optional[str], Optional[str]], None]

JOURNAL_COLUMNS = [
    "exit_time", "entry_time", "mode", "symbol", "contract", "expiry", "strike", "type",
    "lots", "multiplier", "entry_price", "exit_price", "gross_pnl", "costs", "pnl",
    "exit_reason", "sl", "targets", "signal",
]


def money(value: float) -> str:
    if value > 0:
        return f"+₹{value:,.0f}"
    if value < 0:
        return f"-₹{abs(value):,.0f}"
    return "₹0"


def _round_tick(price: float, tick: float) -> float:
    tick = tick or 0.05
    return round(round(price / tick) * tick, 2)


def _lots(n: int) -> str:
    return f"{n} lot{'s' if n != 1 else ''}"


@dataclass
class Quote:
    ltp: float
    bid: float
    ask: float

    def buy_price(self) -> float:
        return self.ask if self.ask > 0 else self.ltp

    def sell_price(self) -> float:
        return self.bid if self.bid > 0 else self.ltp


def _to_quote(row: Optional[dict]) -> Optional[Quote]:
    if not row:
        return None
    depth = row.get("depth") or {}
    bids, asks = depth.get("buy") or [], depth.get("sell") or []
    bid = float(bids[0].get("price") or 0) if bids else 0.0
    ask = float(asks[0].get("price") or 0) if asks else 0.0
    ltp = float(row.get("last_price") or 0) or ask or bid
    if ltp <= 0:
        return None
    return Quote(ltp=ltp, bid=bid, ask=ask)


@dataclass
class Position:
    id: str
    symbol: str
    exchange: str
    tradingsymbol: str
    expiry: str
    strike: float
    opt_type: str
    multiplier: int
    tick_size: float
    lots: int                            # lots still open (or to buy, while PENDING)
    status: str
    signal_ref: str                      # your call, "chat_id:message_id"
    signal_text: str
    created_at: str
    mode: str = "CMP"                    # ABOVE / NEAR / CMP
    level: Optional[float] = None
    initial_lots: int = 0
    sl: Optional[float] = None
    no_sl: bool = False
    sl_note: str = ""
    entry_price: float = 0.0
    entry_time: str = ""
    targets: List[float] = field(default_factory=list)
    targets_hit: List[float] = field(default_factory=list)
    exits: List[dict] = field(default_factory=list)
    costs: List[dict] = field(default_factory=list)
    refs: List[str] = field(default_factory=list)       # every message that points at this trade
    last_ltp: float = 0.0
    closed_at: str = ""
    close_reason: str = ""

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"

    @property
    def label(self) -> str:
        expiry = clock.as_date(self.expiry)
        return f"{self.symbol} {self.strike:g} {self.opt_type} ({expiry.strftime('%d %b') if expiry else '?'})"

    @property
    def gross(self) -> float:
        return sum(float(e["pnl"]) for e in self.exits)

    @property
    def cost_total(self) -> float:
        return sum(float(c["amount"]) for c in self.costs)

    @property
    def avg_exit(self) -> float:
        lots = sum(int(e["lots"]) for e in self.exits)
        return sum(float(e["price"]) * int(e["lots"]) for e in self.exits) / lots if lots else 0.0

    def open_pnl(self) -> float:
        if self.status != OPEN or self.last_ltp <= 0:
            return 0.0
        return (self.last_ltp - self.entry_price) * self.lots * self.multiplier

    def sl_text(self) -> str:
        if self.sl is not None:
            return f"{self.sl:,.2f}"
        if self.no_sl:
            return "none (hero zero)"
        if self.status == PENDING:
            return f"default {config.TRADE['default_sl_pct']:g}% (set at fill)"
        return "none"

    def entry_text(self) -> str:
        if self.mode == "CMP":
            return "Buy CMP"
        return f"Buy {self.mode.title()} {self.level:g}"


def _signal_with_entry(text: str, call: BuyCall) -> str:
    """The call text with its Buy Above / Near / CMP line swapped for this entry."""
    entry = "Buy CMP" if call.mode == "CMP" else f"Buy {call.mode.title()} {call.level:g}"
    updated, n = re.compile(BUY_RE.pattern, re.IGNORECASE).subn(entry, text, count=1)
    return updated if n else text.rstrip() + "\n" + entry


class PaperTrader:
    def __init__(self, gateway: KiteGateway, instruments: InstrumentMaster, notify: Notify) -> None:
        self.gw = gateway
        self.instruments = instruments
        self.notify = notify
        self.lock = threading.RLock()
        self.positions: Dict[str, Position] = {}
        self.ledger = PerformanceLedger()
        self._quote_fail_since: Optional[float] = None
        self._quote_alerted = False

    # ══════════════════════════════════════════════════════════════════════════
    #  Persistence and lookup
    # ══════════════════════════════════════════════════════════════════════════

    def load(self) -> int:
        path = config.PATHS["positions"]
        if not os.path.exists(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Could not read %s: %s", path, exc)
            return 0
        names = {f.name for f in fields(Position)}
        cutoff = clock.now() - timedelta(days=config.ENGINE["keep_closed_days"])
        with self.lock:
            for row in data.get("positions", []):
                try:
                    pos = Position(**{k: v for k, v in row.items() if k in names})
                except TypeError as exc:
                    log.warning("Skipping an unreadable saved trade (%s): %s", exc, row.get("tradingsymbol"))
                    continue
                if pos.status in (CLOSED, CANCELLED):
                    done = clock.parse_iso(pos.closed_at or pos.created_at)
                    if done is not None and done < cutoff:
                        continue
                self.positions[pos.id] = pos
            active = sum(1 for p in self.positions.values() if p.status in (OPEN, PENDING))
        log.info("Loaded %d trades (%d active)", len(self.positions), active)
        return active

    def save(self) -> None:
        path = config.PATHS["positions"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self.lock:
            data = {"saved_at": clock.now().isoformat(),
                    "positions": [asdict(p) for p in self.positions.values()]}
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
            os.replace(tmp, path)
        except OSError as exc:
            log.error("Could not save positions: %s", exc)

    def link(self, position_id: str, ref: str) -> None:
        with self.lock:
            pos = self.positions.get(position_id)
            if pos is not None and ref not in pos.refs:
                pos.refs.append(ref)
        self.save()

    def find_by_message(self, ref: str) -> Optional[Position]:
        """The trade a message points at. After "Buy again" the same call points
        at several trades: the open / waiting one wins, else the latest."""
        with self.lock:
            matches = [p for p in self.positions.values() if ref in p.refs]
        if not matches:
            return None
        return max(matches, key=lambda p: (p.status in (OPEN, PENDING), p.created_at))

    def active(self) -> List[Position]:
        with self.lock:
            return [p for p in self.positions.values() if p.status in (OPEN, PENDING)]

    # ══════════════════════════════════════════════════════════════════════════
    #  Market data
    # ══════════════════════════════════════════════════════════════════════════

    def quotes(self, keys: List[str]) -> Dict[str, Quote]:
        keys = sorted({k for k in keys if k})
        if not keys:
            return {}
        rows = self.gw.quote(keys)
        out = {}
        for key in keys:
            quote = _to_quote(rows.get(key))
            if quote is not None:
                out[key] = quote
        return out

    def _quote(self, key: str, attempts: int = 3) -> Optional[Quote]:
        for attempt in range(attempts):
            quote = self.quotes([key]).get(key)
            if quote is not None:
                return quote
            if attempt < attempts - 1:
                time.sleep(0.4 * (attempt + 1))
        return None

    # ══════════════════════════════════════════════════════════════════════════
    #  Buy call
    # ══════════════════════════════════════════════════════════════════════════

    def open_call(self, call: BuyCall, ref: str, text: str, extra_refs: List[str] = ()) -> None:
        exchange = config.symbol_cfg(call.symbol)["exchange"]

        def refuse(reason: str) -> None:
            log.info("Not taken (%s): %s", call.contract_text(), reason)
            self.notify(f"❌ <b>Not taken</b> · {esc(call.contract_text())}\n{esc(reason)}", ref, None)

        if not clock.is_open(exchange):
            refuse(f"{exchange} is closed now (session {clock.session_text(exchange)}, Mon–Fri).")
            return
        if config.AUTO_SQUARE_OFF and clock.past_square_off(exchange):
            refuse(f"Past the {exchange} square-off time ({clock.square_off_time(exchange).strftime('%H:%M')}).")
            return
        contract, error = self._resolve(call)
        if contract is None:
            refuse(error)
            return
        quote = self._quote(contract.key)
        if quote is None:
            refuse(f"No live quote for {contract.tradingsymbol}.")
            return

        lots = int(config.TRADE["lots"])
        pos = Position(
            id=uuid.uuid4().hex[:8],
            symbol=call.symbol,
            exchange=contract.exchange,
            tradingsymbol=contract.tradingsymbol,
            expiry=contract.expiry.isoformat(),
            strike=contract.strike,
            opt_type=contract.opt_type,
            multiplier=contract.multiplier,
            tick_size=contract.tick_size,
            lots=lots,
            initial_lots=lots,
            status=PENDING,
            signal_ref=ref,
            signal_text=text.strip()[:500],
            created_at=clock.now().isoformat(),
            mode=call.mode,
            level=call.level,
            sl=call.sl,
            no_sl=call.no_sl,
            targets=list(call.targets),
            refs=[ref] + [r for r in extra_refs if r != ref],
            last_ltp=quote.ltp,
        )
        with self.lock:
            # Registered before the confirmation is sent, so its message id can link back.
            self.positions[pos.id] = pos
            if self._entry_ready(pos, quote):
                error = self._fill(pos, quote)
                if error:
                    del self.positions[pos.id]
                    refuse(error)
                    return
            else:
                self._notify_waiting(pos, quote)
        self.save()

    def _resolve(self, call: BuyCall) -> Tuple[Optional[Contract], str]:
        if not self.instruments.ensure_loaded():
            return None, "Instrument list could not be loaded."
        expiry = self.instruments.expiry(call.symbol)
        if expiry is None:
            return None, f"No {call.symbol} option contracts found."
        contract = self.instruments.option(call.symbol, call.strike, call.opt_type)
        if contract is None:
            strikes = self.instruments.strikes(call.symbol)
            nearest = sorted(sorted(strikes, key=lambda s: abs(s - call.strike))[:3])
            listed = ", ".join(f"{s:g}" for s in nearest)
            return None, (f"{call.symbol} {call.strike:g} {call.opt_type} is not listed for the "
                          f"{expiry.strftime('%d %b')} expiry. Nearest strikes: {listed}.")
        return contract, ""

    @staticmethod
    def _cap(pos: Position) -> float:
        band = config.TRADE["above_band_pct" if pos.mode == "ABOVE" else "near_band_pct"]
        return float(pos.level) * (1 + band / 100.0)

    def _entry_ready(self, pos: Position, quote: Quote) -> bool:
        if pos.mode == "CMP":
            return True
        if quote.buy_price() > self._cap(pos):
            return False
        return pos.mode == "NEAR" or quote.ltp >= float(pos.level)

    def _fill(self, pos: Position, quote: Quote) -> str:
        """Buy every lot at the ask. Returns a refusal when the SL is already
        at or above the price."""
        price = _round_tick(quote.buy_price(), pos.tick_size)
        if pos.no_sl:
            sl, note = None, "No SL (hero zero)."
        elif pos.sl is not None:
            if pos.sl >= price:
                return f"Price {price:,.2f} is already at or below your SL {pos.sl:g}."
            sl, note = pos.sl, ""
        else:
            pct = config.TRADE["default_sl_pct"]
            sl = _round_tick(price * (1 - pct / 100.0), pos.tick_size)
            note = f"No SL in the call — {pct:g}% default used. Tag the call with SL … to change it."

        now = clock.now().isoformat()
        pos.status = OPEN
        pos.entry_price = price
        pos.entry_time = now
        pos.sl, pos.sl_note = sl, note
        pos.costs.append({"time": now, "amount": config.TRADE["cost_per_order"]})
        pos.last_ltp = quote.ltp
        pos.targets_hit = [t for t in pos.targets if t <= price]
        log.info("BUY %s %d lots @ %.2f  SL %s", pos.tradingsymbol, pos.lots, price, pos.sl_text())
        self._notify_opened(pos)
        return ""

    # ══════════════════════════════════════════════════════════════════════════
    #  Follow-ups
    # ══════════════════════════════════════════════════════════════════════════

    def follow_up(self, pos: Position, fu: FollowUp, ref: str) -> None:
        if fu.buy_again:
            self.buy_again(pos, ref, fu.entry_mode, fu.entry_level)
            return
        if fu.exit:
            self._exit_on_reply(pos, fu.exit == "HALF", ref)
        else:
            if fu.sl:
                self._set_sl(pos, fu, ref)
            if fu.targets:
                self._set_targets(pos, fu.targets, ref)
        self.save()

    def buy_again(self, pos: Position, ref: str, mode: str = "", level: Optional[float] = None) -> None:
        """The same call again — contract, SL and targets as posted — once the
        previous trade on it is finished. "Buy again Above / Near / CMP" uses
        that entry; a plain "Buy again" repeats the entry already on the call."""
        if pos.status in (OPEN, PENDING):
            what = "still open" if pos.status == OPEN else "still waiting to buy"
            self.notify(f"ℹ️ {esc(pos.label)} is {what} — not bought again. Exit it first.", ref, pos.id)
            return
        parsed = parse_call(normalize(pos.signal_text))[0]
        call = parsed or BuyCall(symbol=pos.symbol, strike=pos.strike, opt_type=pos.opt_type,
                                 mode=pos.mode, level=pos.level, targets=list(pos.targets))
        text = pos.signal_text
        if mode:
            call.mode = mode
            call.level = None if mode == "CMP" else level
            text = _signal_with_entry(text, call)
        log.info("Buy again on %s: %s", pos.tradingsymbol, call)
        self.open_call(call, ref, text, extra_refs=pos.refs)

    def _already_done(self, pos: Position, ref: str) -> None:
        what = "closed" if pos.status == CLOSED else "cancelled"
        self.notify(f"ℹ️ {esc(pos.label)} is already {what}"
                    f"{' (' + esc(pos.close_reason) + ')' if pos.close_reason else ''}.", ref, pos.id)

    def _exit_on_reply(self, pos: Position, half: bool, ref: str) -> None:
        """Sell at once whatever the P&L — every lot, or half of them — or drop
        the call if it is still waiting. One quote try; the monitor keeps
        last_ltp fresh every second, so that is the fallback."""
        with self.lock:
            if pos.status == PENDING:
                self._cancel(pos, "Dropped on your reply", ref)
                return
            if pos.status != OPEN:
                self._already_done(pos, ref)
                return
        try:
            quote = self.quotes([pos.key]).get(pos.key)
        except Exception as exc:
            log.warning("Exit quote for %s failed (%s) — using the last price", pos.tradingsymbol, exc)
            quote = None
        with self.lock:
            if pos.status != OPEN:
                self._already_done(pos, ref)
                return
            if quote:
                pos.last_ltp = quote.ltp
            price = (quote.sell_price() if quote else 0.0) or pos.last_ltp or pos.entry_price
            notes = [] if quote else ["No live quote — used the last traded price."]
            if half:
                lots = max(1, pos.lots // 2)
                if pos.lots == 1:
                    notes.append("Only 1 lot was open — closed it.")
                reason = "BOOK HALF"
            else:
                lots, reason = pos.lots, "EXIT (your reply)"
            self._exit(pos, lots, price, reason, ref, " ".join(notes))

    def _set_sl(self, pos: Position, fu: FollowUp, ref: str) -> None:
        with self.lock:
            if pos.status == PENDING:
                if fu.sl == "NONE":
                    pos.sl, pos.no_sl = None, True
                elif fu.sl == "PRICE":
                    pos.sl, pos.no_sl = fu.sl_value, False
                else:
                    self.notify(f"⚠️ {esc(pos.label)} is not bought yet — SL to cost / to a target "
                                "works after the fill.", ref, pos.id)
                    return
                self.notify(f"🔧 SL for the waiting {esc(pos.label)} is now {esc(pos.sl_text())}. "
                            "It applies when the entry fills.", ref, pos.id)
                return
            if pos.status != OPEN:
                self._already_done(pos, ref)
                return
            if fu.sl == "NONE":
                old = pos.sl_text()
                pos.sl, pos.no_sl = None, True
                self.notify(f"🔧 <b>SL removed</b> · {esc(pos.label)}\n{esc(old)} → <b>none (hero zero)</b>",
                            ref, pos.id)
                return
            if fu.sl == "COST":
                level, name = pos.entry_price, "cost"
            elif fu.sl == "TARGET":
                number = int(fu.sl_value)
                if number > len(pos.targets):
                    self.notify(f"⚠️ {esc(pos.label)} has no target {number}"
                                f"{' (targets ' + ' / '.join(f'{t:g}' for t in pos.targets) + ')' if pos.targets else ''}"
                                ". SL not changed.", ref, pos.id)
                    return
                level, name = pos.targets[number - 1], f"target {number}"
            else:
                level, name = fu.sl_value, ""
        quote = self._quote(pos.key)
        with self.lock:
            if pos.status != OPEN:
                self._already_done(pos, ref)
                return
            ltp = quote.ltp if quote else pos.last_ltp
            level = _round_tick(level, pos.tick_size)
            if level >= ltp:
                self.notify(f"⚠️ SL not changed for {esc(pos.label)}: {level:,.2f} is not below the live "
                            f"premium {ltp:,.2f}.\nSL stays {esc(pos.sl_text())}.", ref, pos.id)
                return
            old = pos.sl_text()
            pos.sl, pos.no_sl = level, False
            log.info("SL %s: %s -> %.2f", pos.tradingsymbol, old, level)
            self.notify(f"🔧 <b>SL moved{' to ' + name if name else ''}</b> · {esc(pos.label)}\n"
                        f"{esc(old)} → <b>{level:,.2f}</b>\nLTP {ltp:,.2f} · entry {pos.entry_price:,.2f}",
                        ref, pos.id)

    def _set_targets(self, pos: Position, targets: List[float], ref: str) -> None:
        with self.lock:
            if pos.status not in (OPEN, PENDING):
                self._already_done(pos, ref)
                return
            pos.targets = sorted(targets)
            reached = max(pos.entry_price, pos.last_ltp) if pos.status == OPEN else 0.0
            pos.targets_hit = [t for t in pos.targets if t <= reached]
            listed = " / ".join(f"{t:g}" for t in pos.targets)
            log.info("Targets %s: %s", pos.tradingsymbol, listed)
            self.notify(f"🎯 <b>Targets set</b> · {esc(pos.label)}\n<b>{esc(listed)}</b>", ref, pos.id)

    def update_from_edit(self, pos: Position, call: BuyCall, ref: str, text: str = "") -> None:
        """You edited the call. The buy level (while waiting), SL and targets
        follow the edit, and so does a later "Buy again"."""
        if (call.symbol, call.opt_type, call.strike) != (pos.symbol, pos.opt_type, pos.strike):
            if pos.status in (OPEN, PENDING):
                self.notify(f"⚠️ The edit changes the contract. {esc(pos.label)} is unchanged — "
                            "tag it with Exit and post a new call.", ref, pos.id)
            return
        if text:
            pos.signal_text = text.strip()[:500]
        if pos.status not in (OPEN, PENDING):
            self.save()
            return
        if pos.status == PENDING:
            with self.lock:
                pos.mode, pos.level = call.mode, call.level
                pos.sl, pos.no_sl = call.sl, call.no_sl
                pos.targets = list(call.targets)
                self.notify(f"✏️ Edit applied to the waiting {esc(pos.label)}: {esc(pos.entry_text())}, "
                            f"SL {esc(pos.sl_text())}.", ref, pos.id)
        else:
            if call.no_sl or call.sl is not None:
                self._set_sl(pos, FollowUp(sl="NONE" if call.no_sl else "PRICE", sl_value=call.sl or 0.0), ref)
            if call.targets and call.targets != pos.targets:
                self._set_targets(pos, call.targets, ref)
        self.save()

    def exit_all(self, ref: str) -> None:
        active = self.active()
        if not active:
            self.notify("ℹ️ Exit all — nothing is open.", ref, None)
            return
        try:
            quotes = self.quotes([p.key for p in active if p.status == OPEN])
        except Exception as exc:
            log.warning("Exit-all quotes failed (%s) — using last prices", exc)
            quotes = {}
        closed = dropped = 0
        with self.lock:
            for pos in active:
                if pos.status == PENDING:
                    self._cancel(pos, "Exit all", ref)
                    dropped += 1
                elif pos.status == OPEN:
                    quote = quotes.get(pos.key)
                    price = (quote.sell_price() if quote else 0.0) or pos.last_ltp or pos.entry_price
                    self._exit(pos, pos.lots, price, "EXIT ALL", ref,
                               "" if quote else "No live quote — used the last traded price.")
                    closed += 1
        self.save()
        self.notify(f"🧹 <b>Exit all done</b> · {closed} closed, {dropped} waiting call(s) dropped\n"
                    f"{self.pnl_text()}", ref, None)

    # ══════════════════════════════════════════════════════════════════════════
    #  Monitor — called every second
    # ══════════════════════════════════════════════════════════════════════════

    def tick(self) -> bool:
        """One pass over every open and waiting trade. True while any of
        their exchanges is open, so the caller can slow down otherwise."""
        active = self.active()
        if not active:
            self._quote_fail_since = None
            return False
        quotes = self.quotes([p.key for p in active])
        self._track_outage(bool(quotes))
        now = clock.now()
        changed = False
        with self.lock:
            for pos in active:
                if pos.status not in (OPEN, PENDING):
                    continue
                quote = quotes.get(pos.key)
                if quote:
                    pos.last_ltp = quote.ltp
                if pos.status == PENDING:
                    changed |= self._check_waiting(pos, quote, now)
                else:
                    changed |= self._check_open(pos, quote, now)
        if changed:
            self.save()
        return any(clock.is_open(p.exchange, now) for p in active)

    def _square_off_due(self, pos: Position, now: datetime) -> bool:
        """Due at the square-off of the day the trade started, or of its expiry
        day when AUTO_SQUARE_OFF is off."""
        if pos.status == OPEN and not config.AUTO_SQUARE_OFF:
            day = clock.as_date(pos.expiry)
        else:
            day = (clock.parse_iso(pos.entry_time or pos.created_at) or now).date()
        if day is None:
            return False
        due = clock.IST.localize(datetime.combine(day, clock.square_off_time(pos.exchange, day)))
        return now >= due

    def _check_waiting(self, pos: Position, quote: Optional[Quote], now: datetime) -> bool:
        reason = ""
        if self._square_off_due(pos, now):
            reason = "Square-off time reached"
        elif not clock.is_open(pos.exchange, now):
            reason = f"{pos.exchange} closed"
        if reason:
            self._cancel(pos, reason, pos.signal_ref)
            return True
        if quote is None or not self._entry_ready(pos, quote):
            return False
        error = self._fill(pos, quote)
        if error:
            self._cancel(pos, error, pos.signal_ref)
        return True

    def _check_open(self, pos: Position, quote: Optional[Quote], now: datetime) -> bool:
        expiry = clock.as_date(pos.expiry)
        if self._square_off_due(pos, now) or (expiry and expiry < now.date()):
            price = quote.sell_price() if quote else pos.last_ltp
            reason = "SQUARE-OFF" if self._square_off_due(pos, now) else "EXPIRED"
            return self._exit(pos, pos.lots, price, reason, pos.signal_ref,
                              "" if quote else "No live quote — used the last price.")
        if quote is None or not clock.is_open(pos.exchange, now):
            return False
        if pos.sl is not None and quote.ltp <= pos.sl:
            return self._exit(pos, pos.lots, quote.sell_price(), "SL HIT", pos.signal_ref,
                              f"LTP {quote.ltp:,.2f} ≤ SL {pos.sl:,.2f}")
        changed = False
        for number, target in enumerate(pos.targets, start=1):
            if target not in pos.targets_hit and quote.ltp >= target:
                pos.targets_hit.append(target)
                changed = True
                gain = (quote.ltp - pos.entry_price) * pos.lots * pos.multiplier
                lines = [f"🎯 <b>Target {number} {target:g} reached</b> · {esc(pos.label)}",
                         f"LTP {quote.ltp:,.2f} · open P&amp;L {money(gain)}"]
                lines.append("Still open — tag the call with Book / Book half / SL … to act.")
                self.notify("\n".join(lines), pos.signal_ref, pos.id)
        return changed

    def _track_outage(self, ok: bool) -> None:
        if ok:
            if self._quote_alerted:
                self.notify("✅ Live quotes are back.", None, None)
            self._quote_fail_since, self._quote_alerted = None, False
            return
        if self._quote_fail_since is None:
            self._quote_fail_since = time.monotonic()
        elif not self._quote_alerted and \
                time.monotonic() - self._quote_fail_since > config.ENGINE["quote_outage_alert_sec"]:
            self._quote_alerted = True
            self.notify("⚠️ No live quotes from Kite for a minute — SL checks are paused until they return.",
                        None, None)

    # ══════════════════════════════════════════════════════════════════════════
    #  Fills out
    # ══════════════════════════════════════════════════════════════════════════

    def _exit(self, pos: Position, lots: int, price: float, reason: str,
              reply_to: Optional[str], note: str = "") -> bool:
        if price <= 0:
            log.warning("No price to exit %s (%s) — will retry", pos.tradingsymbol, reason)
            return False
        lots = max(1, min(int(lots), pos.lots))
        price = _round_tick(price, pos.tick_size)
        now = clock.now().isoformat()
        pnl = round((price - pos.entry_price) * lots * pos.multiplier, 2)
        pos.exits.append({"time": now, "lots": lots, "price": price, "pnl": pnl, "reason": reason})
        pos.costs.append({"time": now, "amount": config.TRADE["cost_per_order"]})
        pos.lots -= lots
        log.info("SELL %s %d lots @ %.2f (%s) pnl %.2f", pos.tradingsymbol, lots, price, reason, pnl)

        if pos.lots > 0:
            lines = [
                f"🟨 <b>BOOKED {lots} OF {pos.initial_lots} LOTS · {esc(pos.label)}</b> · PAPER",
                f"Sold {_lots(lots)} at {price:,.2f} (bought {pos.entry_price:,.2f}) → <b>{money(pnl)}</b>",
                f"{_lots(pos.lots)} still open · SL {esc(pos.sl_text())}",
            ]
        else:
            pos.status = CLOSED
            pos.closed_at = now
            pos.close_reason = " + ".join(dict.fromkeys(e["reason"] for e in pos.exits))
            self._journal(pos)
            net = pos.gross - pos.cost_total
            title = reason if "SL HIT" in reason else "EXITED"
            lines = [
                f"{'🟩' if net >= 0 else '🟥'} <b>{title} · {esc(pos.label)}</b> · PAPER",
                esc(pos.close_reason),
                "",
                f"Bought {pos.entry_price:,.2f} → sold {pos.avg_exit:,.2f} × {_lots(pos.initial_lots)} "
                f"({pos.initial_lots * pos.multiplier} qty)",
                f"<b>P&amp;L {money(net)}</b> (costs ₹{pos.cost_total:,.0f})",
            ]
        if note:
            lines.append(f"<i>{esc(note)}</i>")
        self.notify("\n".join(lines), reply_to, pos.id)
        return True

    def _cancel(self, pos: Position, reason: str, reply_to: Optional[str]) -> None:
        pos.status = CANCELLED
        pos.closed_at = clock.now().isoformat()
        pos.close_reason = reason
        log.info("Dropped waiting call %s: %s", pos.tradingsymbol, reason)
        self.notify(f"🚫 <b>Call dropped</b> · {esc(pos.label)}\n{esc(reason)}. Nothing was bought.",
                    reply_to, pos.id)

    def _journal(self, pos: Position) -> None:
        path = config.PATHS["trade_journal"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        new = not os.path.exists(path)
        row = {
            "exit_time": pos.closed_at,
            "entry_time": pos.entry_time,
            "mode": "paper",
            "symbol": pos.symbol,
            "contract": pos.tradingsymbol,
            "expiry": pos.expiry,
            "strike": f"{pos.strike:g}",
            "type": pos.opt_type,
            "lots": pos.initial_lots,
            "multiplier": pos.multiplier,
            "entry_price": pos.entry_price,
            "exit_price": round(pos.avg_exit, 2),
            "gross_pnl": round(pos.gross, 2),
            "costs": round(pos.cost_total, 2),
            "pnl": round(pos.gross - pos.cost_total, 2),
            "exit_reason": pos.close_reason,
            "sl": pos.sl_text(),
            "targets": "/".join(f"{t:g}" for t in pos.targets),
            "signal": pos.signal_text.replace("\n", " | "),
        }
        try:
            with open(path, "a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=JOURNAL_COLUMNS)
                if new:
                    writer.writeheader()
                writer.writerow(row)
        except OSError as exc:
            log.error("Could not write the trade journal: %s", exc)
        self.ledger.refresh()

    # ══════════════════════════════════════════════════════════════════════════
    #  Messages
    # ══════════════════════════════════════════════════════════════════════════

    def _notify_opened(self, pos: Position) -> None:
        targets = " / ".join(f"{t:g}" for t in pos.targets) or "—"
        lines = [
            f"✅ <b>BOUGHT {esc(pos.label)}</b> · PAPER",
            f"{esc(pos.tradingsymbol)} · {esc(pos.entry_text())}",
            "",
            f"Entry <b>{pos.entry_price:,.2f}</b> × {_lots(pos.lots)} ({pos.lots * pos.multiplier} qty)",
            f"SL <b>{esc(pos.sl_text())}</b> · Targets {esc(targets)}",
        ]
        if pos.sl_note:
            lines.append(f"<i>{esc(pos.sl_note)}</i>")
        self.notify("\n".join(lines), pos.signal_ref, pos.id)

    def _notify_waiting(self, pos: Position, quote: Quote) -> None:
        cap = self._cap(pos)
        if pos.mode == "ABOVE":
            when = f"the premium is between {pos.level:g} and {cap:,.2f}"
        else:
            when = f"the premium is at or under {cap:,.2f}"
        self.notify(
            f"⏳ <b>WAITING · {esc(pos.label)}</b> · {esc(pos.entry_text())}\n"
            f"Buys {_lots(pos.lots)} when {when} (LTP now {quote.ltp:,.2f})\n"
            f"SL {esc(pos.sl_text())} · valid until today's square-off",
            pos.signal_ref, pos.id,
        )

    def pnl_text(self) -> str:
        today = clock.today().isoformat()
        realised = costs = 0.0
        with self.lock:
            positions = list(self.positions.values())
        for pos in positions:
            realised += sum(float(e["pnl"]) for e in pos.exits if e["time"][:10] == today)
            costs += sum(float(c["amount"]) for c in pos.costs if c["time"][:10] == today)
        open_pnl = sum(p.open_pnl() for p in positions)
        return f"Today booked <b>{money(realised - costs)}</b> · open P&amp;L {money(open_pnl)}"

    # ══════════════════════════════════════════════════════════════════════════
    #  /op and /pnl
    # ══════════════════════════════════════════════════════════════════════════

    def open_positions_report(self) -> str:
        active = sorted(self.active(), key=lambda p: p.created_at)
        if not active:
            return "📋 <b>OPEN POSITIONS</b> · PAPER\n\nNothing is open or waiting."
        lines = [f"📋 <b>OPEN POSITIONS ({len(active)})</b> · PAPER"]
        for pos in active:
            targets = " / ".join(f"{t:g}" for t in pos.targets) or "—"
            if pos.status == PENDING:
                lines += ["", f"⏳ <b>{esc(pos.label)}</b> · waiting · {esc(pos.entry_text())}",
                          f"LTP {pos.last_ltp:,.2f} · SL {esc(pos.sl_text())} · Tgt {esc(targets)}"]
            else:
                pnl = pos.open_pnl()
                lines += ["", f"{'🟢' if pnl >= 0 else '🔴'} <b>{esc(pos.label)}</b> · {_lots(pos.lots)} "
                              f"({pos.lots * pos.multiplier} qty)",
                          f"Entry {pos.entry_price:,.2f} · LTP {pos.last_ltp:,.2f} · <b>{money(pnl)}</b>",
                          f"SL {esc(pos.sl_text())} · Tgt {esc(targets)}"]
        return "\n".join(lines)

    def pnl_report(self) -> str:
        today = clock.today().isoformat()
        with self.lock:
            positions = sorted(self.positions.values(), key=lambda p: p.created_at)
        rows, booked, open_pnl = [], 0.0, 0.0
        closed = wins = 0
        for pos in positions:
            day = sum(float(e["pnl"]) for e in pos.exits if e["time"][:10] == today) \
                - sum(float(c["amount"]) for c in pos.costs if c["time"][:10] == today)
            touched = pos.entry_time[:10] == today or any(e["time"][:10] == today for e in pos.exits)
            if not touched:
                continue
            booked += day
            if pos.status == CLOSED:
                closed += 1
                wins += pos.gross - pos.cost_total >= 0
                rows.append(f"{'🟩' if day >= 0 else '🟥'} {esc(pos.label)} · {money(day)} · {esc(pos.close_reason)}")
            elif pos.status == OPEN:
                open_pnl += pos.open_pnl()
                part = f"booked {money(day)} · " if pos.exits else ""
                rows.append(f"🟢 {esc(pos.label)} · {part}open {money(pos.open_pnl())}")
        lines = [f"📊 <b>P&amp;L · {clock.now().strftime('%d %b %Y')}</b> · PAPER", ""]
        lines += rows or ["No trades today."]
        lines += [
            "",
            f"Booked <b>{money(booked)}</b> · {closed} closed · {wins} won · {closed - wins} lost",
            f"Open {money(open_pnl)}",
            f"<b>Total {money(booked + open_pnl)}</b> (after ₹{config.TRADE['cost_per_order']:g}/order costs)",
        ]
        return "\n".join(lines)
