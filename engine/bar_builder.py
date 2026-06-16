"""
BarBuilder — WebSocket ticks → 1-minute bars → TimescaleDB.

This closes the M2 gap that silently broke the Kronos gate: score_from_db()
read only backfilled history — so during live sessions the model forecast
from bars that ended days earlier. With the BarBuilder running, today's bars
land in the bars hypertable within seconds of the minute closing and Kronos
scores the present.

Volume note: Dhan's WS quote packet carries CUMULATIVE day volume. Per-bar
volume is the delta between consecutive ticks (clamped at 0 for resets).
"""
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger("dhan.engine.bars")
IST = ZoneInfo("Asia/Kolkata")

_BARS_SQL = text("""
    INSERT INTO bars (time, security_id, timeframe, open, high, low, close, volume)
    VALUES (:time, :sid, '1m', :open, :high, :low, :close, :volume)
    ON CONFLICT (security_id, timeframe, time) DO UPDATE SET
        open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
        close = EXCLUDED.close, volume = EXCLUDED.volume
""")

class _Bar:
    __slots__ = ("minute_start", "open", "high", "low", "close", "volume")

    def __init__(self, minute_start: datetime, price: float):
        self.minute_start = minute_start
        self.open = self.high = self.low = self.close = price
        self.volume = 0

    def update(self, price: float, vol_delta: int):
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += vol_delta


class BarBuilder:
    """Aggregates ticks per security into 1m bars and flushes closed bars to DB."""

    def __init__(self, exchange_segment: str = "NSE_EQ", flush_interval: float = 5.0):
        self._segment = exchange_segment
        self._flush_interval = flush_interval
        self._current: Dict[str, _Bar] = {}
        self._last_cum_vol: Dict[str, int] = {}
        self._pending: list[dict] = []         # closed bars awaiting DB write
        self._running = False
        self.bars_written = 0
        self.last_flush_ts: Optional[datetime] = None

    # ── Tick ingestion (called from LiveFeed callback) ────────────────────────

    def on_tick(self, security_id: str, price: float, cum_volume: int = 0,
                ts: Optional[datetime] = None):
        if price <= 0:
            return
        now = ts or datetime.now(IST)
        minute_start = now.replace(second=0, microsecond=0)

        last = self._last_cum_vol.get(security_id, cum_volume)
        vol_delta = max(0, cum_volume - last) if cum_volume else 0
        self._last_cum_vol[security_id] = cum_volume or last

        bar = self._current.get(security_id)
        if bar is None:
            self._current[security_id] = _Bar(minute_start, price)
            self._current[security_id].volume = vol_delta
            return
        if bar.minute_start != minute_start:
            self._close_bar(security_id, bar)
            nb = _Bar(minute_start, price)
            nb.volume = vol_delta
            self._current[security_id] = nb
        else:
            bar.update(price, vol_delta)

    def _close_bar(self, security_id: str, bar: _Bar):
        self._pending.append({
            "sid": security_id, "seg": self._segment,
            "time": bar.minute_start,
            "open": bar.open, "high": bar.high, "low": bar.low,
            "close": bar.close, "volume": bar.volume,
        })

    # ── Flush loop ────────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        logger.info("BarBuilder: started (segment=%s, flush every %.0fs)",
                    self._segment, self._flush_interval)
        while self._running:
            await asyncio.sleep(self._flush_interval)
            await self.flush()

    async def flush(self):
        # Force-close bars whose minute has passed even without a new tick
        # (illiquid securities may not print in the next minute).
        cutoff = datetime.now(IST).replace(second=0, microsecond=0)
        for sid, bar in list(self._current.items()):
            if bar.minute_start < cutoff:
                self._close_bar(sid, bar)
                del self._current[sid]

        if not self._pending:
            return
        batch, self._pending = self._pending, []

        def _write():
            from db import get_session
            with get_session() as s:
                s.execute(_BARS_SQL, batch)

        try:
            await asyncio.get_running_loop().run_in_executor(None, _write)
            self.bars_written += len(batch)
            self.last_flush_ts = datetime.now(IST)
            logger.debug("BarBuilder: flushed %d bars (total %d)", len(batch), self.bars_written)
        except Exception as exc:
            # Re-queue so a transient DB blip (backfill contention) loses nothing
            self._pending = batch + self._pending
            # Cap the buffer so a prolonged DB outage cannot exhaust RAM.
            # Oldest bars are dropped first — newest bars matter most for Kronos
            # and the live strategy; this path only fires during a DB outage.
            _MAX_PENDING = 500
            if len(self._pending) > _MAX_PENDING:
                dropped = len(self._pending) - _MAX_PENDING
                self._pending = self._pending[-_MAX_PENDING:]
                logger.warning(
                    "BarBuilder: pending buffer exceeded %d — dropped %d oldest bars",
                    _MAX_PENDING, dropped,
                )
            logger.warning("BarBuilder: flush failed (%s) — %d bars re-queued", exc, len(self._pending))

    def get_current(self, security_id: str) -> Optional[dict]:
        """Return the in-progress bar's intrabar OHLC, or None if no bar exists.

        Used by LiveFeed.get_ohlc_tick() so the runner reads from the same
        aggregator that feeds Kronos/DB (single source of truth for intrabar H/L).
        """
        bar = self._current.get(security_id)
        if bar is None:
            return None
        return {
            "open":   bar.open,
            "high":   bar.high,
            "low":    bar.low,
            "close":  bar.close,
            "volume": bar.volume,
        }

    def stop(self):
        self._running = False

    def status(self) -> dict:
        return {
            "tracking": len(self._current),
            "pending": len(self._pending),
            "bars_written": self.bars_written,
            "last_flush": self.last_flush_ts.isoformat() if self.last_flush_ts else None,
        }
