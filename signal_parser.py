"""
signal_parser.py
================
Reads exactly these messages. Anything else is ignored.

BUY CALL (its own message)

    NIFTY 22800PE
    Buy Above 100      buy once the premium is at or above 100, at most 2.5% over it
    Buy Near 90        buy at any price up to 2.5% over 90
    Buy Near 100-105   buy at 105 or below (inside the range, or under it)
    Buy only 100       buy only when the ask is 100, with no band
    Buy CMP            buy at the current price
    Book near 130      once open, sell everything at 130 or above
    Book 50 near 130   once open, sell 50% at 130 or above
    Sl 80              optional
    Tgt 120 140 160++  optional

TAGGED BUY (reply on a message that only names the option)

    Sensex 72800 PE | ADD TO WATCHLIST
        ↳ Buy CMP                 buys that option at the current price
        ↳ Buy Above 120           same, with that entry
        ↳ Buy Near 110
        ↳ Buy CMP / Sl 80 / Tgt 120   SL and targets on the reply are optional

    Buy Above and Buy Near need a price. The option comes from the tagged message.

FOLLOW-UP (a reply that tags the call, or the bot's message about it)

    SL 70                                   SL to 70
    SL to cost · SL at cost · SL at CTC · SL CTC      SL to the entry price
    SL to t1 · SL to t2 · SL to t3          SL to that target
    No sl · Hero zero · Hero to Zero        no SL
    Exit · Book · Book full · Book fully · Ignore · Avoid · Cancel · Close · Sell · SL hit
                                            exit every lot (or drop a call still waiting)
    Book half · Book 50 · Book 50%          exit half the lots now
    Book near 130 · Book 50 near 130        sell all, or that percent, when price is at or above 130
    Tgt 110 · Tgt 110 120 130 · Target 110 · Target 110 120 130    new targets
    Buy again                               the same call again, once the last one is closed
    Buy again Above 100 · Buy again Near 100 · Buy again CMP
                                            same call, but this entry instead of the original one

EXIT ALL (its own message): "Exit all" closes every trade and drops every waiting call.

COMMANDS (update group only): /op open positions · /pnl today's P&L · /trades the trades file
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import config

NUM = r"\d+(?:\.\d+)?"
_END = r"(?![\d.])"

CONTRACT_RE = re.compile(r"(?<![\d.])(" + NUM + r")\s*-?\s*(CE|PE|CALL|PUT)\b")
WORD_RE = re.compile(r"[A-Z0-9&\-]+")

BUY_WORD_RE = re.compile(r"\bBUY\b")
BUY_RANGE_RE = re.compile(
    r"\bBUY\s*[:\-@]?\s*NEAR\s*[:\-@]?\s*(" + NUM + r")\s*-\s*(" + NUM + r")" + _END
)
BUY_ONLY_RE = re.compile(
    r"\bBUY\s*[:\-@]?\s*(?:ONLY\s*[:\-@]?\s*(" + NUM + r")" + _END + r"|(" + NUM + r")\s*ONLY\b)"
)
BUY_RE = re.compile(r"\bBUY\s*[:\-@]?\s*(?:(ABOVE|NEAR)\s*[:\-@]?\s*(" + NUM + r")" + _END + r"|(CMP)\b)")
BOOK_AT_RE = re.compile(
    r"\bBOOK\s*(?:(" + NUM + r")\s*%?\s+)?NEAR\s*[:\-@]?\s*(" + NUM + r")" + _END
)
_INCOMPLETE_ENTRY_RE = re.compile(
    r"\bBUY\s*[:\-@]?\s*(?:ABOVE|NEAR)\b(?!\s*[:\-@]?\s*" + NUM + _END + r")"
)
_BARE_BUY_PRICE_RE = re.compile(r"\bBUY\s*[:\-@]?\s*" + NUM + _END)
SELL_RE = re.compile(r"\b(?:SELL|SHORT|WRITE)\b")

SL_PRICE_RE = re.compile(r"\bSL\s*(?:TO|AT|@|:|-|=)?\s*(" + NUM + r")" + _END)
SL_COST_RE = re.compile(r"\bSL\s*(?:TO|AT|@|:|-|=)?\s*(?:COST|CTC)\b")
SL_TARGET_RE = re.compile(r"\bSL\s*(?:TO|AT|@|:|-|=)?\s*T(?:GT|ARGET)?\s*-?\s*([1-9])\b")
NO_SL_RE = re.compile(r"\bNO\s*SL\b|\bHERO\s*(?:TO\s*)?ZERO\b")
TARGET_RE = re.compile(r"\b(?:TGT|TARGET)S?\s*[:\-=]?\s*(" + NUM + r"(?:\s*[/,\-]?\s*" + NUM + r")*)")

EXIT_ALL_RE = re.compile(r"(?:EXIT|CLOSE)\s+ALL(?:\s+(?:POSITIONS?|TRADES?))?")
BOOK_HALF_RE = re.compile(r"\bBOOK\s*(?:HALF|50\s*%?" + _END + r")")
EXIT_RE = re.compile(r"\b(?:EXIT|BOOK|IGNORE|AVOID|CANCEL|CLOSE|SELL)\b|\bSL\s*HIT\b")
BUY_AGAIN_RE = re.compile(
    r"\bBUY\s*AGAIN"
    r"(?:"
    r"\s*[:\-@]?\s*(?:(ABOVE|NEAR)\s*[:\-@]?\s*(" + NUM + r")" + _END + r"|(CMP)\b)"
    r"|(\s*[:\-@]?\s*(?:ABOVE|NEAR)\b)"
    r")?"
)
COMMAND_RE = re.compile(r"/(OP|PNL|TRADES)(?:@\w+)?", re.IGNORECASE)
NOT_EXIT_RE = re.compile(r"\b(?:DONT|DO\s+NOT|NOT|NEVER)\s+(?:\w+\s+)?(?:EXIT|BOOK|IGNORE|AVOID|CANCEL|CLOSE|SELL)\b")

# Normalised NFO option name -> Kite name, e.g. "BAJAJAUTO" -> "BAJAJ-AUTO".
_STOCKS: Dict[str, str] = {}


def register_names(names: Iterable[str]) -> None:
    _STOCKS.clear()
    for name in names:
        _STOCKS[re.sub(r"[^A-Z0-9]", "", name.upper())] = name


@dataclass
class BuyCall:
    symbol: str
    strike: float
    opt_type: str                        # CE / PE
    mode: str                            # ABOVE / NEAR / RANGE / ONLY / CMP
    level: Optional[float] = None        # Above / Near / Only price, or the low of a range
    level_high: Optional[float] = None   # top of "Buy Near 100-105"; buy at or below this
    book_level: Optional[float] = None   # sell when the premium reaches this
    book_pct: float = 100.0              # 100 = all, 50 = half, used with book_level
    sl: Optional[float] = None
    no_sl: bool = False
    targets: List[float] = field(default_factory=list)

    def contract_text(self) -> str:
        return f"{self.symbol} {self.strike:g} {self.opt_type}"

    def as_text(self) -> str:
        """The call as a message, so a later Buy again can read it back."""
        lines = [self.contract_text(), self.entry_line()]
        if self.no_sl:
            lines.append("Hero zero")
        elif self.sl is not None:
            lines.append(f"Sl {self.sl:g}")
        if self.targets:
            lines.append("Tgt " + " ".join(f"{t:g}" for t in self.targets))
        if self.book_level:
            if self.book_pct >= 100:
                lines.append(f"Book near {self.book_level:g}")
            else:
                lines.append(f"Book {self.book_pct:g} near {self.book_level:g}")
        return "\n".join(lines)

    def entry_line(self) -> str:
        if self.mode == "CMP":
            return "Buy CMP"
        if self.mode == "ONLY":
            return f"Buy only {self.level:g}"
        if self.mode == "RANGE":
            return f"Buy Near {self.level:g}-{self.level_high:g}"
        return f"Buy {self.mode.title()} {self.level:g}"


@dataclass
class FollowUp:
    buy_again: bool = False
    entry_mode: str = ""                 # "" keeps the call's entry; else ABOVE / NEAR / CMP
    entry_level: Optional[float] = None
    exit: str = ""                       # "" / FULL / HALF
    book_level: Optional[float] = None   # Book near 130, once the price is there
    book_pct: float = 100.0
    sl: str = ""                         # "" / PRICE / COST / TARGET / NONE
    sl_value: float = 0.0                # the price, or the target number
    targets: List[float] = field(default_factory=list)


@dataclass
class Parsed:
    kind: str                            # BUY / FOLLOW_UP / EXIT_ALL / INVALID / NONE
    call: Optional[BuyCall] = None
    follow_up: Optional[FollowUp] = None
    error: str = ""
    from_tag: bool = False               # the option was on the tagged message, the entry on the reply


def normalize(text: str) -> str:
    t = (text or "").upper().replace("’", "").replace("'", "")
    t = re.sub(r"(?<=\d),(?=\d)", "", t)                 # 1,25,000 -> 125000
    t = re.sub(r"[^A-Z0-9.%@/:&,\-\s]", " ", t)           # emojis, ++, ₹ …
    return re.sub(r"\s+", " ", t).strip()


def _symbol_before(text: str, start: int) -> Optional[str]:
    """The instrument named by the 1–3 words right before the strike."""
    words = list(WORD_RE.finditer(text[:start]))
    for n in (3, 2, 1):
        if len(words) < n:
            continue
        span = text[words[-n].start():words[-1].end()]
        for name, cfg in config.SYMBOLS.items():
            if any(re.fullmatch(alias, span) for alias in cfg["aliases"]):
                return name
        name = _STOCKS.get(re.sub(r"[^A-Z0-9]", "", span))
        if name:
            return name
    return None


def _targets(text: str) -> List[float]:
    m = TARGET_RE.search(text)
    if not m:
        return []
    return sorted({float(v) for v in re.findall(NUM, m.group(1))})


def parse_call(t: str) -> Tuple[Optional[BuyCall], str]:
    """(call, "") · (None, reason) for a broken call · (None, "") for anything else."""
    contracts = list(CONTRACT_RE.finditer(t))
    if not contracts or not BUY_WORD_RE.search(t):
        return None, ""
    if len(contracts) > 1 or SELL_RE.search(t):
        return None, "Only one option buy per call — no selling, spreads or hedges."
    contract = contracts[0]
    symbol = _symbol_before(t, contract.start())
    if symbol is None:
        return None, "Instrument not recognised. Write it before the strike, e.g. NIFTY 22800PE."
    found, error = _read_entry(t)
    if error:
        return None, error
    if found is None:
        return None, "Write the entry as Buy Above 100, Buy Near 90, Buy Near 100-105, Buy only 100 or Buy CMP."
    mode, level, level_high, buy = found
    call = BuyCall(symbol=symbol, strike=float(contract.group(1)),
                   opt_type="CE" if contract.group(2).startswith("C") else "PE",
                   mode=mode, level=level, level_high=level_high)
    _apply_sl_targets(call, t[:buy.start()] + " " + t[buy.end():])
    _apply_book(call, t)
    return call, ""


def _read_entry(t: str):
    """(mode, level, level_high, match) or (None, error). An empty error means no entry."""
    rng = BUY_RANGE_RE.search(t)
    if rng:
        low, high = float(rng.group(1)), float(rng.group(2))
        if low <= 0 or high <= 0:
            return None, "The buy level must be above 0."
        if low > high:
            low, high = high, low
        if low == high:
            return ("ONLY", low, None, rng), ""
        return ("RANGE", low, high, rng), ""
    only = BUY_ONLY_RE.search(t)
    if only:
        level = float(only.group(1) or only.group(2))
        if level <= 0:
            return None, "The buy level must be above 0."
        return ("ONLY", level, None, only), ""
    buy = BUY_RE.search(t)
    if buy is None:
        if _INCOMPLETE_ENTRY_RE.search(t) or _BARE_BUY_PRICE_RE.search(t) or re.search(r"\bBUY\s*[:\-@]?\s*ONLY\b", t):
            return None, "Write the entry as Buy Above 100, Buy Near 90, Buy Near 100-105, Buy only 100 or Buy CMP."
        return None, ""
    level = float(buy.group(2)) if buy.group(2) else None
    if level is not None and level <= 0:
        return None, "The buy level must be above 0."
    return (buy.group(1) or "CMP", level, None, buy), ""


def _apply_book(call: BuyCall, text: str) -> None:
    booked = BOOK_AT_RE.search(text)
    if not booked:
        return
    call.book_level = float(booked.group(2))
    call.book_pct = float(booked.group(1)) if booked.group(1) else 100.0
    if call.book_pct <= 0 or call.book_pct > 100:
        call.book_pct = 100.0


def _apply_sl_targets(call: BuyCall, rest: str) -> None:
    sl = SL_PRICE_RE.search(rest)
    if NO_SL_RE.search(rest) or (sl and float(sl.group(1)) == 0):
        call.no_sl = True
    elif sl:
        call.sl = float(sl.group(1))
    call.targets = _targets(rest)


def _contract_from(t: str) -> Tuple[Optional[BuyCall], str]:
    """The single option named in a tagged message."""
    contracts = list(CONTRACT_RE.finditer(t))
    if not contracts:
        return None, "The message you tagged has no option. Tag one like SENSEX 72800 PE."
    if len(contracts) > 1 or SELL_RE.search(t):
        return None, "The message you tagged must name one option only, e.g. SENSEX 72800 PE."
    contract = contracts[0]
    symbol = _symbol_before(t, contract.start())
    if symbol is None:
        return None, "Instrument not recognised. Write it before the strike, e.g. NIFTY 22800PE."
    return BuyCall(symbol=symbol, strike=float(contract.group(1)),
                   opt_type="CE" if contract.group(2).startswith("C") else "PE",
                   mode="CMP"), ""


def parse_tagged_buy(reply: str, tagged: str) -> Tuple[Optional[BuyCall], str]:
    """(call, "") when a Buy Above / Near / CMP reply sits on a message that names the option."""
    found, error = _read_entry(reply)
    if error:
        return None, error
    if found is None:
        return None, ""
    mode, level, level_high, buy = found
    call, error = _contract_from(tagged)
    if call is None:
        return None, error
    call.mode = mode
    call.level = level
    call.level_high = level_high
    _apply_sl_targets(call, reply[:buy.start()] + " " + reply[buy.end():])
    _apply_book(call, reply)
    return call, ""


def parse_follow_up(t: str) -> Tuple[Optional[FollowUp], str]:
    """(follow-up, "") · (None, reason) for a broken one · (None, "") for anything else."""
    again = BUY_AGAIN_RE.search(t)
    if again:
        if again.group(4):
            return None, "Write it as Buy again, Buy again Above 100, Buy again Near 100 or Buy again CMP."
        mode = again.group(1) or ("CMP" if again.group(3) else "")
        level = float(again.group(2)) if again.group(2) else None
        if level is not None and level <= 0:
            return None, "The buy level must be above 0."
        return FollowUp(buy_again=True, entry_mode=mode, entry_level=level), ""
    if not NOT_EXIT_RE.search(t):
        booked = BOOK_AT_RE.search(t)
        if booked:
            level = float(booked.group(2))
            pct = float(booked.group(1)) if booked.group(1) else 100.0
            if level <= 0 or pct <= 0:
                return None, "Write it as Book near 130 or Book 50 near 130."
            return FollowUp(book_level=level, book_pct=100.0 if pct > 100 else pct), ""
        if re.search(r"\bBOOK\b", t) and re.search(r"\bNEAR\b", t):
            return None, "Write it as Book near 130 or Book 50 near 130."
        if BOOK_HALF_RE.search(t):
            return FollowUp(exit="HALF"), ""
        if EXIT_RE.search(t):
            return FollowUp(exit="FULL"), ""
    fu = FollowUp(targets=_targets(t))
    target_no = SL_TARGET_RE.search(t)
    price = SL_PRICE_RE.search(t)
    if NO_SL_RE.search(t):
        fu.sl = "NONE"
    elif target_no:
        fu.sl, fu.sl_value = "TARGET", float(target_no.group(1))
    elif SL_COST_RE.search(t):
        fu.sl = "COST"
    elif price:
        fu.sl, fu.sl_value = ("NONE", 0.0) if float(price.group(1)) == 0 else ("PRICE", float(price.group(1)))
    if fu.sl or fu.targets:
        return fu, ""
    return None, ""


def parse_command(text: str) -> Optional[str]:
    """OP / PNL / TRADES for "/op", "/pnl", "/trades" (with or without @botname)."""
    m = COMMAND_RE.fullmatch((text or "").strip())
    return m.group(1).upper() if m else None


def parse(text: str, is_reply: bool = False, tagged: str = "") -> Parsed:
    t = normalize(text)
    if not t:
        return Parsed("NONE")
    if EXIT_ALL_RE.fullmatch(t):
        return Parsed("EXIT_ALL")
    call, error = parse_call(t)
    if call is not None:
        return Parsed("BUY", call=call)
    if error:
        return Parsed("INVALID", error=error)
    if is_reply and tagged.strip():
        # Before follow-ups, so "Buy CMP / Sl 80" on a watchlist line is a buy, not an SL move.
        call, error = parse_tagged_buy(t, normalize(tagged))
        if call is not None:
            return Parsed("BUY", call=call, from_tag=True)
        if error:
            return Parsed("INVALID", error=error)
    if is_reply:
        fu, error = parse_follow_up(t)
        if fu is not None:
            return Parsed("FOLLOW_UP", follow_up=fu)
        if error:
            return Parsed("INVALID", error=error)
    return Parsed("NONE")
