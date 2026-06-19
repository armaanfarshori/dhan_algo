"""
Opening Gap — pure signal logic (gap-and-go + gap-fade).

Synchronous, IO-free: ticks in, decisions out. Same interface contract as
strategies/orb.py — the same code runs in the backtester and, once the engine
is wired, live.

Two regimes, decided once per session from the first-bar open proxy:

  "go"   — moderate gap [go_min_pct, fade_min_pct): trade WITH the gap after
            a short confirmation window confirms the gap held.
  "fade" — extreme gap [fade_min_pct, ∞): trade TOWARD prior close when price
            reverses off the confirmation-window extreme.
  "none" — gap too small, hold test failed, or prior close missing: inert.

The engine must call seed_prior_day(prior_high, prior_low, prior_close) once
per (security_id, day) BEFORE the first on_tick of that day. If the seed is
missing or non-positive the strategy is gracefully inert all session (returns
None) — the unconditional EOD square-off still fires for any open position.

Open-price approximation: on_tick receives bar close (not open). The strategy
uses the FIRST valid bar's close as the session open proxy, consistent with
ORB's use of the bar stream. This is the only approximation; the 1%/4% regime
thresholds are wide enough that the error (typically a few ticks) is negligible.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from strategies.orb import Decision, IST, MARKET_CLOSE, MARKET_OPEN, MAX_FUTURE_SKEW

logger = logging.getLogger("dhan.strategy.opening_gap")


@dataclass
class OpeningGapParams:
    go_min_pct: float = 0.01          # |gap| floor to classify at all (1%); below → no trade
    fade_min_pct: float = 0.04        # |gap| at/above this → fade regime (4%); between → go
    confirm_min: int = 5              # confirmation-window length, minutes (09:15–09:20)
    go_fill_frac: float = 0.5         # gap-and-go hold test: gap may fill back at most this
                                      #   fraction during the window, else no trade
    sl_buffer_pct: float = 0.002      # stop padding beyond the confirm-window extreme
    target_rr: float = 1.5            # gap-and-go target = entry ± target_rr × entry-risk
    fade_target_frac: float = 0.5     # gap-fade target = this fraction of the way from
                                      #   the open back toward prior close
    squareoff_before_close_min: int = 15   # EOD flatten lead (matches ORB)
    use_prior_range_clamp: bool = False    # optional fade-target clamp using prior_high/low
                                           #   (OFF by default; see spec §2.4)

    def __post_init__(self):
        assert self.go_min_pct < self.fade_min_pct, (
            f"go_min_pct ({self.go_min_pct}) must be < fade_min_pct ({self.fade_min_pct})"
        )


class OpeningGap:
    """
    Opening Gap strategy — one position per session, EOD square-off unconditional.

    Call seed_prior_day(prior_high, prior_low, prior_close) before the first
    on_tick of each session. Degrades gracefully (inert) if not seeded.
    """

    def __init__(self, security_id: str, params: Optional[OpeningGapParams] = None):
        self.security_id = security_id
        self.p = params or OpeningGapParams()

        # Prior-day seed fields — set by seed_prior_day, NOT cleared by _reset_session
        self._prior_close: float = 0.0
        self._prior_high: float = 0.0
        self._prior_low: float = 0.0

        # Per-session trading state — reset each new date
        self._session_date: Optional[date] = None
        self._gap_ref: float = 0.0       # prior_close captured at session start
        self._open_px: float = 0.0       # first bar close used as open proxy
        self._gap: float = 0.0           # (open_px - gap_ref) / gap_ref
        self._gap_dir: int = 0           # +1 up-gap, -1 down-gap
        self._regime: Optional[str] = None  # "none" | "go" | "fade"
        self._cw_high: float = 0.0
        self._cw_low: float = float("inf")
        self._confirm_locked: bool = False
        self._tried: bool = False
        self._stop: float = 0.0
        self._target: float = 0.0

        # Position view — updated only via notify_fill/notify_flat
        self.position: int = 0
        self.entry_price: float = 0.0

    # ── Caller feedback ───────────────────────────────────────────────────────

    def notify_fill(self, side: str, qty: int, price: float):
        self.position += qty if side == "BUY" else -qty
        if self.position != 0:
            self.entry_price = price
        else:
            self.entry_price = 0.0

    def notify_flat(self):
        self.position = 0
        self.entry_price = 0.0
        self._stop = 0.0
        self._target = 0.0

    # ── Prior-day injection ───────────────────────────────────────────────────

    def seed_prior_day(self, prior_high: float, prior_low: float,
                       prior_close: float) -> None:
        """Inject the prior trading day's daily-bar levels for this session.

        Standardised positional signature: seed_prior_day(prior_high, prior_low, prior_close).
        The engine calls it positionally — prior_close is the 3rd positional arg.

        prior_close — REQUIRED. The gap reference and fade target.
            A non-positive value is silently ignored (strategy stays inert).
        prior_high / prior_low — OPTIONAL context (default 0.0 = unknown).
            Used only for the optional fade-target clamp (use_prior_range_clamp).

        Safe to call again on a later session (overwrites the previous seed).
        """
        if prior_close <= 0:
            logger.warning("[OpeningGap %s] seed_prior_day: non-positive prior_close=%s — ignored",
                           self.security_id, prior_close)
            return
        self._prior_close = prior_close
        self._prior_high = prior_high
        self._prior_low = prior_low
        logger.debug("[OpeningGap %s] seeded prior close=%.2f high=%.2f low=%.2f",
                     self.security_id, prior_close, prior_high, prior_low)

    # ── Core logic ────────────────────────────────────────────────────────────

    def on_tick(self, now: datetime, price: float,
                high: Optional[float] = None,
                low: Optional[float] = None,
                volume: Optional[float] = None) -> Optional[Decision]:
        """now must be IST. price = bar close; high/low = current bar extremes."""

        # 1. Reject zero/negative prices
        if price <= 0:
            return None

        # 2. Future-skew guard (copy from ORB verbatim)
        wall = datetime.now(IST)
        ref = wall if now.tzinfo is not None else wall.replace(tzinfo=None)
        if now - ref > MAX_FUTURE_SKEW:
            logger.warning("[OpeningGap %s] ignoring future-stamped tick %s (now %s) — skew > %s",
                           self.security_id, now, ref, MAX_FUTURE_SKEW)
            return None

        today = now.date()
        t = now.time()

        # 3. Session reset on date change
        if self._session_date != today:
            self._reset_session(today)

        # 4. Open capture (first valid bar at/after MARKET_OPEN)
        if self._open_px == 0.0 and t >= MARKET_OPEN:
            self._open_px = price
            # Classify the gap immediately on open capture
            if self._gap_ref > 0:
                self._gap = (self._open_px - self._gap_ref) / self._gap_ref
                self._gap_dir = 1 if self._gap > 0 else -1
                abs_gap = abs(self._gap)
                if abs_gap < self.p.go_min_pct:
                    self._regime = "none"
                    logger.info("[OpeningGap %s] gap %.2f%% < go_min %.2f%% → no trade",
                                self.security_id, self._gap * 100, self.p.go_min_pct * 100)
                elif abs_gap < self.p.fade_min_pct:
                    self._regime = "go"
                    logger.info("[OpeningGap %s] gap %.2f%% → gap-and-go regime",
                                self.security_id, self._gap * 100)
                else:
                    self._regime = "fade"
                    logger.info("[OpeningGap %s] gap %.2f%% ≥ fade_min %.2f%% → fade regime",
                                self.security_id, self._gap * 100, self.p.fade_min_pct * 100)

        # 5. EOD square-off — UNCONDITIONAL (before every gate)
        squareoff = (datetime.combine(today, MARKET_CLOSE)
                     - timedelta(minutes=self.p.squareoff_before_close_min)).time()
        if t >= squareoff:
            if self.position != 0:
                return Decision(action="EXIT", reason="EOD square-off")
            return None

        # 6. Exits for open position (checked before new-entry logic)
        if self.position > 0:
            if price >= self._target:
                return Decision(action="EXIT", reason=f"Target hit ₹{self._target:.2f}")
            if price <= self._stop:
                return Decision(action="EXIT", reason=f"Stop-loss ₹{self._stop:.2f}")
            return None
        if self.position < 0:
            if price <= self._target:
                return Decision(action="EXIT", reason=f"Target hit ₹{self._target:.2f}")
            if price >= self._stop:
                return Decision(action="EXIT", reason=f"Stop-loss ₹{self._stop:.2f}")
            return None

        # 7. Classification gate — inert until seeded (gap_ref) AND open captured
        #    AND the gap classified into a regime.
        if self._gap_ref <= 0 or self._open_px <= 0 or self._regime is None:
            return None
        if self._regime == "none":
            return None

        # 8. Confirmation window — build cw_high/cw_low; lock at confirm_end
        confirm_end = (datetime.combine(today, MARKET_OPEN)
                       + timedelta(minutes=self.p.confirm_min)).time()

        if t < confirm_end and not self._confirm_locked:
            # Still inside the window — accumulate extremes
            bar_high = high if high is not None else price
            bar_low = low if low is not None else price
            self._cw_high = max(self._cw_high, bar_high)
            self._cw_low = min(self._cw_low, bar_low)
            return None

        if not self._confirm_locked:
            # First tick at/after confirm_end — lock and run hold test for "go"
            self._confirm_locked = True
            logger.info("[OpeningGap %s] confirm window locked cw_high=%.2f cw_low=%.2f",
                        self.security_id, self._cw_high, self._cw_low)
            if self._regime == "go":
                open_px = self._open_px
                gap_ref = self._gap_ref
                if self._gap_dir == 1:  # up-gap: cw_low must be >= fill_level
                    fill_level = gap_ref + (1 - self.p.go_fill_frac) * (open_px - gap_ref)
                    hold = self._cw_low >= fill_level
                else:  # down-gap: cw_high must be <= fill_level
                    fill_level = gap_ref + (1 - self.p.go_fill_frac) * (open_px - gap_ref)
                    hold = self._cw_high <= fill_level
                if not hold:
                    self._regime = "none"
                    logger.info("[OpeningGap %s] gap-and-go hold test FAILED → no trade "
                                "(fill_level=%.2f cw_high=%.2f cw_low=%.2f)",
                                self.security_id, fill_level, self._cw_high, self._cw_low)
                    return None

        # Window is locked and regime is active ("go" or "fade")
        if self._regime == "none":
            return None

        # 9. Entry trigger (flat, not yet tried, regime in {"go","fade"}, window locked)
        if self.position == 0 and not self._tried:
            cw_high = self._cw_high
            cw_low = self._cw_low
            open_px = self._open_px
            gap_ref = self._gap_ref
            gap = self._gap

            if self._regime == "go":
                if self._gap_dir == 1 and price > cw_high:
                    # Up-gap continuation breakout: BUY
                    self._tried = True
                    stop = cw_low * (1 - self.p.sl_buffer_pct)
                    risk = price - stop
                    target = price + self.p.target_rr * risk
                    self._stop = stop
                    self._target = target
                    return Decision(
                        action="ENTER", side="BUY",
                        stop=stop, target=target,
                        reason=f"gap-and-go LONG  gap={gap:+.2%} cw={cw_high:.2f}/{cw_low:.2f}",
                    )
                if self._gap_dir == -1 and price < cw_low:
                    # Down-gap continuation breakdown: SELL
                    self._tried = True
                    stop = cw_high * (1 + self.p.sl_buffer_pct)
                    risk = stop - price
                    target = price - self.p.target_rr * risk
                    self._stop = stop
                    self._target = target
                    return Decision(
                        action="ENTER", side="SELL",
                        stop=stop, target=target,
                        reason=f"gap-and-go SHORT gap={gap:+.2%} cw={cw_high:.2f}/{cw_low:.2f}",
                    )

            elif self._regime == "fade":
                if self._gap_dir == 1 and price < cw_low:
                    # Extreme up-gap reversal: fade SHORT toward prior close
                    self._tried = True
                    stop = cw_high * (1 + self.p.sl_buffer_pct)
                    raw_target = gap_ref + (1 - self.p.fade_target_frac) * (open_px - gap_ref)
                    target = raw_target
                    self._stop = stop
                    self._target = target
                    return Decision(
                        action="ENTER", side="SELL",
                        stop=stop, target=target,
                        reason=f"gap-fade SHORT gap={gap:+.2%} →close {gap_ref:.2f}",
                    )
                if self._gap_dir == -1 and price > cw_high:
                    # Extreme down-gap reversal: fade LONG toward prior close
                    self._tried = True
                    stop = cw_low * (1 - self.p.sl_buffer_pct)
                    raw_target = gap_ref + (1 - self.p.fade_target_frac) * (open_px - gap_ref)
                    target = raw_target
                    self._stop = stop
                    self._target = target
                    return Decision(
                        action="ENTER", side="BUY",
                        stop=stop, target=target,
                        reason=f"gap-fade LONG  gap={gap:+.2%} →close {gap_ref:.2f}",
                    )

        # 10. No action
        return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _reset_session(self, today: date):
        """Reset all per-session trading state. Does NOT clear prior-day seed fields."""
        self._session_date = today
        self._open_px = 0.0
        self._gap = 0.0
        self._gap_dir = 0
        self._regime = None
        self._cw_high = 0.0
        self._cw_low = float("inf")
        self._confirm_locked = False
        self._tried = False
        self._stop = 0.0
        self._target = 0.0
        # Capture the pending prior-day seed into the session reference
        self._gap_ref = self._prior_close
        logger.info("[OpeningGap %s] New session %s — reset (gap_ref=%.4f)",
                    self.security_id, today, self._gap_ref)
