"""
CPR / Pivot Points — Central Pivot Range breakout strategy.

Pure, synchronous, IO-free class mirroring strategies/orb.py and obeying every
rule in _CONTRACT.md. One position at a time, EOD square-off unconditional, no
lookahead, no cross-session leakage.

CPR and classic floor-trader pivots are derived ONCE per session from the PRIOR
trading day's High / Low / Close — they are static all day, computed before the
bell, never recomputed from intraday bars.

Entry: price close breaks above cpr_top (long) or below cpr_bottom (short).
Stop: broken CPR edge padded by sl_buffer_pct.
Target: next pivot level (R1/S1 by default, R2/S2 optional).
EOD: unconditional square-off at 15:30 - squareoff_before_close_min.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Optional
from strategies.orb import Decision, IST, MARKET_CLOSE, MAX_FUTURE_SKEW

logger = logging.getLogger("dhan.strategy.cpr_pivot")

MARKET_OPEN = dtime(9, 15)


@dataclass
class CprPivotParams:
    sl_buffer_pct: float = 0.002           # stop padding beyond the broken CPR edge
    squareoff_before_close_min: int = 15   # EOD flatten lead (matches ORB)
    max_cpr_width_pct: float = 0.0         # 0 = disabled; >0 = only trade narrow-CPR days
    min_breakout_pct: float = 0.0          # 0 = disabled; require close to clear edge by this
    target_level: str = "R1S1"            # "R1S1" → long R1 / short S1 | "R2S2" → R2/S2


class CprPivot:
    def __init__(self, security_id: str, params: Optional[CprPivotParams] = None):
        self.security_id = security_id
        self.p = params or CprPivotParams()

        self._session_date: Optional[date] = None

        # CPR / pivot levels (set by seed_prior_day, cleared on session reset)
        self._seeded: bool = False
        self._P: float = 0.0
        self._cpr_top: float = 0.0
        self._cpr_bottom: float = 0.0
        self._cpr_width: float = 0.0
        self._R1: float = 0.0
        self._R2: float = 0.0
        self._R3: float = 0.0
        self._S1: float = 0.0
        self._S2: float = 0.0
        self._S3: float = 0.0

        # Per-session trade state
        self._long_tried: bool = False
        self._short_tried: bool = False
        self._pending_target: float = 0.0   # set when ENTER emitted; promoted on notify_fill
        self._entry_target: float = 0.0     # active target for the open position

        # Position state — updated only via notify_fill / notify_flat
        self.position: int = 0
        self.entry_price: float = 0.0

    # ── Prior-day injection interface ─────────────────────────────────────────

    def seed_prior_day(self, prior_high: float, prior_low: float, prior_close: float) -> None:
        """
        Inject the PRIOR trading day's H/L/C so today's CPR + classic pivots can be
        computed. Called ONCE by the engine BEFORE the session's first on_tick.

        Robustness:
          • If any input is <= 0, or prior_high < prior_low, treat as missing data:
            leave self._seeded = False (strategy will trade nothing today). Do NOT raise.
          • Idempotent within a session: a second call with the same/new values just
            recomputes the levels.
        """
        if prior_high <= 0 or prior_low <= 0 or prior_close <= 0:
            logger.warning("[CprPivot %s] seed_prior_day: invalid inputs (<=0) — not seeded",
                           self.security_id)
            self._seeded = False
            return
        if prior_high < prior_low:
            logger.warning("[CprPivot %s] seed_prior_day: prior_high < prior_low — not seeded",
                           self.security_id)
            self._seeded = False
            return

        PH, PL, PC = prior_high, prior_low, prior_close

        # CPR math
        P = (PH + PL + PC) / 3.0
        BC = (PH + PL) / 2.0
        TC = 2.0 * P - BC

        cpr_top = max(TC, BC)
        cpr_bottom = min(TC, BC)
        cpr_width = cpr_top - cpr_bottom

        # Classic floor-trader pivots
        R1 = 2.0 * P - PL
        S1 = 2.0 * P - PH
        R2 = P + (PH - PL)
        S2 = P - (PH - PL)
        R3 = PH + 2.0 * (P - PL)
        S3 = PL - 2.0 * (PH - PL)

        self._P = P
        self._cpr_top = cpr_top
        self._cpr_bottom = cpr_bottom
        self._cpr_width = cpr_width
        self._R1 = R1
        self._R2 = R2
        self._R3 = R3
        self._S1 = S1
        self._S2 = S2
        self._S3 = S3
        self._seeded = True

        logger.info(
            "[CprPivot %s] seeded P=%.4f cpr=[%.4f, %.4f] width=%.4f "
            "R1=%.4f R2=%.4f R3=%.4f S1=%.4f S2=%.4f S3=%.4f",
            self.security_id, P, cpr_bottom, cpr_top, cpr_width,
            R1, R2, R3, S1, S2, S3,
        )

    # ── Caller feedback ───────────────────────────────────────────────────────

    def notify_fill(self, side: str, qty: int, price: float):
        self.position += qty if side == "BUY" else -qty
        if self.position != 0:
            self.entry_price = price
            self._entry_target = self._pending_target
        else:
            self.entry_price = 0.0

    def notify_flat(self):
        self.position = 0
        self.entry_price = 0.0
        self._entry_target = 0.0

    # ── Core logic ────────────────────────────────────────────────────────────

    def on_tick(
        self,
        now: datetime,
        price: float,
        high: Optional[float] = None,
        low: Optional[float] = None,
        volume: Optional[float] = None,
    ) -> Optional[Decision]:
        """now must be IST. price = bar close; high/low = current bar extremes."""

        # 1. Reject non-positive prices
        if price <= 0:
            return None

        # 2. Future-skew guard — copy ORB lines 88–96 verbatim
        wall = datetime.now(IST)
        ref = wall if now.tzinfo is not None else wall.replace(tzinfo=None)
        if now - ref > MAX_FUTURE_SKEW:
            logger.warning(
                "[CprPivot %s] ignoring future-stamped tick %s (now %s) — skew > %s",
                self.security_id, now, ref, MAX_FUTURE_SKEW,
            )
            return None

        today = now.date()
        t = now.time()

        # 3. Session reset if date changed.
        # When _session_date is None (first tick ever), just stamp the date —
        # do NOT clear pivot levels that seed_prior_day already computed.
        if self._session_date != today:
            if self._session_date is None:
                self._session_date = today
            else:
                self._reset_session(today)

        # 4. EOD square-off (UNCONDITIONAL — above the seeded gate)
        squareoff = (
            datetime.combine(today, MARKET_CLOSE)
            - timedelta(minutes=self.p.squareoff_before_close_min)
        ).time()
        if t >= squareoff:
            if self.position != 0:
                return Decision(action="EXIT", reason="EOD square-off")
            return None

        # 5. Seeded gate — no prior-day data → no trade
        if not self._seeded:
            return None

        # 6. Optional CPR-width filter
        if self.p.max_cpr_width_pct > 0:
            cpr_width_pct = self._cpr_width / self._P if self._P > 0 else 0.0
            if cpr_width_pct > self.p.max_cpr_width_pct:
                return None

        # 7. Exits for an open position
        if self.position > 0:
            target_lvl = self._entry_target
            stop_lvl = self._cpr_bottom * (1.0 - self.p.sl_buffer_pct)
            if price >= target_lvl:
                return Decision(action="EXIT", reason=f"Target hit ₹{target_lvl:.2f}")
            if price <= stop_lvl:
                return Decision(action="EXIT", reason=f"Stop-loss ₹{stop_lvl:.2f}")
            return None

        if self.position < 0:
            target_lvl = self._entry_target
            stop_lvl = self._cpr_top * (1.0 + self.p.sl_buffer_pct)
            if price <= target_lvl:
                return Decision(action="EXIT", reason=f"Target hit ₹{target_lvl:.2f}")
            if price >= stop_lvl:
                return Decision(action="EXIT", reason=f"Stop-loss ₹{stop_lvl:.2f}")
            return None

        # 8. Entries when flat
        # Long breakout above CPR top
        if not self._long_tried:
            crosses_top = price > self._cpr_top
            if crosses_top and self.p.min_breakout_pct > 0:
                crosses_top = (price - self._cpr_top) / self._cpr_top >= self.p.min_breakout_pct
            if crosses_top:
                self._long_tried = True
                target = self._pick_long_target(price)
                if abs(target - price) <= 0:
                    # Zero-distance target — degenerate, skip
                    return None
                stop = self._cpr_bottom * (1.0 - self.p.sl_buffer_pct)
                self._pending_target = target
                return Decision(
                    action="ENTER",
                    side="BUY",
                    stop=stop,
                    target=target,
                    reason=(
                        f"CPR long breakout > TC={self._cpr_top:.2f} → "
                        f"{'R2' if self.p.target_level == 'R2S2' else 'R1'}={target:.2f}"
                    ),
                )

        # Short breakdown below CPR bottom
        if not self._short_tried:
            crosses_bottom = price < self._cpr_bottom
            if crosses_bottom and self.p.min_breakout_pct > 0:
                crosses_bottom = (
                    (self._cpr_bottom - price) / self._cpr_bottom >= self.p.min_breakout_pct
                )
            if crosses_bottom:
                self._short_tried = True
                target = self._pick_short_target(price)
                if abs(target - price) <= 0:
                    # Zero-distance target — degenerate, skip
                    return None
                stop = self._cpr_top * (1.0 + self.p.sl_buffer_pct)
                self._pending_target = target
                return Decision(
                    action="ENTER",
                    side="SELL",
                    stop=stop,
                    target=target,
                    reason=(
                        f"CPR short breakdown < BC={self._cpr_bottom:.2f} → "
                        f"{'S2' if self.p.target_level == 'R2S2' else 'S1'}={target:.2f}"
                    ),
                )

        return None

    # ── Target selection ──────────────────────────────────────────────────────

    def _pick_long_target(self, price: float) -> float:
        """
        Choose the long take-profit level. Base: R1 (or R2 if target_level="R2S2").
        Fallback: next pivot above price among (R1, R2, R3); if none, price + cpr_width.
        """
        base = self._R2 if self.p.target_level == "R2S2" else self._R1
        if base > price:
            return base
        # Fallback: first of R1, R2, R3 strictly above price
        for lvl in (self._R1, self._R2, self._R3):
            if lvl > price:
                return lvl
        # Nothing above — use cpr_width extension
        return price + self._cpr_width

    def _pick_short_target(self, price: float) -> float:
        """
        Choose the short take-profit level. Base: S1 (or S2 if target_level="R2S2").
        Fallback: next pivot below price among (S1, S2, S3); if none, price - cpr_width.
        """
        base = self._S2 if self.p.target_level == "R2S2" else self._S1
        if base < price:
            return base
        # Fallback: first of S1, S2, S3 strictly below price
        for lvl in (self._S1, self._S2, self._S3):
            if lvl < price:
                return lvl
        # Nothing below — use cpr_width extension
        return price - self._cpr_width

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _reset_session(self, today: date) -> None:
        """Reset all intraday state. Does NOT touch position/entry_price."""
        self._session_date = today
        self._seeded = False
        self._P = 0.0
        self._cpr_top = 0.0
        self._cpr_bottom = 0.0
        self._cpr_width = 0.0
        self._R1 = 0.0
        self._R2 = 0.0
        self._R3 = 0.0
        self._S1 = 0.0
        self._S2 = 0.0
        self._S3 = 0.0
        self._long_tried = False
        self._short_tried = False
        self._pending_target = 0.0
        self._entry_target = 0.0
        logger.info("[CprPivot %s] New session %s — reset", self.security_id, today)
