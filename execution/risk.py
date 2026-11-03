"""
execution/risk.py
=================
Pure sizing rules. No broker and no database, so a bad limit is refused
before an order exists.

    lots            the balance range that contains this cash, both ends included
    slippage        buy limit = reference + points, or reference + percent
    max loss/trade  drops the lot count so (entry - SL) × lots stays inside it
    max loss/day    same, using the loss budget still left today

The balance check only chooses the lot count. It does not look at open
positions, so one trade does not shrink or block the next.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BuyPlan:
    ok: bool
    lots: int
    quantity: int
    limit_price: float
    reason: str


def lots_for_balance(balance: float, bands: list) -> int:
    """Lots for the range that contains this cash. Both ends count.

    A balance in no range is 0. Open positions are not an input. When two
    ranges share an endpoint, that rupee stays with the range that ends there.
    A gap of at most ₹1 between two ranges uses the higher one, so a paisa
    between ₹50,000 and ₹50,001 is not dropped.
    """
    try:
        cash = float(balance)
    except (TypeError, ValueError):
        return 0
    parsed = _parse_bands(bands)
    if not parsed or cash < 0:
        return 0
    containing = [band for band in parsed if band[0] <= cash <= band[1]]
    if containing:
        containing.sort(key=lambda band: (band[1], -band[0]))
        return containing[0][2]
    for index, band in enumerate(parsed[:-1]):
        nxt = parsed[index + 1]
        if band[1] < cash < nxt[0] and nxt[0] - band[1] <= 1:
            return nxt[2]
    return 0


def lot_range_reason(balance: float, symbol: str, bands: list) -> str:
    """Why this cash took no lots. Used when lots_for_balance returns 0."""
    name = symbol or "symbol"
    parsed = _parse_bands(bands)
    if not parsed:
        return f"no lot range is set for {name}"
    try:
        cash = float(balance)
    except (TypeError, ValueError):
        cash = 0.0
    lowest = min(band[0] for band in parsed)
    highest = max(band[1] for band in parsed)
    if cash < lowest:
        return f"balance {_inr(cash)} is below {_inr(lowest)} for a {name} lot"
    if cash > highest:
        return f"balance {_inr(cash)} is above {_inr(highest)} for the {name} lot ranges"
    return f"balance {_inr(cash)} is between the {name} lot ranges"


def _parse_bands(bands: list) -> list:
    parsed = []
    for band in bands or []:
        try:
            low = float(band["min_balance"])
            high = float(band["max_balance"])
            lots = int(band["lots"])
        except (TypeError, ValueError, KeyError):
            continue
        if lots >= 1 and high >= low:
            parsed.append((low, high, lots))
    parsed.sort(key=lambda band: (band[0], band[1]))
    return parsed


def _inr(amount: float) -> str:
    return f"₹{float(amount):,.0f}"


def round_tick(price: float, tick: float) -> float:
    tick = tick or 0.05
    return round(round(price / tick) * tick, 2)


def order_quantity(lots: int, lot_size: int, multiplier: int) -> int:
    """Exchange quantity. NFO lot size is the order qty. MCX lists lot size 1
    and keeps the rupee multiplier separate, so those must not be mixed."""
    per = lot_size if lot_size > 0 else (multiplier or 1)
    return max(0, int(lots) * max(1, int(per)))


def buy_limit(price: float, mode: str, value: float, tick: float) -> float:
    if str(mode).upper() == "POINTS":
        raw = price + max(0.0, float(value))
    else:
        raw = price * (1 + max(0.0, float(value)) / 100.0)
    return round_tick(max(raw, price), tick)


def rupees_from_pct(balance: float, pct: float | None) -> float | None:
    """Rupees equal to this percent of cash. Blank or zero percent is no limit."""
    if pct is None:
        return None
    try:
        cash = float(balance)
        rate = float(pct)
    except (TypeError, ValueError):
        return None
    if cash <= 0 or rate <= 0:
        return None
    return cash * rate / 100.0


def plan_buy(
    *,
    lots: int,
    min_lots: int,
    max_lots: int,
    slippage_mode: str,
    slippage_value: float,
    price: float,
    sl: float | None,
    tick: float,
    multiplier: int,
    lot_size: int,
    max_loss_trade: float | None,
    max_loss_day: float | None,
    day_realized: float,
    default_sl_pct: float,
) -> BuyPlan:
    if min_lots < 1 or max_lots < min_lots:
        return BuyPlan(False, 0, 0, 0.0, "min / max lots are not usable")
    sized = min(max(int(lots), min_lots), max_lots)
    loss_per_lot = _loss_per_lot(price, sl, multiplier, default_sl_pct)
    if max_loss_trade is not None and loss_per_lot > 0:
        sized = min(sized, int(float(max_loss_trade) // loss_per_lot))
    if max_loss_day is not None and loss_per_lot > 0:
        room = float(max_loss_day) + min(float(day_realized), 0.0)
        if room <= 0:
            return BuyPlan(False, 0, 0, 0.0, "max loss per day is already reached")
        sized = min(sized, int(room // loss_per_lot))
    if sized < min_lots or sized < 1:
        return BuyPlan(False, 0, 0, 0.0, "max loss leaves fewer lots than the minimum")
    limit = buy_limit(price, slippage_mode, slippage_value, tick)
    return BuyPlan(True, sized, order_quantity(sized, lot_size, multiplier), limit, "")


def sell_lots(open_lots: int, master_lots: int, master_before: int) -> int:
    """The same fraction the master sold, at least one lot when anything is open."""
    if open_lots <= 0:
        return 0
    if master_before <= 0 or master_lots >= master_before:
        return open_lots
    n = int(round(open_lots * (master_lots / master_before)))
    if open_lots == 1:
        return 1
    return max(1, min(open_lots, n))


def loss_estimate(price: float, sl: float | None, lots: int, multiplier: int, default_sl_pct: float) -> float:
    return _loss_per_lot(price, sl, multiplier, default_sl_pct) * max(0, lots)


def _loss_per_lot(price: float, sl: float | None, multiplier: int, default_sl_pct: float) -> float:
    if price <= 0 or multiplier <= 0:
        return 0.0
    if sl is not None and price > sl:
        per_point = price - sl
    else:
        per_point = price * (default_sl_pct / 100.0)
    return per_point * multiplier
