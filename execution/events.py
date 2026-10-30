"""One decision from the signal book, copied to every linked account."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CopyEvent:
    action: str                          # BUY / SELL
    position_id: str
    symbol: str
    exchange: str
    tradingsymbol: str
    tick_size: float
    multiplier: int                      # rupees of P&L per point, per lot
    lot_size: int                        # units to send the exchange per lot
    lots: int                            # lots this action covers on the master
    lots_before: int                     # master lots open before a sell
    price: float                         # reference premium (ask on buy, bid on sell)
    sl: Optional[float]
    reason: str
    signal_ref: str
    exact: bool = False                  # limit at price exactly; ignore account slippage
