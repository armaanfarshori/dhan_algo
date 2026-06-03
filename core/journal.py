"""
Journal — unified logging layer for DhanAIBot
==============================================
Consolidates three previous modules into one clear API:

  TradeLogger   — persistent JSON-lines trade log (.logs/trades.jsonl)
  AsyncDBBackend— async TimescaleDB sink for signals + trades
  LogBuffer     — rolling in-memory buffer for /api/logs endpoint

Usage:
    # Singletons
    from core.journal import get_trade_logger, get_log_buffer, install_log_buffer

    logger = get_trade_logger()
    logger.log_entry(...)
    logger.log_exit(...)

    install_log_buffer()   # call once at startup to capture all logs
    logs = get_log_buffer().get_logs(50)
"""

import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_log = logging.getLogger("dhan.journal")

# ── File paths ─────────────────────────────────────────────────────────────────
_LOG_DIR  = Path(__file__).parent.parent / ".logs"
_LOG_FILE = _LOG_DIR / "trades.jsonl"


# ══════════════════════════════════════════════════════════════════════════════
# TradeLogger — JSON-lines persistent trade journal
# ══════════════════════════════════════════════════════════════════════════════

class TradeLogger:
    """Persists every entry/exit to .logs/trades.jsonl. Survives restarts."""

    def __init__(self):
        _LOG_DIR.mkdir(exist_ok=True)
        self._session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        _log.info("Trade log: %s  (session %s)", _LOG_FILE, self._session_id)

    def _write(self, record: dict):
        record["session_id"] = self._session_id
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def log_entry(
        self,
        engine:   str,
        symbol:   str,
        action:   str,
        price:    float,
        qty:      int,
        mode:     str,
        lot_size: int   = 1,
        num_lots: int   = 1,
        bep:      float = 0.0,
        target:   float = 0.0,
        stop:     float = 0.0,
        rsi:      float = 0.0,
        segment:  str   = "",
        strategy: str   = "",
        reason:   str   = "",
    ):
        record = {
            "ts": datetime.now(timezone.utc).isoformat(), "type": "ENTRY",
            "engine": engine, "symbol": symbol, "segment": segment,
            "strategy": strategy, "action": action,
            "price": round(price, 2), "qty": qty,
            "lot_size": lot_size, "num_lots": num_lots,
            "bep": round(bep, 2), "target": round(target, 2),
            "stop": round(stop, 2), "rsi": round(rsi, 2),
            "notional": round(price * qty, 2), "pnl": None,
            "mode": mode, "reason": reason,
        }
        self._write(record)
        _log.info(
            "[TRADE] %s %s %s | ₹%.2f × %d = ₹%,.0f | BEP ₹%.2f | T ₹%.2f | S ₹%.2f",
            mode, action, symbol, price, qty, price * qty, bep, target, stop,
        )

    def log_exit(
        self,
        engine:      str,
        symbol:      str,
        action:      str,
        price:       float,
        qty:         int,
        mode:        str,
        entry_price: float = 0.0,
        pnl:         float = 0.0,
        reason:      str   = "",
        segment:     str   = "",
        strategy:    str   = "",
    ):
        record = {
            "ts": datetime.now(timezone.utc).isoformat(), "type": "EXIT",
            "engine": engine, "symbol": symbol, "segment": segment,
            "strategy": strategy, "action": action,
            "price": round(price, 2), "qty": qty,
            "entry_price": round(entry_price, 2),
            "notional": round(price * qty, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round((pnl / (entry_price * qty) * 100) if entry_price and qty else 0, 2),
            "mode": mode, "reason": reason,
        }
        self._write(record)
        sign = "+" if pnl >= 0 else ""
        _log.info("[TRADE] %s EXIT %s | ₹%.2f × %d | PnL %s₹%,.2f", mode, symbol, price, qty, sign, pnl)

    def get_trades(self, limit: int = 200) -> list:
        if not _LOG_FILE.exists():
            return []
        try:
            lines = _LOG_FILE.read_text(encoding="utf-8").strip().split("\n")
            return [json.loads(l) for l in lines if l.strip()][-limit:]
        except Exception as exc:
            _log.warning("Trade log read error: %s", exc)
            return []

    def get_session_summary(self) -> dict:
        trades   = self.get_trades(500)
        session  = [t for t in trades if t.get("session_id") == self._session_id]
        entries  = [t for t in session if t["type"] == "ENTRY"]
        exits    = [t for t in session if t["type"] == "EXIT"]
        realized = sum(t.get("pnl", 0) or 0 for t in exits)
        return {
            "session_id":    self._session_id,
            "total_entries": len(entries),
            "total_exits":   len(exits),
            "open_trades":   len(entries) - len(exits),
            "realized_pnl":  round(realized, 2),
            "log_file":      str(_LOG_FILE),
        }


# ══════════════════════════════════════════════════════════════════════════════
# AsyncDBBackend — TimescaleDB sink for signals + trades
# ══════════════════════════════════════════════════════════════════════════════

class AsyncDBBackend:
    """
    Optional async companion to TradeLogger.
    Writes signals and trades to TimescaleDB alongside the JSON-lines file.
    Fails silently if DB is unavailable — never blocks the trading loop.
    """

    def __init__(self):
        import os
        self._enabled = bool(os.getenv("DB_HOST"))
        self._engine  = None
        self._Session = None

    async def connect(self):
        if not self._enabled:
            _log.info("AsyncDBBackend: DB_HOST not set — DB journalling disabled")
            return
        try:
            import os
            from urllib.parse import quote_plus
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker
            url = (
                f"postgresql+psycopg2://"
                f"{quote_plus(os.getenv('DB_USER','trader'))}:"
                f"{quote_plus(os.getenv('DB_PASSWORD','trader123'))}"
                f"@{os.getenv('DB_HOST','localhost')}:{os.getenv('DB_PORT','5432')}"
                f"/{os.getenv('DB_NAME','dhan_trading')}"
            )
            self._engine  = create_engine(url, pool_pre_ping=True, pool_size=3, max_overflow=5)
            self._Session = sessionmaker(bind=self._engine)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._ping)
            _log.info("AsyncDBBackend: connected to TimescaleDB")
        except Exception as exc:
            _log.warning("AsyncDBBackend: connect failed (%s) — DB journalling disabled", exc)
            self._enabled = False

    def _ping(self):
        from sqlalchemy import text
        with self._engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    def _exec(self, sql: str, params: dict):
        from sqlalchemy import text
        session = self._Session()
        try:
            session.execute(text(sql), params)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    async def _run(self, sql: str, params: dict):
        if not self._enabled:
            return
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._exec, sql, params)
        except Exception as exc:
            _log.warning("AsyncDBBackend write error: %s", exc)

    async def log_signal(self, security_id: str, side: str, strategy: str,
                         score: float = 0.0, confidence: float = 0.0,
                         features: Optional[dict] = None) -> None:
        await self._run("""
            INSERT INTO signals (ts, security_id, side, score, confidence, strategy, features_snapshot)
            VALUES (:ts, :security_id, :side, :score, :confidence, :strategy, :features)
        """, {
            "ts": datetime.now(timezone.utc), "security_id": security_id,
            "side": side, "score": score, "confidence": confidence,
            "strategy": strategy, "features": json.dumps(features or {}),
        })

    async def log_trade_entry(self, security_id: str, side: str, qty: int,
                              entry_price: float, strategy: str,
                              signal_id: Optional[int] = None,
                              dhan_order_id: Optional[str] = None) -> None:
        await self._run("""
            INSERT INTO trades (signal_id, security_id, side, qty, entry_ts, entry_price, strategy, dhan_order_id, status)
            VALUES (:signal_id, :security_id, :side, :qty, :entry_ts, :entry_price, :strategy, :order_id, 'OPEN')
        """, {
            "signal_id": signal_id, "security_id": security_id, "side": side,
            "qty": qty, "entry_ts": datetime.now(timezone.utc),
            "entry_price": entry_price, "strategy": strategy, "order_id": dhan_order_id,
        })

    async def log_trade_exit(self, security_id: str, exit_price: float, pnl: float,
                             dhan_order_id: Optional[str] = None) -> None:
        await self._run("""
            UPDATE trades SET exit_ts=:exit_ts, exit_price=:exit_price, pnl=:pnl, status='CLOSED'
            WHERE security_id=:security_id AND status='OPEN'
              AND (:order_id IS NULL OR dhan_order_id=:order_id)
        """, {
            "exit_ts": datetime.now(timezone.utc), "exit_price": exit_price,
            "pnl": pnl, "security_id": security_id, "order_id": dhan_order_id,
        })
        today = datetime.now(timezone.utc).date()
        await self._run("""
            INSERT INTO daily_pnl (date, realized_pnl, trades_count, wins, losses)
            VALUES (:date, :pnl, 1, :win, :loss)
            ON CONFLICT (date) DO UPDATE SET
                realized_pnl = daily_pnl.realized_pnl + EXCLUDED.realized_pnl,
                trades_count = daily_pnl.trades_count + 1,
                wins         = daily_pnl.wins  + EXCLUDED.wins,
                losses       = daily_pnl.losses + EXCLUDED.losses
        """, {"date": today, "pnl": pnl, "win": 1 if pnl >= 0 else 0, "loss": 1 if pnl < 0 else 0})


# ══════════════════════════════════════════════════════════════════════════════
# LogBuffer — rolling in-memory buffer for /api/logs
# ══════════════════════════════════════════════════════════════════════════════

class LogBuffer:
    """Captures Python logging output into a deque for /api/logs."""

    _ICONS = {"INFO": "·", "WARNING": "⚠", "ERROR": "✗", "CRITICAL": "⛔", "DEBUG": "·"}
    _SKIP  = {"aiohttp.access", "dhan.client"}

    def __init__(self, maxlen: int = 100):
        self._buf: deque = deque(maxlen=maxlen)

    def get_logs(self, limit: int = 50) -> list:
        return list(self._buf)[-limit:]

    def _handler(self) -> logging.Handler:
        buf = self._buf
        icons = self._ICONS
        skip = self._SKIP

        class _H(logging.Handler):
            def emit(self, record):
                if record.name in skip:
                    return
                buf.append({
                    "ts":    datetime.now(timezone.utc).isoformat(),
                    "level": record.levelname,
                    "icon":  icons.get(record.levelname, "·"),
                    "name":  record.name.replace("dhan.", ""),
                    "msg":   self.format(record),
                })

        h = _H()
        h.setFormatter(logging.Formatter("%(message)s"))
        h.setLevel(logging.DEBUG)
        return h

    def install(self):
        logging.getLogger().addHandler(self._handler())


# ── Singletons ─────────────────────────────────────────────────────────────────

_trade_logger: Optional[TradeLogger]  = None
_db_backend:   Optional[AsyncDBBackend] = None
_log_buffer:   Optional[LogBuffer]    = None


def get_trade_logger() -> TradeLogger:
    global _trade_logger
    if _trade_logger is None:
        _trade_logger = TradeLogger()
    return _trade_logger


def get_db_backend() -> AsyncDBBackend:
    global _db_backend
    if _db_backend is None:
        _db_backend = AsyncDBBackend()
    return _db_backend


def get_log_buffer() -> LogBuffer:
    global _log_buffer
    if _log_buffer is None:
        _log_buffer = LogBuffer()
    return _log_buffer


def install_log_buffer():
    """Call once at startup to capture all log output."""
    get_log_buffer().install()


# ── Backwards-compat shims (so existing imports don't break) ──────────────────

# core.trade_log compatibility
def get_trade_logger_compat() -> TradeLogger:
    return get_trade_logger()

# core.log_buffer compatibility
def install():
    install_log_buffer()

def get_logs(limit: int = 50) -> list:
    return get_log_buffer().get_logs(limit)
