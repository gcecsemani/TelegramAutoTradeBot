"""Broker-neutral order types. Adapters translate these; the engine does not."""

from __future__ import annotations

from dataclasses import dataclass


class BrokerError(RuntimeError):
    """The broker refused the order. Do not retry."""


class TokenError(BrokerError):
    """The access token is dead. Retry only after the user logs in again."""


class TransientError(RuntimeError):
    """Timeout or a rate limit. The order may or may not exist."""


@dataclass(frozen=True)
class OrderRequest:
    exchange: str
    tradingsymbol: str
    side: str                            # BUY / SELL
    quantity: int
    product: str                         # MIS / NRML
    order_type: str                      # LIMIT / MARKET / SL-M
    price: float
    validity: str                        # IOC / DAY
    tag: str
    trigger_price: float = 0.0           # sell stop trigger for SL-M


@dataclass(frozen=True)
class OrderView:
    order_id: str
    status: str                          # COMPLETE / REJECTED / CANCELLED / OPEN / UNKNOWN
    average_price: float
    filled_quantity: int
    message: str
