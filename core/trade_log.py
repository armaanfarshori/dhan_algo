"""
Persistent Trade Logger
=======================
Writes every BUY / SELL / EXIT event to a JSON-lines file that survives
restarts. One record per line — easy to grep, tail, and import to pandas.

Log file: .logs/trades.jsonl
Format:   {"ts":"ISO","engine":"F&O","symbol":"NIFTY PE 24300","action":"BUY",
           "price":164.3,"qty":260,"lot_size":65,"num_lots":4,
           "bep":164.86,"target":169.86,"stop":159.3,"rsi":81.7,
           "pnl":null,"mode":"PAPER","session_id":"..."}

Usage:
    logger = TradeLogger()           # singleton via get_trade_logger()
    logger.log_entry(...)
    logger.log_exit(...)
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_logger = logging.getLogger("dhan.trade_log")

LOG_DIR  = Path(__file__).parent.parent / ".logs"
LOG_FILE = LOG_DIR / "trades.jsonl"


class TradeLogger:
    """Thread-safe (single asyncio event loop) trade logger."""

    def __init__(self):
        LOG_DIR.mkdir(exist_ok=True)
        self._session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        _logger.info(f"Trade log: {LOG_FILE}  (session {self._session_id})")

    def _write(self, record: dict):
        record["session_id"] = self._session_id
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def log_entry(
        self,
        engine:    str,          # "F&O" | "EQ"
        symbol:    str,          # "NIFTY PE 24300" | "RSYSTEMS"
        action:    str,          # "BUY" | "SELL"
        price:     float,
        qty:       int,
        mode:      str,          # "PAPER" | "LIVE"
        lot_size:  int   = 1,
        num_lots:  int   = 1,
        bep:       float = 0.0,
        target:    float = 0.0,
        stop:      float = 0.0,
        rsi:       float = 0.0,
        segment:   str   = "",
        strategy:  str   = "",
        reason:    str   = "",
    ):
        record = {
            "ts":       datetime.now(timezone.utc).isoformat(),
            "type":     "ENTRY",
            "engine":   engine,
            "symbol":   symbol,
            "segment":  segment,
            "strategy": strategy,
            "action":   action,
            "price":    round(price, 2),
            "qty":      qty,
            "lot_size": lot_size,
            "num_lots": num_lots,
            "bep":      round(bep, 2),
            "target":   round(target, 2),
            "stop":     round(stop, 2),
            "rsi":      round(rsi, 2),
            "notional": round(price * qty, 2),
            "pnl":      None,
            "mode":     mode,
            "reason":   reason,
        }
        self._write(record)
        _logger.info(
            f"[TRADE LOG] {mode} {action} {symbol} | ₹{price:.2f} × {qty} = ₹{price*qty:,.0f} "
            f"| BEP ₹{bep:.2f} | T ₹{target:.2f} | S ₹{stop:.2f}"
        )

    def log_exit(
        self,
        engine:       str,
        symbol:       str,
        action:       str,        # "EXIT"
        price:        float,
        qty:          int,
        mode:         str,
        entry_price:  float = 0.0,
        pnl:          float = 0.0,
        reason:       str   = "",
        segment:      str   = "",
        strategy:     str   = "",
    ):
        record = {
            "ts":          datetime.now(timezone.utc).isoformat(),
            "type":        "EXIT",
            "engine":      engine,
            "symbol":      symbol,
            "segment":     segment,
            "strategy":    strategy,
            "action":      action,
            "price":       round(price, 2),
            "qty":         qty,
            "entry_price": round(entry_price, 2),
            "notional":    round(price * qty, 2),
            "pnl":         round(pnl, 2),
            "pnl_pct":     round((pnl / (entry_price * qty) * 100) if entry_price and qty else 0, 2),
            "mode":        mode,
            "reason":      reason,
        }
        self._write(record)
        sign = "+" if pnl >= 0 else ""
        _logger.info(
            f"[TRADE LOG] {mode} EXIT {symbol} | ₹{price:.2f} × {qty} | PnL {sign}₹{pnl:,.2f}"
        )

    def get_trades(self, limit: int = 200) -> list:
        """Read last N trades from the log file."""
        if not LOG_FILE.exists():
            return []
        try:
            lines = LOG_FILE.read_text(encoding="utf-8").strip().split("\n")
            records = [json.loads(l) for l in lines if l.strip()]
            return records[-limit:]
        except Exception as e:
            _logger.warning(f"Trade log read error: {e}")
            return []

    def get_session_summary(self) -> dict:
        trades = self.get_trades(500)
        session = [t for t in trades if t.get("session_id") == self._session_id]
        entries = [t for t in session if t["type"] == "ENTRY"]
        exits   = [t for t in session if t["type"] == "EXIT"]
        realized = sum(t.get("pnl", 0) or 0 for t in exits)
        return {
            "session_id":    self._session_id,
            "total_entries": len(entries),
            "total_exits":   len(exits),
            "open_trades":   len(entries) - len(exits),
            "realized_pnl":  round(realized, 2),
            "log_file":      str(LOG_FILE),
        }


# ── Singleton ──────────────────────────────────────────────────────────────────
_instance: Optional[TradeLogger] = None

def get_trade_logger() -> TradeLogger:
    global _instance
    if _instance is None:
        _instance = TradeLogger()
    return _instance
