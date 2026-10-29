"""
core/ledger.py
==============
Daily and monthly P&L, rebuilt from the trade journal after every closed trade.

    state/trades.csv            one row per closed trade
    state/ledger_daily.csv      one row per day
    state/ledger_monthly.csv    one row per month

`net_pnl` is already net of costs.
"""

from __future__ import annotations

import csv
import os
from typing import Dict, List

import config
from core.logging_setup import get

log = get("ledger")

_DAILY_COLUMNS = ["date", "trades", "wins", "losses", "win_rate_pct", "net_pnl", "costs", "by_symbol", "result"]
_MONTHLY_COLUMNS = ["month", "trading_days", "trades", "wins", "losses", "win_rate_pct", "net_pnl", "costs",
                    "best_day", "best_day_pnl", "worst_day", "worst_day_pnl", "result"]


def _num(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _result(pnl: float) -> str:
    return "PROFIT" if pnl > 0 else ("LOSS" if pnl < 0 else "FLAT")


def _write(path: str, columns: List[str], rows: List[Dict[str, object]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


class PerformanceLedger:
    def refresh(self) -> None:
        path = config.PATHS["trade_journal"]
        if not os.path.exists(path):
            return
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                trades = [row for row in csv.DictReader(fh) if len(str(row.get("exit_time") or "")) >= 10]
            days = self._days(trades)
            _write(config.PATHS["ledger_daily"], _DAILY_COLUMNS, days)
            _write(config.PATHS["ledger_monthly"], _MONTHLY_COLUMNS, self._months(days))
        except OSError as exc:
            log.error("Could not refresh the P&L ledger: %s", exc)

    @staticmethod
    def _days(trades: List[Dict[str, str]]) -> List[Dict[str, object]]:
        grouped: Dict[str, List[Dict[str, str]]] = {}
        for row in trades:
            grouped.setdefault(row["exit_time"][:10], []).append(row)
        out = []
        for day in sorted(grouped):
            rows = grouped[day]
            pnl = sum(_num(r.get("pnl")) for r in rows)
            wins = sum(1 for r in rows if _num(r.get("pnl")) >= 0)
            by_symbol: Dict[str, float] = {}
            for r in rows:
                by_symbol[r.get("symbol", "?")] = by_symbol.get(r.get("symbol", "?"), 0.0) + _num(r.get("pnl"))
            out.append({
                "date": day,
                "trades": len(rows),
                "wins": wins,
                "losses": len(rows) - wins,
                "win_rate_pct": round(wins / len(rows) * 100.0, 1),
                "net_pnl": round(pnl, 2),
                "costs": round(sum(_num(r.get("costs")) for r in rows), 2),
                "by_symbol": " ".join(f"{k}:{v:.0f}" for k, v in sorted(by_symbol.items())),
                "result": _result(pnl),
            })
        return out

    @staticmethod
    def _months(days: List[Dict[str, object]]) -> List[Dict[str, object]]:
        grouped: Dict[str, List[Dict[str, object]]] = {}
        for row in days:
            grouped.setdefault(str(row["date"])[:7], []).append(row)
        out = []
        for month in sorted(grouped):
            rows = grouped[month]
            pnl = sum(_num(r["net_pnl"]) for r in rows)
            trades = int(sum(_num(r["trades"]) for r in rows))
            wins = int(sum(_num(r["wins"]) for r in rows))
            best = max(rows, key=lambda r: _num(r["net_pnl"]))
            worst = min(rows, key=lambda r: _num(r["net_pnl"]))
            out.append({
                "month": month,
                "trading_days": len(rows),
                "trades": trades,
                "wins": wins,
                "losses": trades - wins,
                "win_rate_pct": round(wins / trades * 100.0, 1) if trades else "",
                "net_pnl": round(pnl, 2),
                "costs": round(sum(_num(r["costs"]) for r in rows), 2),
                "best_day": best["date"], "best_day_pnl": best["net_pnl"],
                "worst_day": worst["date"], "worst_day_pnl": worst["net_pnl"],
                "result": _result(pnl),
            })
        return out
