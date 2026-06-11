"""
Portfolio — the single owner of position state, persisted to TimescaleDB.

Every Fill flows through apply_fill(); position state is upserted to the
engine_positions table on every change, so a crash or restart mid-session
recovers exactly where it left off (reconcile_on_boot). The old platform kept
positions in process RAM — a restart orphaned them silently.

Positions are intraday: rows from previous session dates are reported and
cleared on boot, never silently resumed.
"""
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text

from engine.types import Fill, Position

logger = logging.getLogger("dhan.engine.portfolio")
IST = ZoneInfo("Asia/Kolkata")

_UPSERT = text("""
    INSERT INTO engine_positions (security_id, mode, session_date, qty, avg_price, strategy, updated_at)
    VALUES (:sid, :mode, :session_date, :qty, :avg_price, :strategy, NOW())
    ON CONFLICT (security_id, mode) DO UPDATE SET
        session_date = EXCLUDED.session_date,
        qty          = EXCLUDED.qty,
        avg_price    = EXCLUDED.avg_price,
        strategy     = EXCLUDED.strategy,
        updated_at   = NOW()
""")


class Portfolio:
    def __init__(self, mode: str, db_backend=None):
        self.mode = mode                       # "PAPER" | "LIVE"
        self.positions: Dict[str, Position] = {}
        self.realized_pnl: float = 0.0         # this session
        self._db = db_backend                  # AsyncDBBackend for trades journal

    # ── Queries ───────────────────────────────────────────────────────────────

    def get(self, security_id: str) -> Position:
        return self.positions.get(security_id) or Position(security_id=security_id)

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if not p.is_flat]

    def open_count(self) -> int:
        return len(self.open_positions())

    def unrealized_pnl(self, ltp_lookup) -> float:
        """ltp_lookup: callable security_id -> last price (0 if unknown)."""
        return sum(p.unrealized_pnl(float(ltp_lookup(p.security_id) or 0))
                   for p in self.open_positions())

    def summary(self, ltp_lookup=None) -> dict:
        upnl = self.unrealized_pnl(ltp_lookup) if ltp_lookup else 0.0
        return {
            "mode": self.mode,
            "open_positions": [
                {"security_id": p.security_id, "qty": p.qty,
                 "avg_price": p.avg_price, "strategy": p.strategy}
                for p in self.open_positions()
            ],
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(upnl, 2),
            "total_pnl": round(self.realized_pnl + upnl, 2),
        }

    # ── Mutations ─────────────────────────────────────────────────────────────

    async def apply_fill(self, fill: Fill, strategy: str = "") -> float:
        """Apply a fill; returns realized P&L from this fill (0 if opening)."""
        pos = self.positions.setdefault(
            fill.security_id, Position(security_id=fill.security_id, strategy=strategy))
        signed = fill.qty if fill.side == "BUY" else -fill.qty
        realized = 0.0

        if pos.qty == 0 or (pos.qty > 0) == (signed > 0):
            # Opening or adding — new weighted average
            total_cost = pos.avg_price * abs(pos.qty) + fill.price * abs(signed)
            pos.qty += signed
            pos.avg_price = total_cost / abs(pos.qty) if pos.qty else 0.0
            if strategy:
                pos.strategy = strategy
            if self._db:
                await self._db.log_trade_entry(
                    security_id=fill.security_id, side=fill.side,
                    qty=fill.qty, entry_price=fill.price,
                    strategy=strategy or pos.strategy, dhan_order_id=fill.order_id)
        else:
            # Reducing / closing / flipping
            closing = min(abs(signed), abs(pos.qty))
            direction = 1 if pos.qty > 0 else -1
            realized = (fill.price - pos.avg_price) * closing * direction
            self.realized_pnl += realized
            pos.qty += signed
            if pos.qty == 0:
                pos.avg_price = 0.0
            elif (pos.qty > 0) != (direction > 0):
                pos.avg_price = fill.price   # flipped — remainder at fill price
            if self._db:
                await self._db.log_trade_exit(
                    security_id=fill.security_id, exit_price=fill.price,
                    pnl=realized, dhan_order_id=fill.order_id)

        await self._persist(pos)
        logger.info("Portfolio[%s]: %s now qty=%+d avg=%.2f (session realized ₹%+.2f)",
                    self.mode, fill.security_id, pos.qty, pos.avg_price, self.realized_pnl)
        return realized

    # ── Persistence ───────────────────────────────────────────────────────────

    async def _persist(self, pos: Position):
        def _write():
            from db import get_session
            with get_session() as s:
                s.execute(_UPSERT, {
                    "sid": pos.security_id, "mode": self.mode,
                    "session_date": datetime.now(IST).date(),
                    "qty": pos.qty, "avg_price": pos.avg_price,
                    "strategy": pos.strategy,
                })
        try:
            await asyncio.get_event_loop().run_in_executor(None, _write)
        except Exception as exc:
            # Never let journalling failure block trading — but be loud:
            # losing this row means a restart loses the position.
            logger.error("Portfolio persist FAILED for %s: %s", pos.security_id, exc)

    async def reconcile_on_boot(self):
        """Restore today's open positions from DB; report and clear stale ones."""
        today = datetime.now(IST).date()

        def _read():
            from db import get_session
            with get_session() as s:
                return s.execute(text("""
                    SELECT security_id, session_date, qty, avg_price, strategy
                    FROM engine_positions WHERE mode = :mode AND qty != 0
                """), {"mode": self.mode}).fetchall()

        try:
            rows = await asyncio.get_event_loop().run_in_executor(None, _read)
        except Exception as exc:
            logger.error("Portfolio reconcile failed (%s) — starting empty. "
                         "If a position was open, it is now UNTRACKED.", exc)
            return

        for sid, session_date, qty, avg_price, strategy in rows:
            if session_date == today:
                self.positions[sid] = Position(
                    security_id=sid, qty=int(qty),
                    avg_price=float(avg_price or 0), strategy=strategy or "")
                logger.warning("Portfolio RECONCILED open position: %s qty=%+d avg=%.2f (%s)",
                               sid, int(qty), float(avg_price or 0), strategy)
            else:
                # Intraday product — a position from a previous session should
                # have been squared off. Surface it, then zero the row.
                logger.error("Portfolio: STALE position from %s: %s qty=%+d — clearing "
                             "(intraday product; verify with the broker if this was LIVE)",
                             session_date, sid, int(qty))
                stale = Position(security_id=sid, qty=0, avg_price=0.0, strategy=strategy or "")
                await self._persist(stale)

        if not rows:
            logger.info("Portfolio[%s]: no open positions to reconcile", self.mode)
