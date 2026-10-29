"""
data/instruments.py
===================
Option contracts from the Kite instrument dumps (NFO, BFO, MCX).

Only the nearest option expiry of each name is kept, because every trade is
on the nearest expiry. Downloaded once per day and cached in state/.

Expiry dates, strikes and lot sizes come from the dump; only the MCX
multipliers live in config, because Kite lists MCX lots as 1.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

import config
import signal_parser
from core import clock
from core.logging_setup import get
from data.kite_gateway import KiteGateway

log = get("instruments")


@dataclass(frozen=True)
class Contract:
    symbol: str
    token: int
    tradingsymbol: str
    exchange: str
    expiry: date
    strike: float
    opt_type: str            # CE / PE
    lot_size: int
    tick_size: float

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"

    @property
    def multiplier(self) -> int:
        return int(config.symbol_cfg(self.symbol).get("multiplier") or self.lot_size or 1)


class InstrumentMaster:
    def __init__(self, gateway: KiteGateway) -> None:
        self.gw = gateway
        self._loaded_for: Optional[date] = None
        self._options: Dict[str, Dict[Tuple[float, str], Contract]] = {}
        self._expiry: Dict[str, date] = {}

    # ─── Loading ──────────────────────────────────────────────────────────────

    def _cache_path(self) -> str:
        return os.path.join(config.STATE_DIR, f"instruments_{clock.datestamp()}.json")

    def ensure_loaded(self) -> bool:
        if self._loaded_for == clock.today() and self._options:
            return True
        rows = self._load_cache()
        if rows is None:
            rows = self._download()
            if rows:
                self._save_cache(rows)
        if not rows:
            log.error("No instruments available")
            return False
        self._index(rows)
        self._loaded_for = clock.today()
        return bool(self._options)

    @staticmethod
    def _wanted(row: dict) -> bool:
        name, exchange = row.get("name"), row.get("exchange")
        if name in config.SYMBOLS:
            return exchange == config.SYMBOLS[name]["exchange"]
        return config.NFO_STOCK_OPTIONS and exchange == "NFO"

    def _download(self) -> List[dict]:
        kept: List[dict] = []
        for exchange in config.exchanges():
            for row in self.gw.instruments(exchange):
                if row.get("instrument_type") not in ("CE", "PE") or not self._wanted(row):
                    continue
                expiry = clock.as_date(row.get("expiry"))
                if expiry is None or expiry < clock.today():
                    continue
                kept.append({
                    "symbol": row["name"],
                    "token": int(row["instrument_token"]),
                    "tradingsymbol": row["tradingsymbol"],
                    "exchange": row["exchange"],
                    "expiry": expiry.isoformat(),
                    "strike": float(row.get("strike") or 0.0),
                    "type": row["instrument_type"],
                    "lot_size": int(row.get("lot_size") or 1),
                    "tick_size": float(row.get("tick_size") or 0.05),
                })

        nearest: Dict[str, str] = {}
        for row in kept:
            if row["expiry"] < nearest.get(row["symbol"], "9999"):
                nearest[row["symbol"]] = row["expiry"]
        slim = [row for row in kept if row["expiry"] == nearest[row["symbol"]]]
        log.info("Kept %d contracts for %d names (nearest expiry only)", len(slim), len(nearest))
        return slim

    def _load_cache(self) -> Optional[List[dict]]:
        path = self._cache_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                rows = json.load(fh)
            log.info("Loaded %d contracts from today's cache", len(rows))
            return rows or None
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Instrument cache unreadable (%s) — downloading again", exc)
            return None

    def _save_cache(self, rows: List[dict]) -> None:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        try:
            with open(self._cache_path(), "w", encoding="utf-8") as fh:
                json.dump(rows, fh)
            for name in os.listdir(config.STATE_DIR):
                if name.startswith("instruments_") and name != os.path.basename(self._cache_path()):
                    os.remove(os.path.join(config.STATE_DIR, name))
        except OSError as exc:
            log.warning("Could not write instrument cache: %s", exc)

    def _index(self, rows: List[dict]) -> None:
        self._options.clear()
        self._expiry.clear()
        for row in rows:
            if row["type"] not in ("CE", "PE"):
                continue
            contract = Contract(
                symbol=row["symbol"],
                token=row["token"],
                tradingsymbol=row["tradingsymbol"],
                exchange=row["exchange"],
                expiry=clock.as_date(row["expiry"]),
                strike=float(row["strike"]),
                opt_type=row["type"],
                lot_size=row["lot_size"],
                tick_size=row["tick_size"],
            )
            self._options.setdefault(contract.symbol, {})[(contract.strike, contract.opt_type)] = contract
            self._expiry[contract.symbol] = contract.expiry
        signal_parser.register_names(n for n in self._options if n not in config.SYMBOLS)
        for symbol in config.SYMBOLS:
            if symbol in self._expiry:
                log.info("%-10s %d options, expiry %s", symbol, len(self._options[symbol]),
                         self._expiry[symbol].isoformat())
            else:
                log.warning("%-10s no option contracts found", symbol)
        others = len(self._options) - sum(1 for s in config.SYMBOLS if s in self._options)
        if others:
            log.info("NFO stock / index options: %d names", others)

    # ─── Lookups ──────────────────────────────────────────────────────────────

    def expiry(self, symbol: str) -> Optional[date]:
        """The nearest option expiry, today's on expiry day."""
        expiry = self._expiry.get(symbol)
        return expiry if expiry is not None and expiry >= clock.today() else None

    def strikes(self, symbol: str) -> List[float]:
        return sorted({key[0] for key in self._options.get(symbol, {})})

    def option(self, symbol: str, strike: float, opt_type: str) -> Optional[Contract]:
        return self._options.get(symbol, {}).get((float(strike), opt_type))
