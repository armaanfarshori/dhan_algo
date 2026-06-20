"""
Surface Harvester — vega-balanced term-structure REGIME ROUTER (Phase-3).

WHAT IT IS
----------
A regime router that switches the SIGN of its vega exposure by the option
term-structure regime, harvesting the volatility *surface* (term + skew) shape:

  • CONTANGO  (front IV ≤ next IV — calm, upward-sloping term structure)
        → deploy RAVEN (``research.backtest.fno_raven``): a SHORT-vega,
          skew-aware, defined-risk asymmetric iron condor. Already built + gated;
          we REUSE it verbatim (lane discipline).

  • BACKWARDATION (front IV > next IV — near-term stress, inverted term structure)
        → deploy the SKEW-LEAN DIAGONAL (this module): a LONG-vega, defined-risk
          calendar/diagonal — SELL a near-expiry (front) option + BUY a
          farther-expiry (next) option of the same type, slightly skew-leaned in
          strike. Long-vega because the long FAR leg carries more vega than the
          short NEAR leg, so the position BENEFITS when the front is stressed (the
          inverted curve tends to re-normalise / the far vega re-prices up). The
          long far-leg caps the short near-leg → DEFINED RISK.

  • EVENT / EXTREME (expiry day, configured event date, or a degenerate/illegible
        term structure) → STAND_ASIDE (fail-open default).

WHY THE VEGA SIGN FLIP
----------------------
RAVEN (and the whole validated F&O edge) is SHORT vega: it sells rich premium when
the term structure is normal (contango) and the VRP is fat. But premium-selling is
exactly WRONG in backwardation — the front is stressed, realised tends to exceed
implied, and a short-vega condor bleeds. The honest move in backwardation is NOT
"stand aside and earn nothing" but to flip to a LONG-vega, DEFINED-RISK structure
that monetises the inverted term structure: the skew-lean diagonal. The router
composes the two so the book is vega-balanced ACROSS regimes (short-vega in calm,
long-vega in stress), never naked-long-vol and never naked-short-vol in the wrong
regime. GATE-V2's ``is_backwardation`` (term-structure veto) is the switch.

INDEX-AGNOSTIC ([[index-agnostic-fno]])
---------------------------------------
No NIFTY assumptions in the LOGIC. ``lot`` / ``step`` are PARAMETERS (default NIFTY
``NIFTY_LOT`` / ``step=50``, overridable per call for BANKNIFTY/FINNIFTY/etc.), the
gate rides on a per-index ``GateV2Config``, and the cycle dicts are the same
index-agnostic shape ``cycles_from_db`` produces for any ``IndexConfig``. The
diagonal pricing is Black-76 (index-agnostic). A non-NIFTY underlying drives the
router by passing its own cycles + ``GateV2Config`` + ``lot``/``step``.

DATA HONESTY — what is backtestable vs FORWARD-PAPER-ONLY
---------------------------------------------------------
A diagonal needs TWO co-observed expiries' IV per date: the FRONT (rolling
``expiryCode=1`` → ``option_atm_iv`` for that day's nearest expiry) AND the NEXT
(rolling ``expiryCode=2``). ``core.dhan_option_history`` ingests exactly this
rolling 2-expiry IV (pass ``--expiry-codes 1,2``), surfaced on each cycle as
``straddle_iv`` / ``atm_straddle_iv`` (front) + ``next_iv`` (next). Where a cycle
carries BOTH, the diagonal entry credit/debit and the near-expiry resolution are
BACKTESTABLE (Black-76 on the two real IVs). Where ``next_iv`` is ABSENT we DO NOT
fabricate a term structure: the diagonal cycle is marked FORWARD-PAPER-ONLY and
excluded from the backtested P&L (counted + reported, never invented).

The near-leg resolves at the near-expiry settlement (intrinsic). The long FAR leg
is then re-priced at that instant with the remaining DTE using ``next_iv`` HELD
FLAT (a flat-vol MTM approximation — it does NOT see the realised far-vol path).
This is the central diagonal caveat: the long-vega P&L is only as good as the
held-flat ``next_iv`` proxy until forward per-day far-leg marks are collected.

HONESTY / PRELIMINARY CAVEATS (inherited verbatim — never dropped)
------------------------------------------------------------------
VIX-as-weekly-IV proxy (front) · ``next_iv`` rolling-code-2 proxy for the far IV
· CLOSE-not-FSP settlement · near-expiry-resolution + FLAT far-IV MTM (no far-vol
path) · single-σ Black-76 unless real per-strike IV is supplied · small sample
(backwardation is RARE → the diagonal leg's n is SMALL → very wide CIs) · selection
/ config trials inflate Sharpe → deflate it · PAPER / research only. The diagonal
edge is UNVERIFIED until forward 2-expiry IV + far-leg marks are collected — flagged
PRELIMINARY + FORWARD-PAPER throughout.
"""

from __future__ import annotations

import argparse
import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

# ---------------------------------------------------------------------------
# Reused surface — import, never re-implement (lane discipline)
# ---------------------------------------------------------------------------
from ml.fno_vol_gate import (
    SELL_PREMIUM,
    STAND_ASIDE,
    GateV2Config,
    gate_v2_decision,
    is_backwardation,
    is_event_day,
)
from research.backtest.fno_condor import (
    DELTA_IV_SOURCE_FLAT,
    DELTA_IV_SOURCE_REAL,
    black76_call,
    black76_put,
    resolve_iv_source,
    select_short_strike_by_delta,
)
from research.backtest.fno_costs import NIFTY_LOT, condor_costs, slippage
from research.backtest.fno_edge_restate import (
    CycleSeries,
    restate_edge,
)
from research.backtest.fno_raven import (
    RavenParams,
    run_raven_backtest,
)

logger = logging.getLogger("dhan.backtest.fno_surface_harvester")

# ── Regime route labels ────────────────────────────────────────────────────
ROUTE_RAVEN = "raven_short_vega_condor"        # contango → short-vega condor
ROUTE_DIAGONAL = "skew_lean_diagonal_long_vega"  # backwardation → long-vega diagonal
ROUTE_STAND_ASIDE = "stand_aside"                # event/extreme/illegible

# ── Default diagonal structure parameters (index-agnostic) ──────────────────
# Skew-lean: a PUT diagonal leans into the (richer) OTM put skew by default. The
# short near-leg sits at a fixed |delta| (where the near, stressed IV is fattest);
# the long far-leg sits a small number of grid steps FURTHER OTM so it is cheaper
# than the near and the structure is a genuine bounded-risk diagonal.
DEFAULT_DIAGONAL_OPTION_TYPE = "PE"   # lean into the put skew (downside-stress hedge)
DEFAULT_SHORT_NEAR_DELTA = 0.30        # near-the-money short near-leg (rich near-IV)
DEFAULT_LONG_FAR_OFFSET_STRIKES = 2    # long far-leg this many steps further OTM
# A diagonal's far leg must out-DTE the near leg by at least this many days for the
# long-vega term-structure bet to exist (else it collapses to a same-expiry spread).
MIN_TERM_GAP_DAYS = 3

# Extreme-IV stand-aside: an absurd front/next IV (data error or genuine panic
# spike beyond any modellable regime) routes to STAND_ASIDE rather than trading a
# mispriced structure. Bounds are generous — they catch corrupt rows, not normal vol.
EXTREME_IV_FLOOR = 0.01    # 1% annualised — below this the chain is broken
EXTREME_IV_CEIL = 3.00     # 300% annualised — above this is a data error / panic

CAVEATS = (
    "VIX-as-weekly-IV proxy (front) · next_iv rolling-code-2 proxy for far IV · "
    "CLOSE-not-FSP · near-expiry-resolution + FLAT far-IV MTM (no far-vol path) · "
    "single-σ B76 unless real per-strike IV · backwardation RARE → diagonal n small "
    "→ wide CIs · trials inflate Sharpe → deflated · diagonal edge UNVERIFIED "
    "(FORWARD-PAPER until 2-expiry IV + far-leg marks collected) · PAPER only"
)


# ===========================================================================
# Skew-lean diagonal construction (defined-risk, long-vega)
# ===========================================================================
@dataclass(frozen=True)
class DiagonalParams:
    """Skew-lean diagonal structure knobs (index-agnostic — no NIFTY constants).

    Attributes
    ----------
    option_type:
        ``"PE"`` (default — lean into the richer OTM put skew) or ``"CE"``.
    short_near_delta:
        Target |delta| of the SHORT NEAR-expiry leg (default
        :data:`DEFAULT_SHORT_NEAR_DELTA`). Chosen via the fixed-delta machinery on
        the NEAR (front) IV, so under a real per-strike skew map the short sits
        where the near, stressed vol is fattest.
    long_far_offset_strikes:
        Grid steps FURTHER OTM (away from spot) at which the LONG FAR-expiry leg
        sits relative to the short near strike (default
        :data:`DEFAULT_LONG_FAR_OFFSET_STRIKES`). ``0`` ⇒ a pure calendar (same
        strike, different expiry); ``> 0`` ⇒ a diagonal. Must be ``>= 0``.
    """

    option_type: str = DEFAULT_DIAGONAL_OPTION_TYPE
    short_near_delta: float = DEFAULT_SHORT_NEAR_DELTA
    long_far_offset_strikes: int = DEFAULT_LONG_FAR_OFFSET_STRIKES

    def __post_init__(self) -> None:
        if self.option_type not in ("PE", "CE"):
            raise ValueError(
                f"DiagonalParams.option_type must be 'PE'|'CE', got {self.option_type!r}"
            )
        if not (0.0 < self.short_near_delta < 1.0):
            raise ValueError(
                f"DiagonalParams.short_near_delta must be in (0, 1), got "
                f"{self.short_near_delta}"
            )
        if self.long_far_offset_strikes < 0:
            raise ValueError(
                "DiagonalParams.long_far_offset_strikes must be >= 0, got "
                f"{self.long_far_offset_strikes}"
            )


def build_skew_lean_diagonal(
    spot: float,
    front_iv: float,
    near_dte: int,
    *,
    params: DiagonalParams | None = None,
    step: int = 50,
    per_strike_iv: dict[int, float] | None = None,
) -> tuple[dict[str, int], str]:
    """Construct the two strikes of a defined-risk skew-lean diagonal.

    SELL a near-expiry option at ``params.short_near_delta`` (fixed-delta on the
    FRONT IV, via :func:`fno_condor.select_short_strike_by_delta` — REAL per-strike
    IV when present, flat front IV otherwise) and BUY a far-expiry option
    ``params.long_far_offset_strikes`` grid steps FURTHER OTM (away from spot). For
    a PUT diagonal "further OTM" is LOWER; for a CALL diagonal it is HIGHER.

    Returns ``(strikes, delta_iv_source)`` where ``strikes`` has the keys
    ``short_near_k`` / ``long_far_k`` / ``option_type`` and ``delta_iv_source`` is
    :data:`fno_condor.DELTA_IV_SOURCE_REAL` when the short resolved a real
    per-strike IV (skew SEEN), else :data:`fno_condor.DELTA_IV_SOURCE_FLAT`.

    DEFINED-RISK GUARD
    ------------------
    ``near_dte <= 0`` is rejected (at/after near-expiry the Black-76 delta
    degenerates to a moneyness step). A valid diagonal always enters with the near
    leg ``near_dte > 0``. The long far-leg (further OTM, longer-dated) caps the
    short near-leg's loss → the structure is bounded-risk by construction.
    """
    if near_dte <= 0:
        raise ValueError(
            f"build_skew_lean_diagonal: near_dte must be > 0, got {near_dte}"
        )
    p = params or DiagonalParams()

    short_near_k, src = select_short_strike_by_delta(
        spot, front_iv, near_dte, p.option_type, p.short_near_delta,
        step=step, per_strike_iv=per_strike_iv,
    )
    # Long far-leg sits FURTHER OTM than the short near-leg: PUT → lower, CALL →
    # higher. offset 0 ⇒ same strike (pure calendar).
    direction = -1 if p.option_type == "PE" else +1
    long_far_k = short_near_k + direction * p.long_far_offset_strikes * step

    if long_far_k <= 0:
        raise ValueError(
            "build_skew_lean_diagonal: long far strike <= 0 "
            f"(short_near_k={short_near_k}, offset={p.long_far_offset_strikes}, "
            f"step={step}) — degenerate (sub-grid spot?)"
        )

    delta_iv_source = (
        DELTA_IV_SOURCE_REAL if src == DELTA_IV_SOURCE_REAL else DELTA_IV_SOURCE_FLAT
    )
    return (
        {
            "short_near_k": short_near_k,
            "long_far_k": long_far_k,
            "option_type": p.option_type,
        },
        delta_iv_source,
    )


def _b76(option_type: str, spot: float, strike: float, dte: int, iv: float) -> float:
    """Black-76 premium per unit for one leg (F ≈ spot, T = dte / 365)."""
    T = max(dte, 0) / 365.0
    if option_type == "CE":
        return black76_call(spot, strike, T, iv)
    return black76_put(spot, strike, T, iv)


# ===========================================================================
# Diagonal trade record + single-cycle pricing/resolution
# ===========================================================================
@dataclass
class DiagonalTrade:
    """Outcome record for one diagonal cycle (near-expiry-resolved, flat far MTM)."""

    entry_date: date
    near_expiry_date: date
    far_expiry_date: date
    strikes: dict[str, int]
    near_dte: int
    far_dte: int
    front_iv: float
    next_iv: float
    entry_net: float        # ₹ per position: + = net CREDIT received, − = net DEBIT paid
    gross_pnl: float        # ₹ before costs
    costs: float            # ₹ statutory + brokerage (entry both legs + near-leg exit)
    net_pnl: float          # gross − costs
    margin: float           # ₹ defined-risk capital at risk (long debit + near wing cap)
    win: bool
    delta_iv_source: str = ""
    long_vega: float = 0.0  # far-leg vega − near-leg vega per unit (>0 ⇒ long vega)


def _b76_vega_per_unit(spot: float, strike: float, dte: int, iv: float) -> float:
    """Black-76 vega per UNIT (∂price/∂σ, per 1.0 vol), F ≈ spot, T = dte/365.

    vega = F · φ(d1) · √T  (call/put share the same vega). Used only as a SIGN
    diagnostic (is the diagonal net long vega?) — not in P&L. T<=0 / σ<=0 → 0.
    """
    T = max(dte, 0) / 365.0
    if T <= 0.0 or iv <= 0.0 or spot <= 0.0 or strike <= 0.0:
        return 0.0
    sqrt_T = math.sqrt(T)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * T) / (iv * sqrt_T)
    phi = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    return spot * phi * sqrt_T


def price_and_resolve_diagonal(
    cycle: dict[str, Any],
    *,
    params: DiagonalParams,
    lot: int = NIFTY_LOT,
    step: int = 50,
    slip_pct: float = 0.005,
) -> DiagonalTrade | None:
    """Price + near-expiry-resolve ONE skew-lean diagonal cycle. ``None`` if the
    cycle lacks a usable 2-expiry term structure (FORWARD-PAPER, not backtestable).

    Backtestable path (cycle carries BOTH front + next IV):
      ENTRY  — SELL near leg @ front_iv/near_dte, BUY far leg @ next_iv/far_dte;
               adverse slippage on each (SELL receives less, BUY pays more).
      RESOLVE at near-expiry settlement (``expiry_spot``):
               * short near leg → intrinsic payoff.
               * long far leg → RE-PRICED with the remaining far DTE
                 (far_dte − near_dte) using ``next_iv`` HELD FLAT (flat-vol MTM —
                 the central caveat; no far-vol path), sold at close-side slippage.
      gross = (entry net credit/debit) + (far-leg residual value sold) − (near-leg
              intrinsic paid). costs = entry stack (both legs) + near-leg buy-to-
              close stack. margin = defined-risk capital at risk.

    ``near_dte`` = ``cycle["dte"]`` (front). ``far_dte`` = ``cycle["next_dte"]`` if
    present else derived from ``cycle["next_expiry_date"]`` − entry; if neither, a
    one-front-cycle gap is assumed (``near_dte`` + the front inter-expiry span,
    floored so the term gap >= :data:`MIN_TERM_GAP_DAYS`). A term gap below
    :data:`MIN_TERM_GAP_DAYS` ⇒ ``None`` (not a real diagonal).
    """
    front_iv, _src = resolve_iv_source(cycle)
    next_iv = cycle.get("next_iv")
    spot = cycle.get("spot")
    near_dte_raw = cycle.get("dte")
    expiry_spot = cycle.get("expiry_spot")
    if (
        front_iv is None
        or next_iv is None
        or spot is None
        or near_dte_raw is None
        or expiry_spot is None
    ):
        return None  # FORWARD-PAPER: insufficient 2-expiry data — do not fabricate.
    try:
        front_iv = float(front_iv)
        next_iv = float(next_iv)
        spot = float(spot)
        near_dte = int(near_dte_raw)
        expiry_spot = float(expiry_spot)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: int(inf)/int(nan) on a corrupt cycle — fail-open (skip),
        # never crash the backtest on one bad row.
        return None
    if not (
        math.isfinite(front_iv) and math.isfinite(next_iv)
        and math.isfinite(spot) and math.isfinite(expiry_spot)
    ):
        return None
    if front_iv <= 0 or next_iv <= 0 or spot <= 0 or near_dte <= 0 or expiry_spot <= 0:
        return None

    # Far DTE: explicit next_dte, else next_expiry_date − entry, else one-front gap.
    far_dte = _resolve_far_dte(cycle, near_dte)
    if far_dte is None or (far_dte - near_dte) < MIN_TERM_GAP_DAYS:
        return None  # no genuine term gap → not a diagonal.

    strikes, delta_iv_source = build_skew_lean_diagonal(
        spot, front_iv, near_dte,
        params=params, step=step, per_strike_iv=cycle.get("per_strike_iv"),
    )
    ot = strikes["option_type"]
    near_k = float(strikes["short_near_k"])
    far_k = float(strikes["long_far_k"])

    # ── ENTRY pricing (adverse slippage; SELL near, BUY far) ────────────────
    near_mid = _b76(ot, spot, near_k, near_dte, front_iv)
    far_mid = _b76(ot, spot, far_k, far_dte, next_iv)
    near_fill = near_mid - slippage(near_mid, slip_pct)   # SELL: receive less
    far_fill = far_mid + slippage(far_mid, slip_pct)      # BUY: pay more
    entry_net_per_unit = near_fill - far_fill             # +credit / −debit
    entry_net = entry_net_per_unit * lot

    # ── RESOLVE at near-expiry ──────────────────────────────────────────────
    near_intrinsic = _leg_intrinsic(ot, near_k, expiry_spot)   # we pay this (short)
    far_residual_dte = max(far_dte - near_dte, 0)
    far_residual_mid = _b76(ot, expiry_spot, far_k, far_residual_dte, next_iv)
    far_sold = far_residual_mid - slippage(far_residual_mid, slip_pct)  # SELL: less

    gross_per_unit = entry_net_per_unit + far_sold - near_intrinsic
    gross_pnl = gross_per_unit * lot

    # ── Costs: entry both legs + near-leg buy-to-close (far leg sold at near-exp) ──
    entry_legs = [
        (near_fill, lot, "SELL"),
        (far_fill, lot, "BUY"),
    ]
    # At near-expiry we BUY-to-close the short near (its intrinsic) and SELL-to-close
    # the long far (its residual). Both incur the statutory stack.
    near_close_px = _leg_intrinsic(ot, near_k, expiry_spot) + slippage(
        _leg_intrinsic(ot, near_k, expiry_spot), slip_pct
    )
    exit_legs = [
        (near_close_px, lot, "BUY"),
        (far_sold, lot, "SELL"),
    ]
    costs = condor_costs(entry_legs).total + condor_costs(exit_legs).total
    net_pnl = gross_pnl - costs

    # ── Defined-risk margin ─────────────────────────────────────────────────
    margin = _diagonal_margin(
        ot, near_k, far_k, entry_net_per_unit, lot,
        params.long_far_offset_strikes, step,
    )

    long_vega = (
        _b76_vega_per_unit(spot, far_k, far_dte, next_iv)
        - _b76_vega_per_unit(spot, near_k, near_dte, front_iv)
    )

    return DiagonalTrade(
        entry_date=cycle.get("entry_date", date(1970, 1, 1)),
        near_expiry_date=cycle.get("expiry_date", date(1970, 1, 1)),
        far_expiry_date=cycle.get("next_expiry_date", cycle.get("expiry_date", date(1970, 1, 1))),
        strikes=strikes,
        near_dte=near_dte,
        far_dte=far_dte,
        front_iv=front_iv,
        next_iv=next_iv,
        entry_net=entry_net,
        gross_pnl=gross_pnl,
        costs=costs,
        net_pnl=net_pnl,
        margin=margin,
        win=net_pnl > 0,
        delta_iv_source=delta_iv_source,
        long_vega=long_vega,
    )


def _resolve_far_dte(cycle: dict[str, Any], near_dte: int) -> int | None:
    """Far-leg DTE: explicit ``next_dte`` → ``next_expiry_date`` − entry → assumed
    one-front gap (near_dte doubled, floored to a real term gap). ``None`` only when
    a supplied value is unusable."""
    nd = cycle.get("next_dte")
    if nd is not None:
        try:
            v = int(nd)
            return v if v > near_dte else None
        except (TypeError, ValueError):
            return None
    nxd = cycle.get("next_expiry_date")
    ed = cycle.get("entry_date")
    if nxd is not None and ed is not None:
        try:
            v = (nxd - ed).days
            return v if v > near_dte else None
        except (TypeError, AttributeError):
            return None
    # No explicit far expiry → assume the next front sits one inter-expiry span out.
    # Floor the gap to MIN_TERM_GAP_DAYS so a tiny near_dte still yields a diagonal.
    return near_dte + max(near_dte, MIN_TERM_GAP_DAYS)


def _leg_intrinsic(option_type: str, strike: float, spot: float) -> float:
    """Per-unit intrinsic value at expiry (buyer's value, >= 0)."""
    if option_type == "CE":
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def _diagonal_margin(
    option_type: str,
    near_k: float,
    far_k: float,
    entry_net_per_unit: float,
    lot: int,
    offset_strikes: int,
    step: int,
) -> float:
    """Defined-risk capital at risk for the diagonal (₹, per position).

    The long far-leg caps the short near-leg. The dominant capital at risk is the
    NET DEBIT paid (when the structure is a debit) PLUS the bounded near-vs-far
    strike gap exposure (a vertical-spread-like cap of ``|near_k − far_k|`` per unit
    that the long can fail to fully cover only by the residual time value, which the
    broker blocks). We take the conservative ``max`` of the two so ROM is never
    flattered:

        debit_cap   = max(0, −entry_net_per_unit) · lot     (net debit paid)
        spread_cap  = |near_k − far_k| · lot                (strike-gap cap)
        margin      = max(debit_cap, spread_cap, MIN_FLOOR)

    A pure calendar (offset 0) has ``spread_cap = 0`` so the debit IS the risk. The
    floor keeps ROM finite for a (rare) net-credit zero-gap calendar.
    """
    debit_cap = max(0.0, -entry_net_per_unit) * lot
    spread_cap = abs(near_k - far_k) * lot
    margin = max(debit_cap, spread_cap)
    # Floor: at least one strike-step of capital at risk (a position is never free).
    return max(margin, float(step) * lot)


# ===========================================================================
# Diagonal backtest over the gated (backwardation) subset
# ===========================================================================
def gate_diagonal_cycles(
    cycles: list[dict[str, Any]],
    *,
    cfg: GateV2Config | None = None,
    event_dates: list[date] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split cycles into (BACKWARDATION-and-not-event) deploy set vs the rest.

    The diagonal deploys ONLY when the term structure is in BACKWARDATION
    (``is_backwardation(front_iv, next_iv)``) AND it is not an event/expiry day AND
    the IVs are not extreme/illegible. Returns ``(deploy, skipped)`` where ``deploy``
    is the diagonal subset and ``skipped`` carries everything else (for accounting).
    Index-agnostic (the gate config rides per index). Fail-open: a cycle with no
    usable term structure goes to ``skipped`` (never deployed on missing data).
    """
    _cfg = cfg or GateV2Config()  # noqa: F841 — reserved for future per-index event cfg
    deploy: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for c in cycles:
        front_iv, _s = resolve_iv_source(c)
        next_iv = c.get("next_iv")
        if (
            front_iv is None
            or next_iv is None
            or not _iv_legible(front_iv)
            or not _iv_legible(next_iv)
        ):
            skipped.append(c)
            continue
        event = is_event_day(c.get("entry_date"), c.get("expiry_date"), event_dates)
        if is_backwardation(front_iv, next_iv) and not event:
            deploy.append(c)
        else:
            skipped.append(c)
    return deploy, skipped


def _iv_legible(iv: Any) -> bool:
    """True when ``iv`` is a finite positive value inside the modellable band."""
    try:
        v = float(iv)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and EXTREME_IV_FLOOR <= v <= EXTREME_IV_CEIL


@dataclass
class DiagonalResult:
    """Diagonal backtest output: trades + the honest restate + data-honesty counts."""

    trades: list[DiagonalTrade]
    metrics: dict[str, Any]
    restate: dict[str, Any]
    n_backwardation_cycles: int
    n_backtestable: int
    n_forward_paper_only: int
    # Trades whose long_vega <= 0 — the structure was NOT net long vega for those
    # cycles (a far leg with much lower IV + further-OTM strike can carry LESS vega
    # than the short near leg). The diagonal's long-vega thesis does NOT hold for
    # these; they are counted + surfaced (never silently folded into a "long-vega"
    # result). Non-zero ⇒ the chosen offset / IV regime is too aggressive for this
    # underlying — tighten ``long_far_offset_strikes`` or stand aside.
    n_vega_reversed: int = 0
    delta_iv_sources: dict[str, int] = field(default_factory=dict)


def run_diagonal_backtest(
    cycles: list[dict[str, Any]],
    *,
    params: DiagonalParams | None = None,
    gate_config: GateV2Config | None = None,
    event_dates: list[date] | None = None,
    capital: float = 200_000.0,
    lot: int = NIFTY_LOT,
    step: int = 50,
    slip_pct: float = 0.005,
    n_configs_tried: int = 1,
) -> DiagonalResult:
    """Backtest the skew-lean diagonal over the BACKWARDATION subset (honest).

    Steps:
      1. ``gate_diagonal_cycles`` → the backwardation, non-event, legible subset.
      2. For each such cycle, ``price_and_resolve_diagonal``. Cycles with a usable
         2-expiry term structure are BACKTESTED; cycles missing ``next_iv`` (or with
         no real term gap) are counted FORWARD-PAPER-ONLY and excluded — never
         fabricated.
      3. Honest restate (``fno_edge_restate.restate_edge``) on the backtested
         diagonal P&L. NO passive benchmark is supplied (there is no "always-on
         diagonal" baseline the way there is for a short condor) → the
         BEAT_PASSIVE_AND_RANDOM gate fails closed, which is the honest outcome for
         a rare-regime long-vega structure on a tiny sample.

    Returns a :class:`DiagonalResult`. The PRELIMINARY + FORWARD-PAPER caveats ride
    on the restate verdict + this module's :data:`CAVEATS`.
    """
    p = params or DiagonalParams()
    deploy, _skipped = gate_diagonal_cycles(
        cycles, cfg=gate_config, event_dates=event_dates,
    )

    trades: list[DiagonalTrade] = []
    n_forward = 0
    for c in deploy:
        t = price_and_resolve_diagonal(
            c, params=p, lot=lot, step=step, slip_pct=slip_pct,
        )
        if t is None:
            n_forward += 1
        else:
            trades.append(t)

    trades.sort(key=lambda t: t.entry_date)
    metrics = _diagonal_metrics(trades, capital=capital)

    delta_iv_sources = {DELTA_IV_SOURCE_REAL: 0, DELTA_IV_SOURCE_FLAT: 0}
    for t in trades:
        delta_iv_sources[t.delta_iv_source] = (
            delta_iv_sources.get(t.delta_iv_source, 0) + 1
        )

    if trades:
        series = CycleSeries(
            pnl=[float(t.net_pnl) for t in trades],
            margin=[float(t.margin) for t in trades],
            short_vol_factor=[float(t.next_iv - t.front_iv) for t in trades],
            # NO passive benchmark — see docstring (fail-closed BEAT_PASSIVE gate).
            iv_source=[],  # provenance is unknown/proxy → PRELIMINARY (conservative)
            capital=capital,
            n_configs_tried=n_configs_tried,
        )
        restate = restate_edge(series)
    else:
        restate = restate_edge(CycleSeries(pnl=[], margin=[]))

    n_vega_reversed = sum(1 for t in trades if t.long_vega <= 0)

    return DiagonalResult(
        trades=trades,
        metrics=metrics,
        restate=restate,
        n_backwardation_cycles=len(deploy),
        n_backtestable=len(trades),
        n_forward_paper_only=n_forward,
        n_vega_reversed=n_vega_reversed,
        delta_iv_sources=delta_iv_sources,
    )


def _diagonal_metrics(trades: list[DiagonalTrade], *, capital: float) -> dict[str, Any]:
    """Headline metrics for the diagonal leg (mirrors the engine metric shape)."""
    from research.backtest.fno_strategies import _max_drawdown, _sharpe_from_pnls

    n = len(trades)
    if n == 0:
        return {
            "n_trades": 0, "win_rate": 0.0, "net_pnl": 0.0,
            "return_on_margin": 0.0, "mean_span": 0.0, "sharpe": 0.0,
            "max_drawdown": 0.0, "return_on_capital": 0.0, "mean_long_vega": 0.0,
        }
    pnls = [t.net_pnl for t in trades]
    margins = [t.margin for t in trades]
    total_margin = sum(margins)
    net = sum(pnls)
    return {
        "n_trades": n,
        "win_rate": sum(1 for p in pnls if p > 0) / n,
        "net_pnl": net,
        "return_on_margin": (net / total_margin) if total_margin > 0 else 0.0,
        "mean_span": total_margin / n,
        "sharpe": _sharpe_from_pnls(pnls),
        "max_drawdown": _max_drawdown(pnls),
        "return_on_capital": net / capital if capital > 0 else 0.0,
        "mean_long_vega": sum(t.long_vega for t in trades) / n,
    }


# ===========================================================================
# Regime router — per-cycle route + composed backtest
# ===========================================================================
@dataclass(frozen=True)
class RouteDecision:
    """Per-cycle routing verdict + the term-structure diagnostics."""

    route: str                  # ROUTE_RAVEN | ROUTE_DIAGONAL | ROUTE_STAND_ASIDE
    front_iv: float | None
    next_iv: float | None
    backwardation: bool
    event: bool
    reason: str = ""


def regime_route(
    cycle: dict[str, Any],
    *,
    cfg: GateV2Config | None = None,
    trailing_spreads: list[float | None] | None = None,
    event_dates: list[date] | None = None,
) -> RouteDecision:
    """Route ONE cycle by term-structure regime (fail-open STAND_ASIDE).

    Priority:
      1. EVENT / illegible / extreme IV → STAND_ASIDE.
      2. BACKWARDATION (front IV > next IV) → DIAGONAL (long-vega).
      3. CONTANGO + GATE-V2 says SELL_PREMIUM → RAVEN (short-vega condor).
      4. Otherwise (contango but GATE-V2 not SELL — VRP thin etc.) → STAND_ASIDE.

    GATE-V2 is the authority for the RAVEN branch (it already encodes the VRP-rich +
    no-backwardation + no-event checks); we read its term-structure + event verdict
    via ``is_backwardation`` / ``is_event_day`` for the explicit branch labels.
    Index-agnostic: ``cfg`` rides per index. Fail-open: any degenerate input →
    STAND_ASIDE (never an aggressive trade on missing data).
    """
    _cfg = cfg or GateV2Config()
    front_iv, _s = resolve_iv_source(cycle)
    next_iv = cycle.get("next_iv")
    entry_date = cycle.get("entry_date")
    expiry_date = cycle.get("expiry_date")

    event = is_event_day(entry_date, expiry_date, event_dates)
    if event:
        return RouteDecision(
            route=ROUTE_STAND_ASIDE, front_iv=front_iv, next_iv=next_iv,
            backwardation=False, event=True, reason="event/expiry day → stand aside",
        )
    if front_iv is None or not _iv_legible(front_iv):
        return RouteDecision(
            route=ROUTE_STAND_ASIDE, front_iv=front_iv, next_iv=next_iv,
            backwardation=False, event=False,
            reason="front IV missing/illegible → stand aside (fail-open)",
        )
    if next_iv is not None and not _iv_legible(next_iv):
        return RouteDecision(
            route=ROUTE_STAND_ASIDE, front_iv=front_iv, next_iv=next_iv,
            backwardation=False, event=False,
            reason="next IV illegible/extreme → stand aside (fail-open)",
        )

    backwardation = is_backwardation(front_iv, next_iv)
    if backwardation:
        return RouteDecision(
            route=ROUTE_DIAGONAL, front_iv=front_iv, next_iv=next_iv,
            backwardation=True, event=False,
            reason="backwardation (front IV > next IV) → long-vega diagonal",
        )

    # Contango (or no term structure): defer to GATE-V2 for the short-vega branch.
    rvol = cycle.get("realized_vol_20d")
    if rvol is None:
        return RouteDecision(
            route=ROUTE_STAND_ASIDE, front_iv=front_iv, next_iv=next_iv,
            backwardation=False, event=False,
            reason="no realized vol → GATE-V2 cannot rule → stand aside",
        )
    verdict = gate_v2_decision(
        float(rvol), front_iv,
        trailing_spreads=trailing_spreads,
        front_iv=front_iv, next_iv=next_iv,
        cycle_date=entry_date, expiry_date=expiry_date, event_dates=event_dates,
        rv_is_trading_basis=True, cfg=_cfg,
    )
    if verdict.decision == SELL_PREMIUM:
        return RouteDecision(
            route=ROUTE_RAVEN, front_iv=front_iv, next_iv=next_iv,
            backwardation=False, event=False,
            reason="contango + GATE-V2 SELL_PREMIUM → short-vega RAVEN condor",
        )
    return RouteDecision(
        route=ROUTE_STAND_ASIDE, front_iv=front_iv, next_iv=next_iv,
        backwardation=False, event=(verdict.decision == STAND_ASIDE and verdict.event_veto),
        reason=f"contango but GATE-V2={verdict.decision} → stand aside",
    )


def route_cycles(
    cycles: list[dict[str, Any]],
    *,
    cfg: GateV2Config | None = None,
    event_dates: list[date] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[RouteDecision]]:
    """Route every cycle (chronological), bucketing by route.

    Returns ``(buckets, decisions)`` where ``buckets`` maps each ROUTE_* label to
    its cycle list (RAVEN gets the contango-SELL subset, DIAGONAL the backwardation
    subset, STAND_ASIDE the rest) and ``decisions`` is the parallel per-cycle
    :class:`RouteDecision` list. PIT trailing VRP spreads for the RAVEN/GATE-V2
    percentile sub-gate are accumulated chronologically (no look-ahead).
    """
    _cfg = cfg or GateV2Config()
    buckets: dict[str, list[dict[str, Any]]] = {
        ROUTE_RAVEN: [], ROUTE_DIAGONAL: [], ROUTE_STAND_ASIDE: [],
    }
    decisions: list[RouteDecision] = []
    trailing = _PitTrailingSpreads(_cfg)
    for c in cycles:
        d = regime_route(
            c, cfg=_cfg, trailing_spreads=trailing.snapshot(), event_dates=event_dates,
        )
        buckets[d.route].append(c)
        decisions.append(d)
        trailing.observe(c)
    return buckets, decisions


class _PitTrailingSpreads:
    """Point-in-time trailing IV−RV spread accumulator for the router's GATE-V2 call.

    Mirrors ``fno_raven._trailing_spreads_before`` but streamed: ``observe`` appends
    the just-decided cycle's spread AFTER it was routed, so ``snapshot`` only ever
    returns spreads from STRICTLY EARLIER cycles (no look-ahead)."""

    def __init__(self, cfg: GateV2Config) -> None:
        self._cfg = cfg
        self._spreads: list[float | None] = []

    def snapshot(self) -> list[float | None]:
        return list(self._spreads)

    def observe(self, cycle: dict[str, Any]) -> None:
        from ml.fno_vol_gate import _rebase_to_calendar, vrp_spread  # noqa: PLC0415

        iv, _s = resolve_iv_source(cycle)
        rvol = cycle.get("realized_vol_20d")
        if iv is None or rvol is None:
            self._spreads.append(None)
            return
        rv_cal = _rebase_to_calendar(float(rvol))
        self._spreads.append(vrp_spread(rv_cal, iv))


@dataclass
class HarvesterResult:
    """Full surface-harvester output: routed legs + honest restate vs baselines."""

    route_counts: dict[str, int]
    raven_result: Any                 # research.backtest.fno_raven.RavenResult
    diagonal_result: DiagonalResult
    restate_router: dict[str, Any]
    restate_always_raven: dict[str, Any]
    restate_always_condor: dict[str, Any]
    n_input_cycles: int


def run_surface_harvester_backtest(
    cycles: list[dict[str, Any]],
    *,
    cfg: GateV2Config | None = None,
    raven_params: RavenParams | None = None,
    diagonal_params: DiagonalParams | None = None,
    event_dates: list[date] | None = None,
    capital: float = 200_000.0,
    lot: int = NIFTY_LOT,
    step: int = 50,
    slip_pct: float = 0.005,
    n_configs_tried: int = 1,
) -> HarvesterResult:
    """Backtest the routed surface harvester + honest restate vs two baselines.

    Pipeline:
      1. ``route_cycles`` → RAVEN (contango-SELL) / DIAGONAL (backwardation) /
         STAND_ASIDE buckets (PIT, no look-ahead).
      2. RAVEN leg via ``fno_raven.run_raven_backtest`` on the RAVEN bucket; diagonal
         leg via ``run_diagonal_backtest`` on the DIAGONAL bucket (its own internal
         backwardation gate is a no-op here — the cycles are already backwardation —
         but it still splits backtestable vs FORWARD-PAPER).
      3. The ROUTER P&L series = RAVEN trades ∪ diagonal trades (chronological),
         restated honestly. Baselines for the comparison:
           * ALWAYS-RAVEN — RAVEN run over ALL cycles (its own GATE-V2 still gates
             internally) — "what if we never flipped to the diagonal?"
           * ALWAYS-CONDOR — the symmetric baseline condor (RAVEN's own baseline),
             the bar the orchestrator MVP measured against.
         Each baseline is restated identically so the router's incremental value is
         an apples-to-apples honest comparison.

    HONEST FRAMING: backwardation is RARE → the diagonal leg's n is SMALL → its CIs
    are very wide and most diagonal cycles will be FORWARD-PAPER (no ``next_iv`` in
    the historical proxy). The router's restate is therefore PRELIMINARY +
    FORWARD-PAPER-heavy; read the diagonal contribution as a forward-paper hypothesis,
    not a proven historical edge.

    ``n_configs_tried`` HONESTY: the default ``1`` says "this exact config was run
    once, untuned". But the harvester is a SELECTION FUNNEL across MANY knobs — the
    diagonal (``option_type`` × ``short_near_delta`` × ``long_far_offset_strikes``),
    RAVEN (its four deltas/wings), GATE-V2 ``k``, and the router thresholds. If you
    SWEEP any of these and report the best, the true trial count is the product of
    the swept grid sizes (often 10–100+), and the deflated-Sharpe haircut in the
    restate is only honest if you pass that count. Leaving it at 1 after a sweep
    OVER-states the edge — raise ``n_configs_tried`` to the real number of configs
    explored before quoting any GO.
    """
    _cfg = cfg or GateV2Config()
    rp = raven_params or RavenParams()
    dp = diagonal_params or DiagonalParams()

    buckets, _decisions = route_cycles(cycles, cfg=_cfg, event_dates=event_dates)

    raven_result = run_raven_backtest(
        buckets[ROUTE_RAVEN],
        raven_params=rp, gate_config=_cfg, event_dates=event_dates,
        capital=capital, lot=lot, step=step, slip_pct=slip_pct,
        n_configs_tried=n_configs_tried,
        # The bucket is already contango+SELL; RAVEN's internal GATE-V2 re-confirms.
    )
    diagonal_result = run_diagonal_backtest(
        buckets[ROUTE_DIAGONAL],
        params=dp, gate_config=_cfg, event_dates=event_dates,
        capital=capital, lot=lot, step=step, slip_pct=slip_pct,
        n_configs_tried=n_configs_tried,
    )

    # ── Router P&L series = RAVEN trades ∪ diagonal trades (chronological) ───
    restate_router = _restate_router(
        raven_result, diagonal_result,
        capital=capital, n_configs_tried=n_configs_tried,
    )

    # ── Baseline 1: ALWAYS-RAVEN (never flip) over ALL cycles ───────────────
    always_raven = run_raven_backtest(
        cycles, raven_params=rp, gate_config=_cfg, event_dates=event_dates,
        capital=capital, lot=lot, step=step, slip_pct=slip_pct,
        n_configs_tried=n_configs_tried,
    )
    restate_always_raven = always_raven.restate

    # ── Baseline 2: ALWAYS-CONDOR (symmetric baseline) — RAVEN already restates
    #    its asymmetry vs the symmetric baseline; we surface that baseline's own
    #    honest series as the always-condor restate (same gated cycles). ─────
    restate_always_condor = _restate_baseline_condor(
        always_raven, capital=capital, n_configs_tried=n_configs_tried,
    )

    route_counts = {k: len(v) for k, v in buckets.items()}

    return HarvesterResult(
        route_counts=route_counts,
        raven_result=raven_result,
        diagonal_result=diagonal_result,
        restate_router=restate_router,
        restate_always_raven=restate_always_raven,
        restate_always_condor=restate_always_condor,
        n_input_cycles=len(cycles),
    )


def _restate_router(
    raven_result: Any,
    diagonal_result: DiagonalResult,
    *,
    capital: float,
    n_configs_tried: int,
) -> dict[str, Any]:
    """Honest restate of the COMBINED router P&L (RAVEN ∪ diagonal trades).

    Builds one chronological CycleSeries from the RAVEN trades (net_pnl + span) and
    the backtested diagonal trades (net_pnl + margin), then restates. No passive
    benchmark (a combined RAVEN+diagonal book has no single always-on baseline) →
    BEAT_PASSIVE fails closed (honest)."""
    raven_trades = raven_result.raven_metrics.get("trades", [])
    diag_trades = diagonal_result.trades

    rows: list[tuple[date, float, float]] = []
    for t in raven_trades:
        rows.append((t.entry_date, float(t.net_pnl), float(t.span)))
    for t in diag_trades:
        rows.append((t.entry_date, float(t.net_pnl), float(t.margin)))
    rows.sort(key=lambda r: r[0])

    if not rows:
        return restate_edge(CycleSeries(pnl=[], margin=[]))

    series = CycleSeries(
        pnl=[r[1] for r in rows],
        margin=[r[2] for r in rows],
        iv_source=[],  # mixed/proxy provenance → PRELIMINARY (conservative)
        capital=capital,
        n_configs_tried=n_configs_tried,
    )
    return restate_edge(series)


def _restate_baseline_condor(
    raven_result: Any,
    *,
    capital: float,
    n_configs_tried: int,
) -> dict[str, Any]:
    """Honest restate of the ALWAYS-CONDOR (symmetric baseline) leg.

    Reuses the symmetric-baseline trades RAVEN already computed (same gated cycles)
    as the always-condor book — the bar the orchestrator MVP measured against."""
    base_trades = raven_result.baseline_metrics.get("trades", [])
    if not base_trades:
        return restate_edge(CycleSeries(pnl=[], margin=[]))
    ordered = sorted(base_trades, key=lambda t: t.entry_date)
    series = CycleSeries(
        pnl=[float(t.net_pnl) for t in ordered],
        margin=[float(t.span) for t in ordered],
        iv_source=[],
        capital=capital,
        n_configs_tried=n_configs_tried,
    )
    return restate_edge(series)


# ===========================================================================
# Reporting
# ===========================================================================
def render_report(result: HarvesterResult) -> str:
    """Human-readable surface-harvester report: routes + both legs + restates."""
    rc = result.route_counts
    dr = result.diagonal_result
    rv = result.raven_result

    lines: list[str] = []
    lines.append("=" * 80)
    lines.append("SURFACE HARVESTER — RAVEN↔diagonal regime router (PRELIMINARY / PAPER)")
    lines.append("=" * 80)
    lines.append(f"input cycles={result.n_input_cycles}")
    lines.append(
        f"routes: RAVEN(contango)={rc.get(ROUTE_RAVEN, 0)}  "
        f"DIAGONAL(backwardation)={rc.get(ROUTE_DIAGONAL, 0)}  "
        f"STAND_ASIDE={rc.get(ROUTE_STAND_ASIDE, 0)}"
    )
    lines.append("-" * 80)
    rm = rv.raven_metrics
    lines.append(
        f"RAVEN leg   : n={rm.get('n_trades', 0):>4}  "
        f"net=₹{rm.get('net_pnl', 0.0):>12,.0f}  "
        f"ROM={rm.get('return_on_margin', 0.0):>7.2%}  "
        f"sharpe={rm.get('sharpe', 0.0):>6.2f}"
    )
    dm = dr.metrics
    lines.append(
        f"DIAGONAL leg: n={dm.get('n_trades', 0):>4}  "
        f"net=₹{dm.get('net_pnl', 0.0):>12,.0f}  "
        f"ROM={dm.get('return_on_margin', 0.0):>7.2%}  "
        f"sharpe={dm.get('sharpe', 0.0):>6.2f}  "
        f"long_vega(mean)={dm.get('mean_long_vega', 0.0):>8.2f}"
    )
    lines.append(
        f"  diagonal data: backwardation cycles={dr.n_backwardation_cycles}  "
        f"backtested={dr.n_backtestable}  FORWARD-PAPER-only={dr.n_forward_paper_only}"
    )
    if dr.n_vega_reversed:
        lines.append(
            f"  WARNING: {dr.n_vega_reversed}/{dr.n_backtestable} diagonal trades were "
            "NOT net long vega (far-leg vega < near-leg vega) — long-vega thesis "
            "VIOLATED for those; tighten long_far_offset_strikes"
        )
    lines.append("-" * 80)
    lines.append(f"ROUTER restate      : {result.restate_router.get('verdict', 'n/a')}")
    lines.append(f"ALWAYS-RAVEN restate: {result.restate_always_raven.get('verdict', 'n/a')}")
    lines.append(f"ALWAYS-CONDOR restate: {result.restate_always_condor.get('verdict', 'n/a')}")
    lines.append("=" * 80)
    return "\n".join(lines)


# ===========================================================================
# CLI
# ===========================================================================
def main() -> None:  # pragma: no cover — needs the dhan_trading DB
    """Entry point for ``python -m research.backtest.fno_surface_harvester``."""
    parser = argparse.ArgumentParser(
        description="Surface harvester — Skew-Lean Diagonal + RAVEN↔diagonal router",
    )
    parser.add_argument("--mode", default="weekly", choices=["weekly", "expiry_calendar"])
    parser.add_argument("--capital", type=float, default=200_000.0)
    parser.add_argument("--k", type=float, default=0.9, help="v1 VRP threshold inside GATE-V2")
    parser.add_argument("--entry-dte", type=int, default=None, dest="entry_dte")
    parser.add_argument(
        "--diagonal-type", default=DEFAULT_DIAGONAL_OPTION_TYPE, choices=["PE", "CE"],
    )
    parser.add_argument(
        "--short-near-delta", type=float, default=DEFAULT_SHORT_NEAR_DELTA,
    )
    parser.add_argument(
        "--long-far-offset", type=int, default=DEFAULT_LONG_FAR_OFFSET_STRIKES,
        dest="long_far_offset",
    )
    parser.add_argument(
        "--n-configs-tried", type=int, default=1, dest="n_configs_tried",
        help="trials count for the deflated-Sharpe haircut (RAISE this if you swept "
             "diagonal/RAVEN/k/router params and report the best — default 1 = untuned)",
    )
    # Index-agnostic ([[index-agnostic-fno]]): lot/step default to NIFTY but a
    # non-NIFTY underlying drives them here (e.g. BANKNIFTY lot/step).
    parser.add_argument(
        "--lot", type=int, default=NIFTY_LOT,
        help=f"contract lot size (default NIFTY {NIFTY_LOT}; pass per-index for others)",
    )
    parser.add_argument(
        "--step", type=int, default=50,
        help="strike-grid step in index points (default NIFTY 50)",
    )
    args = parser.parse_args()

    from config import get_config  # noqa: PLC0415
    from db import init_db  # noqa: PLC0415

    from research.backtest.fno_condor import cycles_from_db  # noqa: PLC0415

    init_db(get_config().db_url)
    cycles = cycles_from_db(mode=args.mode, entry_dte=args.entry_dte)

    cfg = GateV2Config(k=args.k)
    dp = DiagonalParams(
        option_type=args.diagonal_type,
        short_near_delta=args.short_near_delta,
        long_far_offset_strikes=args.long_far_offset,
    )
    result = run_surface_harvester_backtest(
        cycles, cfg=cfg, diagonal_params=dp,
        capital=args.capital, lot=args.lot, step=args.step,
        n_configs_tried=args.n_configs_tried,
    )
    print(render_report(result))
    print(f"\nCAVEATS: {CAVEATS}")


if __name__ == "__main__":  # pragma: no cover
    main()
