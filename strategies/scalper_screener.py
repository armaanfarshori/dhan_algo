"""
Scalper contract SCREENER — pick ONE tradeable option contract from a live chain.

This is the *contract-selection* facet of the intraday options scalper. The scalper
proper (``strategies/options_scalper.py``) decides DIRECTION (LONG/SHORT) on the
underlying; this module turns that direction into a concrete, *liquid* CE/PE contract
to actually trade — or returns ``None`` (with a logged reason) if nothing on the chain
clears the hard gates. **It never fabricates a contract.**

Design contract
---------------
- **Pure / IO-light.** ``select`` takes already-fetched chain rows and returns a
  dataclass (or ``None``). No DB, no network, no clock surprises — ``now`` is injected.
- **Index-agnostic.** Per-index knobs (strike step, lot size, spread caps, premium
  band) live in :class:`ScreenerParams`; the algorithm is identical for NIFTY,
  BANKNIFTY, and any future index — add a row to the per-index maps, nothing else.
- **Schema VERBATIM.** ``chain_rows`` are the dicts emitted by
  ``core.fno_backfill.parse_option_chain`` (the ``option_chain_snapshot`` row shape):
  ``strike / option_type / security_id / ltp / oi / volume / top_bid_price /
  top_ask_price / top_bid_qty / top_ask_qty / iv / delta / ...``. The LIVE path and
  the backtest projection path feed the **same** schema.

Backtest vs forward-paper
-------------------------
The rolling-option backtest projection emits rows with ``top_bid_price`` /
``top_ask_price`` / ``top_bid_qty`` / ``top_ask_qty`` == ``None`` (the historical
``option_chain_snapshot`` did not always capture L1 depth). When bid/ask are absent the
**spread gate and the depth gate cannot be evaluated** — they are *skipped* (treated as
"unknown, not a hard fail") so the screener still runs in backtest. Those two gates are
therefore **forward-paper-only**; in backtest, selection is driven by strike-proximity,
liquidity (OI/volume), and the premium band alone. The ranking simply omits the
spread/depth sub-scores it cannot compute and renormalises.

iv_rank
-------
``iv_rank`` is an optional band gate. If no IV history is supplied (or too few points),
the gate **fails OPEN** — the contract is not rejected for lack of history. This mirrors
``research.backtest.fno_orchestrator.iv_rank`` semantics (None ⇒ unknown).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

logger = logging.getLogger("dhan.strategy.scalper_screener")


# ---------------------------------------------------------------------------
# Per-index defaults (index-agnostic: extend the maps, never the algorithm)
# ---------------------------------------------------------------------------
# Strike grid spacing (₹) per index.
DEFAULT_STRIKE_STEP: dict[str, int] = {"NIFTY": 50, "BANKNIFTY": 100}
# Lot size (contract multiplier) per index — used ONLY for the depth gate
# (min top-qty in lots → contracts). Kept in sync with the canonical
# ``research.backtest.fno_costs.NIFTY_LOT`` (65). NSE revises F&O lot sizes
# periodically (BANKNIFTY especially) — these are conservative defaults and
# should be reconciled to the live exchange circular before any non-NIFTY use.
DEFAULT_LOT_SIZE: dict[str, int] = {"NIFTY": 65, "BANKNIFTY": 35}
# Premium band MAX (₹/unit) per index — BANKNIFTY options trade richer than NIFTY.
DEFAULT_MAX_PREMIUM: dict[str, float] = {"NIFTY": 400.0, "BANKNIFTY": 1200.0}
# Absolute spread cap (₹) per index — a hard L1 width ceiling alongside the % cap.
DEFAULT_MAX_SPREAD_ABS: dict[str, float] = {"NIFTY": 3.0, "BANKNIFTY": 8.0}

# Ranking weights (must sum to 1.0). When a sub-score is uncomputable (backtest:
# no bid/ask), its weight is dropped and the rest renormalised.
W_PROXIMITY = 0.45
W_SPREAD = 0.25
W_DEPTH = 0.20
W_LIQUIDITY = 0.10


@dataclass
class ScreenerParams:
    """Per-index screener configuration. All gates are HARD unless noted."""

    index: str = "NIFTY"

    # ── strike grid / lot (per-index; fall back to NIFTY if index unknown) ──
    step: Optional[int] = None          # ₹ strike spacing; None → DEFAULT_STRIKE_STEP
    lot: Optional[int] = None           # contract multiplier; None → DEFAULT_LOT_SIZE

    # ── expiry selection (front weekly) ────────────────────────────────────
    dte_min: int = 0                    # accept 0-DTE (expiry day) and up
    dte_max: int = 4                    # reject anything further than the front weekly
    dte_prefer_lo: int = 1             # preferred DTE window (proximity tie-break / note)
    dte_prefer_hi: int = 2

    # ── strike selection mode ──────────────────────────────────────────────
    strike_mode: str = "atm"            # "atm" | "offset" | "delta"
    offset_steps: int = 0               # for strike_mode="offset": steps OTM (+) / ITM (-)
    target_delta: float = 0.50          # for strike_mode="delta": target |delta|
    n_candidates: int = 5               # how many nearest strikes (by mode) to consider

    # ── HARD liquidity gates ───────────────────────────────────────────────
    min_oi: int = 100_000               # minimum open interest (contracts)
    min_volume: int = 10_000            # minimum traded volume today (contracts)
    max_spread_pct: float = 0.02        # bid/ask width as fraction of mid (≤ 2%)
    max_spread_abs: Optional[float] = None   # ₹ width ceiling; None → DEFAULT_MAX_SPREAD_ABS
    min_top_qty_lots: float = 5.0       # L1 depth: min(top_bid_qty, top_ask_qty) ≥ this many lots

    # ── premium band (HARD) ────────────────────────────────────────────────
    min_premium: float = 5.0            # reject sub-₹5 lottery tickets
    max_premium: Optional[float] = None  # None → DEFAULT_MAX_PREMIUM

    # ── iv_rank band (fail-OPEN when history is absent) ─────────────────────
    iv_rank_min: Optional[float] = None  # e.g. 0.10 ; None → no lower bound
    iv_rank_max: Optional[float] = None  # e.g. 0.90 ; None → no upper bound

    def __post_init__(self) -> None:
        if self.strike_mode not in ("atm", "offset", "delta"):
            raise ValueError(
                f"strike_mode must be 'atm'|'offset'|'delta', got {self.strike_mode!r}"
            )
        if self.n_candidates < 1:
            raise ValueError(f"n_candidates must be >= 1, got {self.n_candidates}")
        if self.step is None:
            self.step = DEFAULT_STRIKE_STEP.get(self.index, DEFAULT_STRIKE_STEP["NIFTY"])
        if self.lot is None:
            self.lot = DEFAULT_LOT_SIZE.get(self.index, DEFAULT_LOT_SIZE["NIFTY"])
        if self.max_premium is None:
            self.max_premium = DEFAULT_MAX_PREMIUM.get(
                self.index, DEFAULT_MAX_PREMIUM["NIFTY"]
            )
        if self.max_spread_abs is None:
            self.max_spread_abs = DEFAULT_MAX_SPREAD_ABS.get(
                self.index, DEFAULT_MAX_SPREAD_ABS["NIFTY"]
            )

        # ── invariants (fail fast on misconfiguration, never silently) ──────
        if self.step is None or self.step <= 0:
            raise ValueError(f"step must be > 0, got {self.step!r}")
        if self.lot is None or self.lot <= 0:
            raise ValueError(f"lot must be > 0, got {self.lot!r}")
        if self.dte_min > self.dte_max:
            raise ValueError(
                f"dte_min ({self.dte_min}) must be <= dte_max ({self.dte_max})"
            )
        if self.dte_prefer_lo > self.dte_prefer_hi:
            raise ValueError(
                f"dte_prefer_lo ({self.dte_prefer_lo}) must be <= "
                f"dte_prefer_hi ({self.dte_prefer_hi})"
            )
        if self.min_premium < 0 or self.min_premium >= float(self.max_premium):
            raise ValueError(
                f"require 0 <= min_premium ({self.min_premium}) < "
                f"max_premium ({self.max_premium})"
            )
        if not (0.0 < self.max_spread_pct <= 1.0):
            raise ValueError(
                f"max_spread_pct must be in (0, 1], got {self.max_spread_pct}"
            )
        if float(self.max_spread_abs) <= 0:
            raise ValueError(f"max_spread_abs must be > 0, got {self.max_spread_abs}")
        if self.min_oi < 0 or self.min_volume < 0:
            raise ValueError("min_oi and min_volume must be >= 0")
        if self.min_top_qty_lots < 0:
            raise ValueError(
                f"min_top_qty_lots must be >= 0, got {self.min_top_qty_lots}"
            )
        if self.strike_mode == "delta" and not (0.0 < self.target_delta < 1.0):
            raise ValueError(
                f"target_delta must be in (0, 1) for delta mode, got {self.target_delta}"
            )
        for name, v in (("iv_rank_min", self.iv_rank_min), ("iv_rank_max", self.iv_rank_max)):
            if v is not None and not (0.0 <= v <= 1.0):
                raise ValueError(f"{name} must be in [0, 1], got {v}")
        if (
            self.iv_rank_min is not None
            and self.iv_rank_max is not None
            and self.iv_rank_min > self.iv_rank_max
        ):
            raise ValueError(
                f"iv_rank_min ({self.iv_rank_min}) must be <= "
                f"iv_rank_max ({self.iv_rank_max})"
            )


@dataclass
class ScreenerInputs:
    """One screening request — everything needed to pick a contract, no IO."""

    index: str                          # "NIFTY" | "BANKNIFTY"
    direction: str                      # "LONG" → CE ; "SHORT" → PE
    spot: float
    chain_rows: list[dict[str, Any]]    # parse_option_chain dicts (verbatim schema)
    expiry_date: date
    dte: int
    now: datetime
    iv_hist: Optional[list[float]] = None  # trailing IV closes (PIT) for iv_rank; None → fail-open


@dataclass
class ChosenContract:
    """The single screened contract, or the screener returns ``None`` instead."""

    index: str
    security_id: Any
    option_type: str                    # "CE" | "PE"
    strike: float
    expiry_date: date
    dte: int
    expiry_restricted: bool             # True when dte outside the preferred 1–2 window
    ref_ltp: float
    est_spread_pct: Optional[float]     # None when bid/ask unavailable (backtest)
    score: float
    reason: str
    # diagnostics (non-load-bearing)
    extras: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers (pure)
# ---------------------------------------------------------------------------
def _atm_strike(spot: float, step: int) -> int:
    """Nearest ``step`` multiple to spot (half-step rounds UP, mirrors fno_backfill)."""
    return int(math.floor(spot / step + 0.5)) * step


def _otm_sign(side: str) -> int:
    """+1 for CE (OTM = higher strike), -1 for PE (OTM = lower strike)."""
    return 1 if side == "CE" else -1


def _side_for_direction(direction: str) -> str:
    d = direction.strip().upper()
    if d == "LONG":
        return "CE"
    if d == "SHORT":
        return "PE"
    raise ValueError(f"direction must be 'LONG'|'SHORT', got {direction!r}")


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The screener
# ---------------------------------------------------------------------------
class ScalperScreener:
    """Stateless contract screener. Construct with per-index :class:`ScreenerParams`."""

    def __init__(self, params: Optional[ScreenerParams] = None) -> None:
        self.params = params or ScreenerParams()

    # -- public API --------------------------------------------------------
    def select(self, inputs: ScreenerInputs) -> Optional[ChosenContract]:
        """Pick ONE liquid contract for ``inputs.direction``, or ``None`` (logged).

        Steps: (1) DTE gate, (2) restrict to the right side (CE/PE), (3) pick the
        ``n_candidates`` strikes by ``strike_mode``, (4) apply HARD gates
        (OI / volume / spread / depth / premium / iv_rank) to each, (5) rank the
        survivors and return the best. Any all-fail path returns ``None`` with a reason.
        """
        p = self.params
        side = _side_for_direction(inputs.direction)

        # (1) DTE gate — front weekly only.
        if inputs.dte < p.dte_min or inputs.dte > p.dte_max:
            return self._none(
                f"dte {inputs.dte} outside [{p.dte_min},{p.dte_max}] (front-weekly only)"
            )
        expiry_restricted = not (p.dte_prefer_lo <= inputs.dte <= p.dte_prefer_hi)

        # iv_rank (fail-open if no/short history) — band gate, computed once.
        ivr = self._iv_rank(inputs)
        ivr_ok, ivr_reason = self._iv_rank_band_ok(ivr)
        if not ivr_ok:
            return self._none(ivr_reason)

        # (2) side filter.
        side_rows = [
            r for r in inputs.chain_rows
            if str(r.get("option_type", "")).upper() == side
            and _f(r.get("strike")) is not None
        ]
        if not side_rows:
            return self._none(f"no {side} rows in chain")

        # (3) candidate strikes by mode.
        candidates = self._pick_candidates(side_rows, inputs, side)
        if not candidates:
            return self._none(f"no candidate {side} strikes for mode={p.strike_mode}")

        atm = _atm_strike(inputs.spot, int(p.step))
        # Proximity is scored against the mode's strike target (ATM for atm-mode,
        # the offset strike for offset-mode); delta-mode has no strike target → ATM.
        prox_target = self._proximity_target(inputs, side)
        if prox_target is None:
            prox_target = atm

        # (4) hard gates + (5) scoring.
        survivors: list[tuple[float, ChosenContract]] = []
        reject_reasons: list[str] = []
        for row in candidates:
            ok, reason, scored = self._evaluate(
                row, inputs, side, atm, prox_target, expiry_restricted, ivr
            )
            if ok and scored is not None:
                survivors.append((scored.score, scored))
            else:
                reject_reasons.append(reason)

        if not survivors:
            sample = "; ".join(reject_reasons[: p.n_candidates])
            return self._none(f"all {len(candidates)} candidates failed gates [{sample}]")

        # (5) rank: highest score; tie → nearest ATM; final tie → lower strike
        # (fully deterministic regardless of caller-supplied chain_rows order).
        survivors.sort(
            key=lambda sc: (-sc[0], abs(sc[1].strike - atm), sc[1].strike)
        )
        best = survivors[0][1]
        logger.info(
            "screener PICK %s %s %s strike=%g dte=%d score=%.3f spread=%s reason=%s",
            inputs.index, side, best.option_type, best.strike, inputs.dte,
            best.score, best.est_spread_pct, best.reason,
        )
        return best

    # -- internals ---------------------------------------------------------
    def _none(self, reason: str) -> None:
        logger.info("screener NONE: %s", reason)
        return None

    def _iv_rank(self, inputs: ScreenerInputs) -> Optional[float]:
        """Percentile of today's ATM IV in the trailing history. None ⇒ fail-open."""
        hist = inputs.iv_hist
        if not hist:
            return None
        vals = [v for v in (_f(x) for x in hist) if v is not None and v > 0]
        if len(vals) < 2:
            return None
        # today's IV: median IV across the chain rows (robust; raw percent units).
        ivs = [v for v in (_f(r.get("iv")) for r in inputs.chain_rows) if v is not None and v > 0]
        if not ivs:
            return None
        ivs.sort()
        iv_today = ivs[len(ivs) // 2]
        below = sum(1 for v in vals if v < iv_today)
        return below / len(vals)

    def _iv_rank_band_ok(self, ivr: Optional[float]) -> tuple[bool, str]:
        p = self.params
        if ivr is None:
            return True, ""  # fail-open: no history → do not reject
        if p.iv_rank_min is not None and ivr < p.iv_rank_min:
            return False, f"iv_rank {ivr:.2f} < min {p.iv_rank_min:.2f}"
        if p.iv_rank_max is not None and ivr > p.iv_rank_max:
            return False, f"iv_rank {ivr:.2f} > max {p.iv_rank_max:.2f}"
        return True, ""

    def _proximity_target(self, inputs: ScreenerInputs, side: str) -> Optional[int]:
        """Reference strike for proximity scoring/candidate selection (mode-aware).

        ``atm`` and ``offset`` have a concrete strike target; ``delta`` does not
        (it targets a |delta|, not a strike) → returns ``None`` so proximity falls
        back to ATM-distance only for tie-breaks. For ``offset``, ``offset_steps``
        is SIDE-AWARE: positive means OTM (higher strike for CE, lower for PE).
        """
        p = self.params
        step = int(p.step)
        atm = _atm_strike(inputs.spot, step)
        if p.strike_mode == "offset":
            return atm + _otm_sign(side) * p.offset_steps * step
        if p.strike_mode == "atm":
            return atm
        return None  # delta mode

    def _pick_candidates(
        self, side_rows: list[dict[str, Any]], inputs: ScreenerInputs, side: str
    ) -> list[dict[str, Any]]:
        """Choose up to ``n_candidates`` rows by strike_mode, nearest-first."""
        p = self.params
        step = int(p.step)
        atm = _atm_strike(inputs.spot, step)

        if p.strike_mode == "delta":
            # target |delta|; rows without delta are dropped (delta mode needs it).
            scored = []
            for r in side_rows:
                d = _f(r.get("delta"))
                if d is None:
                    continue
                k = _f(r.get("strike"))
                if k is None:
                    continue
                scored.append((abs(abs(d) - p.target_delta), abs(k - atm), r))
            scored.sort(key=lambda t: (t[0], t[1]))
            return [r for _, _, r in scored[: p.n_candidates]]

        # atm / offset: offset_steps is SIDE-AWARE (positive = OTM).
        target = self._proximity_target(inputs, side)
        if target is None:  # defensive — only delta returns None
            target = atm

        side_rows_sorted = sorted(
            side_rows,
            key=lambda r: (abs((_f(r.get("strike")) or 0.0) - target),
                           _f(r.get("strike")) or 0.0),
        )
        return side_rows_sorted[: p.n_candidates]

    def _evaluate(
        self,
        row: dict[str, Any],
        inputs: ScreenerInputs,
        side: str,
        atm: int,
        prox_target: int,
        expiry_restricted: bool,
        ivr: Optional[float],
    ) -> tuple[bool, str, Optional[ChosenContract]]:
        """Apply HARD gates to one row; if it passes, build a scored ChosenContract."""
        p = self.params
        strike = _f(row.get("strike"))
        ltp = _f(row.get("ltp"))
        oi = _i(row.get("oi"))
        vol = _i(row.get("volume"))
        bid = _f(row.get("top_bid_price"))
        ask = _f(row.get("top_ask_price"))
        bid_qty = _i(row.get("top_bid_qty"))
        ask_qty = _i(row.get("top_ask_qty"))
        sec_id = row.get("security_id")

        tag = f"k={strike:g}" if strike is not None else "k=?"

        # never return a contract we cannot route an order against.
        if sec_id is None:
            return False, f"{tag} missing security_id", None

        # premium gate (HARD) — needs an LTP.
        if ltp is None:
            return False, f"{tag} no ltp", None
        if ltp < p.min_premium:
            return False, f"{tag} premium {ltp:g} < min {p.min_premium:g}", None
        if ltp > float(p.max_premium):
            return False, f"{tag} premium {ltp:g} > max {p.max_premium:g}", None

        # liquidity gates (HARD).
        if oi is None or oi < p.min_oi:
            return False, f"{tag} oi {oi} < min {p.min_oi}", None
        if vol is None or vol < p.min_volume:
            return False, f"{tag} volume {vol} < min {p.min_volume}", None

        # spread + depth gates — FORWARD-PAPER-ONLY. Skipped when BOTH bid/ask are
        # absent (backtest rows carry None for both). When present they are HARD.
        # A ONE-SIDED book (exactly one of bid/ask present) is a live data anomaly,
        # not a backtest row — we cannot trust the spread, so the gates are skipped
        # and the anomaly is logged (we do NOT silently pass it as if clean).
        est_spread_pct: Optional[float] = None
        spread_score: Optional[float] = None
        depth_score: Optional[float] = None
        have_quotes = bid is not None and ask is not None and bid > 0 and ask > 0
        if not have_quotes and (bid is not None) != (ask is not None):
            logger.warning(
                "screener %s one-sided book (bid=%s ask=%s) — spread/depth gates skipped",
                tag, bid, ask,
            )
        if have_quotes:
            if ask < bid:
                return False, f"{tag} crossed book (bid {bid:g} > ask {ask:g})", None
            mid = (bid + ask) / 2.0
            width = ask - bid
            est_spread_pct = (width / mid) if mid > 0 else None
            if est_spread_pct is None or est_spread_pct > p.max_spread_pct:
                return False, (
                    f"{tag} spread {est_spread_pct} > {p.max_spread_pct:.3f}"
                ), None
            if width > float(p.max_spread_abs):
                return False, f"{tag} spread_abs {width:g} > {p.max_spread_abs:g}", None
            spread_score = max(0.0, 1.0 - est_spread_pct / p.max_spread_pct)

            # depth gate: both sides must show >= min_top_qty_lots of L1 size.
            lot = int(p.lot)
            min_units = p.min_top_qty_lots * lot
            if bid_qty is None or ask_qty is None:
                return False, f"{tag} missing L1 depth", None
            top_units = min(bid_qty, ask_qty)
            if top_units < min_units:
                return False, (
                    f"{tag} depth {top_units} < {min_units:g} units "
                    f"({p.min_top_qty_lots:g} lots)"
                ), None
            # score depth on a soft 0..1 over [min, 3x min].
            depth_score = min(1.0, (top_units - min_units) / max(min_units, 1) / 2.0)

        # ── scoring ──────────────────────────────────────────────────────
        # proximity: 1.0 at the mode target, decays with strike distance (in steps).
        step = int(p.step)
        dist_steps = abs(strike - prox_target) / step if step else abs(strike - prox_target)
        prox_score = 1.0 / (1.0 + dist_steps)
        # distance from ATM (for the tie-break + diagnostics).
        atm_dist_steps = abs(strike - atm) / step if step else abs(strike - atm)

        # liquidity: blended OI+volume headroom over the floors, soft-capped.
        oi_head = min(1.0, (oi - p.min_oi) / max(p.min_oi, 1) / 2.0) if oi else 0.0
        vol_head = min(1.0, (vol - p.min_volume) / max(p.min_volume, 1) / 2.0) if vol else 0.0
        liq_score = 0.5 * oi_head + 0.5 * vol_head

        score = self._composite(prox_score, spread_score, depth_score, liq_score)

        reason = "ok" if have_quotes else "ok (backtest: spread/depth skipped)"
        contract = ChosenContract(
            index=inputs.index,
            security_id=sec_id,
            option_type=side,
            strike=float(strike),
            expiry_date=inputs.expiry_date,
            dte=inputs.dte,
            expiry_restricted=expiry_restricted,
            ref_ltp=float(ltp),
            est_spread_pct=est_spread_pct,
            score=score,
            reason=reason,
            extras={
                "oi": oi,
                "volume": vol,
                "dist_steps": dist_steps,
                "atm_dist_steps": atm_dist_steps,
                "iv_rank": ivr,
                "have_quotes": have_quotes,
            },
        )
        return True, reason, contract

    @staticmethod
    def _composite(
        prox: float,
        spread: Optional[float],
        depth: Optional[float],
        liq: float,
    ) -> float:
        """Weighted blend; drop uncomputable sub-scores and renormalise weights."""
        parts = [(W_PROXIMITY, prox), (W_LIQUIDITY, liq)]
        if spread is not None:
            parts.append((W_SPREAD, spread))
        if depth is not None:
            parts.append((W_DEPTH, depth))
        wsum = sum(w for w, _ in parts)
        if wsum <= 0:
            return 0.0
        return sum(w * v for w, v in parts) / wsum
