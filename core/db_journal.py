"""
TimescaleDB Journal
===================
Async-friendly bridge that writes signals and trades to the TimescaleDB
ohlcv_1min / signals / trades / daily_pnl tables alongside the existing
JSON-lines TradeLogger.

Designed to be used optionally — if DB is not configured the methods no-op.

Usage:
    from core.db_journal import DBJournal
    journal = DBJournal()              # picks up DB_* env vars
    await journal.connect()
    await journal.log_signal(...)
    await journal.log_trade_entry(...)
    await journal.log_trade_exit(...)
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("dhan.db_journal")


def _db_available() -> bool:
    return bool(os.getenv("DB_HOST"))


class DBJournal:
    """
    Thin async wrapper around synchronous SQLAlchemy.
    Runs DB I/O in a thread-pool executor to avoid blocking the event loop.
    """

    def __init__(self):
        self._enabled = _db_available()
        self._engine = None
        self._SessionLocal = None

    async def connect(self):
        if not self._enabled:
            logger.info("DBJournal: DB_HOST not set — journalling disabled")
            return
        try:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker

            db_url = (
                f"postgresql+psycopg2://"
                f"{os.getenv('DB_USER','trader')}:{os.getenv('DB_PASSWORD','trader123')}"
                f"@{os.getenv('DB_HOST','localhost')}:{os.getenv('DB_PORT','5432')}"
                f"/{os.getenv('DB_NAME','dhan_trading')}"
            )
            self._engine = create_engine(db_url, pool_pre_ping=True, pool_size=3, max_overflow=5)
            self._SessionLocal = sessionmaker(bind=self._engine)
            # Quick health check
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._ping)
            logger.info("DBJournal: connected to TimescaleDB")
        except Exception as exc:
            logger.warning("DBJournal: connect failed (%s) — journalling disabled", exc)
            self._enabled = False

    def _ping(self):
        from sqlalchemy import text
        with self._engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    def _exec(self, sql: str, params: dict):
        """Synchronous execute — always called via run_in_executor."""
        from sqlalchemy import text
        session = self._SessionLocal()
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
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._exec, sql, params)
        except Exception as exc:
            logger.warning("DBJournal write error: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def log_signal(
        self,
        security_id: str,
        side: str,
        strategy: str,
        score: float = 0.0,
        confidence: float = 0.0,
        features: Optional[dict] = None,
    ) -> None:
        import json
        await self._run(
            """
            INSERT INTO signals (ts, security_id, side, score, confidence, strategy, features_snapshot)
            VALUES (:ts, :security_id, :side, :score, :confidence, :strategy, :features)
            """,
            {
                "ts": datetime.now(timezone.utc),
                "security_id": security_id,
                "side": side,
                "score": score,
                "confidence": confidence,
                "strategy": strategy,
                "features": json.dumps(features or {}),
            },
        )

    async def log_trade_entry(
        self,
        security_id: str,
        side: str,
        qty: int,
        entry_price: float,
        strategy: str,
        signal_id: Optional[int] = None,
        dhan_order_id: Optional[str] = None,
    ) -> None:
        await self._run(
            """
            INSERT INTO trades
                (signal_id, security_id, side, qty, entry_ts, entry_price, strategy, dhan_order_id, status)
            VALUES
                (:signal_id, :security_id, :side, :qty, :entry_ts, :entry_price, :strategy, :order_id, 'OPEN')
            """,
            {
                "signal_id": signal_id,
                "security_id": security_id,
                "side": side,
                "qty": qty,
                "entry_ts": datetime.now(timezone.utc),
                "entry_price": entry_price,
                "strategy": strategy,
                "order_id": dhan_order_id,
            },
        )

    async def log_trade_exit(
        self,
        security_id: str,
        exit_price: float,
        pnl: float,
        dhan_order_id: Optional[str] = None,
    ) -> None:
        await self._run(
            """
            UPDATE trades
            SET exit_ts = :exit_ts, exit_price = :exit_price, pnl = :pnl, status = 'CLOSED'
            WHERE security_id = :security_id AND status = 'OPEN'
              AND (:order_id IS NULL OR dhan_order_id = :order_id)
            """,
            {
                "exit_ts": datetime.now(timezone.utc),
                "exit_price": exit_price,
                "pnl": pnl,
                "security_id": security_id,
                "order_id": dhan_order_id,
            },
        )
        # Upsert daily_pnl
        today = datetime.now(timezone.utc).date()
        await self._run(
            """
            INSERT INTO daily_pnl (date, realized_pnl, trades_count, wins, losses)
            VALUES (:date, :pnl, 1, :win, :loss)
            ON CONFLICT (date) DO UPDATE SET
                realized_pnl  = daily_pnl.realized_pnl + EXCLUDED.realized_pnl,
                trades_count  = daily_pnl.trades_count + 1,
                wins          = daily_pnl.wins  + EXCLUDED.wins,
                losses        = daily_pnl.losses + EXCLUDED.losses
            """,
            {
                "date": today,
                "pnl": pnl,
                "win": 1 if pnl >= 0 else 0,
                "loss": 1 if pnl < 0 else 0,
            },
        )
