"""
F&O Strategy Orchestrator — regime-aware selection layer.

The **selection layer** that sits ON TOP of ``research/backtest/fno_strategies.py``.
Per cycle, the orchestrator computes the regime (vol-gate state + VRP + DTE + trend +
IV-rank), applies a routing policy to pick exactly ONE defined-risk GO strategy — or to
STAND ASIDE — and dispatches the chosen strategy to the frozen ``fno_strategies`` engine
for construction, pricing, resolution, SPAN and metrics.

Lane discipline (``orchestrator_specs/_CONTEXT.md`` §Lanes): this module **calls**
``fno_strategies`` / ``fno_condor`` / ``fno_vol_gate`` / ``fno_derived``; it NEVER edits
their internals and NEVER re-implements pricing / resolution / cost / SPAN / metrics math.

The one subtlety of composing with the engine (``03_architecture.md`` §4.1, the
"double-gating trap"): ``dispatch`` calls ``run_strategy_backtest(..., gate="none")``.
The orchestrator is the gate authority — it has ALREADY made the regime decision in
``decide()``; letting the engine's internal vol-gate veto a strategy we chose would
double-gate and silently drop cycles. The engine still records the gate label per trade.

Honesty ledger (inherited verbatim from ``fno_strategies`` — never dropped):
VIX-as-weekly-IV proxy · CLOSE-not-FSP settlement · EXPIRY-ONLY (tail-blind) ·
single-σ Black-76 · selection in-sample-optimistic. ROM (return-on-SPAN-margin) is the
headline; the **OOS (last-30%) ROM** is what gates the PRELIMINARY verdict (spec 06 §4.4).
Defined-risk only. PAPER / research only — no live order paths.

Trend + IV-rank wiring (spec 01 §7 option 2): the router needs two trailing histories
per cycle that ``cycles_from_db`` does not carry — VIX/100 closes and index closes
≤ entry_date. ``RegimeSidecar`` runs ONE read-only ``index_bars`` query at run time and,
per cycle, computes a PIT-safe ``iv_rank`` (252-day percentile) + ``trend`` (normalised
20DMA slope). It degrades to ``None`` (→ STAND_ASIDE-safe) when history is short, and is
only touched when a real DB exists. Unit tests drive synthetic ``trend`` / ``iv_rank``
directly through ``regime_from_cycle`` — no DB needed.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from statistics import mean
from typing import Any, Optional, Protocol

# ---------------------------------------------------------------------------
# Reused surface — import, never re-implement (lane discipline)
# ---------------------------------------------------------------------------
from core.fno_derived import implied_move as _implied_move
from ml.fno_vol_gate import (
    BUY_PREMIUM,  # noqa: F401  (re-exported for callers / tests)
    DEFAULT_K,
    SELL_PREMIUM,
    STAND_ASIDE,  # noqa: F401  (re-exported for callers / tests)
    gate_decision,
)
from research.backtest.fno_strategies import (
    FNO_STRATEGIES,
    _max_drawdown,
    _sharpe_from_pnls,
    cycles_from_db,  # re-exported unchanged (the only DB touch, used by cycles_for_index)
    go_no_go,
    run_strategy_backtest,
)
from research.backtest.fno_costs import NIFTY_LOT

# ---------------------------------------------------------------------------
# The GO set (defined-risk, premium-selling) — from _CONTEXT.md / spec 02 §0.
# These are the ONLY strategies the router may emit. Undefined-risk and
# directional/long-premium families are excluded by construction.
# ---------------------------------------------------------------------------
GO_SET: tuple[str, ...] = (
    "iron_condor",        # gated 3.91% GO — neutral workhorse / default
    "bull_put_spread",    # gated 7.19% GO — bullish, far-OTM put credit
    "credit_put_spread",  # gated 2.70% GO — bullish, ATM-er short (aggressive)
    "broken_wing_condor", # gated 2.67% GO — neutral with directional skew
)
DEFAULT_ALLOWED: frozenset[str] = frozenset(GO_SET)

STAND_ASIDE_ACTION = "STAND_ASIDE"

# Trailing-window structural defaults (spec 01 §3b / §4).
IV_RANK_WINDOW = 252      # ~1y of trading days for the IV-rank percentile
IV_RANK_MIN_PERIODS = 60  # below this → iv_rank=None (fail-safe)
TREND_MA_WINDOW = 20      # 20DMA for the spot-vs-MA trend slope


# ---------------------------------------------------------------------------
# Index registry (index-agnostic by construction; NIFTY is the only live row).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IndexParams:
    """Everything index-specific selection or downstream construction needs.

    NIFTY is the only populated entry today. BANKNIFTY/FINNIFTY/etc. are added
    here once their data is ingested (``_CONTEXT.md`` §HARD-REALITY-1). No NIFTY
    constants are hard-coded in orchestrator logic — they all flow from here.
    """

    symbol: str                       # "NIFTY"
    index_security_id: str            # index_bars security_id, e.g. "13" (spec 04)
    vix_id: Optional[str]             # implied-vol proxy security_id, e.g. "21" (None → no vol index)
    lot: int                          # contract lot size (NIFTY_LOT)
    step: int                         # strike grid spacing (50 for NIFTY)
    iv_source: str                    # "vix" | "atm"
    expiry_mode: str                  # "weekly" | "expiry_calendar" (cycles_from_db.mode)
    has_history: bool                 # False → forward-only (no historical option chains)


INDEX_REGISTRY: dict[str, IndexParams] = {
    "NIFTY": IndexParams(
        symbol="NIFTY",
        index_security_id="13",
        vix_id="21",
        lot=NIFTY_LOT,
        step=50,
        iv_source="vix",
        expiry_mode="weekly",
        has_history=True,
    ),
    # BANKNIFTY/FINNIFTY/... : add when ingested; has_history likely False.
}


# ---------------------------------------------------------------------------
# Data models (pure, frozen — no DB)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RegimeSignals:
    """The measured state of one cycle — the orchestrator's input.

    Carries the vol-gate decision + VRP + DTE plus the optional ``iv_rank`` and
    ``trend`` refinements (spec 01). ``trend`` is a signed normalised slope
    (estimator B: ``(spot − SMA20) / SMA20``); ``trend_strength`` is its magnitude
    ``abs(trend)``. Both are ``None`` when the trailing history is too short
    (fail-safe → STAND_ASIDE on trend/rank-dependent regimes).
    """

    entry_date: date
    expiry_date: date
    dte: Optional[int]
    spot: float
    realized_vol: Optional[float]   # cycle["realized_vol_20d"] (annualised fraction)
    implied_vol: Optional[float]    # cycle["straddle_iv"]      (annualised fraction)
    vrp: Optional[float]            # implied_vol - realized_vol (signed VRP edge, vol pts)
    iv_ratio: Optional[float]       # realized_vol / implied_vol (gate ratio; lower = richer IV)
    gate_label: str                 # SELL_PREMIUM | BUY_PREMIUM | STAND_ASIDE
    implied_move: float             # core.fno_derived.implied_move(spot, iv, dte) or 0.0
    # Optional / extensible (filled by the sidecar at run time; None when short):
    iv_rank: Optional[float] = None
    trend: Optional[float] = None

    @property
    def trend_strength(self) -> float:
        """Magnitude of the (signed) trend slope; 0.0 when trend is unknown (spec 02 §2)."""
        return abs(self.trend) if self.trend is not None else 0.0


@dataclass(frozen=True)
class RoutingDecision:
    """The orchestrator's output — a CHOICE, never a Leg.

    ``strategy`` is a registry key in FNO_STRATEGIES (None on stand-aside);
    ``params`` is a builder-param dict forwarded verbatim to the engine.
    """

    entry_date: date
    stand_aside: bool
    strategy: Optional[str]
    params: Optional[dict[str, Any]]
    reason: str
    signals: RegimeSignals


@dataclass(frozen=True)
class RoutingParams:
    """Tunable routing thresholds (spec 02 §6). Defaults are starting points
    seeded from the condor report + builder defaults — NOT validated values."""

    # gate / floors (R0–R3)
    gate_k: float = DEFAULT_K       # passed to gate_decision
    dte_min: int = 1                # avoid expiry-day pin/gamma
    dte_max: int = 7                # weekly window the backtest validated
    vrp_min: float = 0.0            # absolute vol-pt edge floor
    iv_floor: float = 0.0           # min annualised IV for premium selling to pay
    iv_rank_window: int = IV_RANK_WINDOW  # lookback for the iv_rank percentile (spec 01 §3b)
    # trend thresholds (R4–R7) — used only when a trend signal is present
    trend_strong: float = 0.60      # |trend| >= this => directional branch
    trend_skew: float = 0.30        # [skew, strong) => broken_wing
    iv_rank_aggressive: float = 0.70  # uptrend + iv_rank>=this => credit_put_spread
    # per-policy enable set (spec 02 §6) — a disabled name is never returned.
    enabled: frozenset = field(default_factory=lambda: frozenset(GO_SET))
    skew_orientation: str = "toward_trend"  # broken_wing: widen the wing on the trend side
    # per-strategy builder params (forwarded to fno_strategies builders)
    iron_condor_params: dict = field(
        default_factory=lambda: {"move_mult": 1.5, "wing_strikes": 2}
    )
    bull_put_params: dict = field(default_factory=lambda: {"move_mult": 0.5, "width": 2})
    credit_put_params: dict = field(
        default_factory=lambda: {"short_mult": 1.0, "width_strikes": 2}
    )
    broken_wing_params: dict = field(
        default_factory=lambda: {
            "move_mult": 1.5,
            "base_wing": 100.0,
            "skew": 1.5,
            "wing_in_move_units": False,
        }
    )


@dataclass
class OrchestratorResult:
    """Portfolio-level rollup. ``metrics`` is computed with the SAME engine
    helpers (``_sharpe_from_pnls`` / ``_max_drawdown`` / ``go_no_go``) so every
    honesty caveat is inherited — no metrics math is reimplemented here."""

    index: str
    n_cycles: int
    n_traded: int
    n_stand_aside: int
    decisions: list[RoutingDecision]
    trades: list[Any]                  # chosen StrategyTrade objects, chronological
    per_strategy: dict[str, dict]      # strategy name -> deploy count
    metrics: dict[str, Any]            # net_pnl, win_rate, sharpe, ROM, max_dd, go_no_go


# ---------------------------------------------------------------------------
# Routing policy protocol + the MVP regime policy (spec 02)
# ---------------------------------------------------------------------------
class RoutingPolicy(Protocol):
    def select(
        self, signals: RegimeSignals, allowed: frozenset[str]
    ) -> tuple[Optional[str], Optional[dict[str, Any]], str]:
        """(strategy_name_or_None, params_or_None, reason). None => stand aside."""
        ...


class RegimeRoutingPolicy:
    """Transparent ordered-rules policy (spec 02 §3, R0–R7).

    Pure, deterministic, never raises — any degenerate / missing signal resolves
    to STAND_ASIDE (fail-safe). Returns ONLY GO-set members or None; an
    excluded family can never be emitted (closure property, spec 02 §7).

    Disabled-safe (spec 02 §5.4): if a rule's chosen strategy is disabled (not in
    ``allowed`` ∩ ``params.enabled``), the policy falls through to the next rule
    that yields an enabled strategy, then to the neutral default, then STAND_ASIDE.
    """

    def __init__(self, params: Optional[RoutingParams] = None) -> None:
        self.params = params or RoutingParams()

    def _strat(self, name: str, allowed: frozenset[str]) -> Optional[str]:
        """Return name iff it is allowed AND enabled, else None (disabled-safe)."""
        if name in allowed and name in self.params.enabled:
            return name
        return None

    def _broken_wing_params(self, trend: Optional[float]) -> dict[str, Any]:
        """broken_wing builder params with the skew oriented per ``skew_orientation``.

        Default ``skew>1`` widens the PUT wing. With ``skew_orientation="toward_trend"``
        we widen the wing on the TREND side: an uptrend (trend>0) wants more CALL-side
        room, so we invert the skew (skew<1 → wider call wing); a downtrend keeps the
        default put-wide skew. Orientation is applied only when a trend is present.
        """
        params = dict(self.params.broken_wing_params)
        if self.params.skew_orientation == "toward_trend" and trend is not None and trend != 0:
            skew = float(params.get("skew", 1.5))
            if skew > 0 and trend > 0:
                # uptrend → widen the CALL wing (invert the default put-wide skew)
                params["skew"] = 1.0 / skew
            # downtrend → keep the default put-wide skew (skew>1 → wider put wing)
        return params

    def _candidates(
        self, signals: RegimeSignals
    ) -> tuple[list[tuple[str, dict[str, Any], str]], str]:
        """Build the per-regime ORDERED candidate list (spec 02 §3, R4–R7).

        Returns ``(candidates, regime_tag)`` where each candidate is
        ``(strategy_name, builder_params, reason)`` in fall-through order — the
        caller returns the first that is BOTH allowed AND enabled, else
        STAND_ASIDE. A strong DOWNTREND yields ``[iron_condor]`` ONLY (never a
        bullish spread into weakness → stand aside if disabled).
        """
        p = self.params
        s = signals
        trend = s.trend  # None in a short-history regime → neutral default
        strength = s.trend_strength

        # R4 — STRONG DOWNTREND → iron_condor only (never a bullish put spread).
        if trend is not None and trend < 0 and strength >= p.trend_strong:
            return (
                [("iron_condor", dict(p.iron_condor_params), "R4/strong-downtrend->iron_condor")],
                "R4/strong-downtrend",
            )

        # R5 — CLEAR UPTREND → (credit_put if rich IV else bull_put) → broken_wing → iron_condor.
        if trend is not None and trend > 0 and strength >= p.trend_strong:
            cands: list[tuple[str, dict[str, Any], str]] = []
            if s.iv_rank is not None and s.iv_rank >= p.iv_rank_aggressive:
                cands.append(("credit_put_spread", dict(p.credit_put_params),
                              "R5/uptrend+rich-iv->credit_put_spread"))
            else:
                cands.append(("bull_put_spread", dict(p.bull_put_params),
                              "R5/uptrend->bull_put_spread"))
            cands.append(("broken_wing_condor", self._broken_wing_params(trend),
                          "R5/uptrend(spread disabled)->broken_wing_condor"))
            cands.append(("iron_condor", dict(p.iron_condor_params),
                          "R5/uptrend(spreads disabled)->iron_condor"))
            return (cands, "R5/strong-uptrend")

        # R6 — NEUTRAL-WITH-SKEW → broken_wing_condor (skew toward trend) → iron_condor.
        if trend is not None and trend != 0 and p.trend_skew <= strength < p.trend_strong:
            return (
                [
                    ("broken_wing_condor", self._broken_wing_params(trend),
                     "R6/neutral-skew->broken_wing_condor"),
                    ("iron_condor", dict(p.iron_condor_params),
                     "R6/neutral-skew(broken_wing disabled)->iron_condor"),
                ],
                "R6/neutral-skew",
            )

        # R7 — NEUTRAL DEFAULT → iron_condor (the workhorse + catch-all).
        return (
            [("iron_condor", dict(p.iron_condor_params), "R7/neutral-default->iron_condor")],
            "R7/neutral-default",
        )

    def select(
        self, signals: RegimeSignals, allowed: frozenset[str]
    ) -> tuple[Optional[str], Optional[dict[str, Any]], str]:
        p = self.params
        s = signals
        aside = (None, None, "")

        # R0 — GATE MASTER SWITCH
        if s.gate_label != SELL_PREMIUM:
            return (*aside[:2], f"R0/gate={s.gate_label}->STAND_ASIDE")

        # R1 — DTE WINDOW
        if s.dte is None or s.dte < p.dte_min or s.dte > p.dte_max:
            return (*aside[:2], f"R1/dte={s.dte} out of [{p.dte_min},{p.dte_max}]->STAND_ASIDE")

        # R2 — MINIMUM EDGE (absolute VRP floor) — None vrp is a data-quality veto
        if s.vrp is None or s.vrp < p.vrp_min:
            return (*aside[:2], f"R2/vrp={s.vrp} < {p.vrp_min}->STAND_ASIDE")

        # R3 — IV FLOOR — None iv is a data-quality veto
        if s.implied_vol is None or s.implied_vol < p.iv_floor:
            return (*aside[:2], f"R3/iv={s.implied_vol} < {p.iv_floor}->STAND_ASIDE")

        # --- gate GO, DTE in-window, edge & IV clear floors ---
        # Ordered candidate fall-through (spec 02 §3/§5.4): return the FIRST candidate
        # that is BOTH allowed AND enabled, else STAND_ASIDE. This is what makes a
        # disabled strategy fall through to the next within the SAME regime (e.g. a
        # strong uptrend with bull_put/credit_put disabled → broken_wing_condor, NOT a
        # jump to the neutral iron_condor; a strong downtrend's [iron_condor]-only list
        # → STAND_ASIDE if disabled, never a bullish spread).
        candidates, regime_tag = self._candidates(s)
        for name, params, reason in candidates:
            if self._strat(name, allowed):
                return (name, params, reason)
        return (*aside[:2], f"{regime_tag}/no enabled candidate->STAND_ASIDE")


class VrpDefaultPolicy:
    """Trivial placeholder policy (spec 03 §5): SELL_PREMIUM → iron_condor, else
    stand aside. Kept for wiring / sanity tests; the live MVP uses
    ``RegimeRoutingPolicy``."""

    def __init__(self, params: Optional[RoutingParams] = None) -> None:
        self.params = params or RoutingParams()

    def select(
        self, signals: RegimeSignals, allowed: frozenset[str]
    ) -> tuple[Optional[str], Optional[dict[str, Any]], str]:
        if (
            signals.gate_label == SELL_PREMIUM
            and "iron_condor" in allowed
            and "iron_condor" in self.params.enabled
        ):
            return ("iron_condor", dict(self.params.iron_condor_params),
                    "vrp-default/SELL_PREMIUM->iron_condor")
        return (None, None, f"vrp-default/gate={signals.gate_label}->STAND_ASIDE")


# ---------------------------------------------------------------------------
# Pure regime-signal helpers (DB-free, unit-testable — spec 01 §7)
# ---------------------------------------------------------------------------
def iv_rank(iv_today: float, iv_hist: list[float], *, min_periods: int = IV_RANK_MIN_PERIODS) -> Optional[float]:
    """Percentile rank of ``iv_today`` in the trailing window (spec 01 §3b).

    ``iv_hist`` must be VIX/100 closes STRICTLY ≤ entry_date (PIT-safe). Returns
    ``None`` (→ fail-safe stand-aside) when fewer than ``min_periods`` usable
    observations exist. Robust percentile form: fraction of trailing days below today.
    """
    vals = [v for v in iv_hist if v is not None and v > 0]
    if len(vals) < min_periods:
        return None
    below = sum(1 for v in vals if v < iv_today)
    return below / len(vals)


def trend_slope(closes: list[float], *, ma_window: int = TREND_MA_WINDOW) -> Optional[float]:
    """Normalised 20DMA slope (estimator B: ``(spot − SMA20) / SMA20``), PIT-safe (spec 01 §4).

    ``closes`` must be index closes ≤ entry_date (inclusive, no future bars); the
    last element is today's spot. Returns ``None`` when fewer than ``ma_window + 1``
    closes exist (fail-safe) — estimator B needs today's spot to be DISTINCT from
    the ``ma_window``-day MA it is compared against, so a full window PLUS the
    current bar is required. Positive → spot above the 20DMA (uptrend).
    """
    vals = [c for c in closes if c is not None and c > 0]
    if len(vals) < ma_window + 1:
        return None
    ma = mean(vals[-ma_window:])
    if ma <= 0:
        return None
    return (vals[-1] - ma) / ma


# ---------------------------------------------------------------------------
# Free functions (pure, module-level)
# ---------------------------------------------------------------------------
def regime_from_cycle(
    cycle: dict[str, Any],
    *,
    k: float = DEFAULT_K,
    iv_rank_val: Optional[float] = None,
    trend: Optional[float] = None,
) -> RegimeSignals:
    """Build ``RegimeSignals`` from a raw cycle dict (the ``cycles_from_db``
    shape). Calls the real ``gate_decision`` + ``implied_move`` — the regime rule
    is consumed, never re-derived. ``iv_rank_val`` / ``trend`` are injected by the
    caller (the sidecar computes them from trailing windows; the orchestrator
    stays single-cycle-pure). The param is named ``iv_rank_val`` to avoid shadowing
    the module-level ``iv_rank()`` helper (L-SHADOW). Reads ONLY entry-date
    observables — never ``expiry_spot``.

    Data quality: a missing / non-positive ``realized_vol_20d`` or ``straddle_iv``
    is kept as ``None`` (never coerced to a fabricated 0.0 that would masquerade as a
    measured signal). The gate fails open to STAND_ASIDE, and the router's R2/R3
    vetoes stand aside on ``None`` vrp/iv — so a data gap can never silently route
    to an aggressive strategy.
    """
    rv = cycle.get("realized_vol_20d")
    iv = cycle.get("straddle_iv")
    spot = cycle.get("spot")
    dte = cycle.get("dte")

    rv_f = float(rv) if rv is not None and float(rv) > 0 else None
    iv_f = float(iv) if iv is not None and float(iv) > 0 else None
    spot_f = float(spot) if spot is not None else 0.0
    dte_i = int(dte) if dte is not None else None

    gate_label = gate_decision(rv, iv, k=k)
    im = _implied_move(spot, iv, dte) if (spot and iv and dte is not None) else None
    iv_ratio = (rv_f / iv_f) if (rv_f is not None and iv_f is not None) else None
    vrp = (iv_f - rv_f) if (rv_f is not None and iv_f is not None) else None

    return RegimeSignals(
        entry_date=cycle.get("entry_date", date.today()),
        expiry_date=cycle.get("expiry_date", date.today()),
        dte=dte_i,
        spot=spot_f,
        realized_vol=rv_f,
        implied_vol=iv_f,
        vrp=vrp,
        iv_ratio=iv_ratio,
        gate_label=gate_label,
        implied_move=float(im) if im else 0.0,
        iv_rank=iv_rank_val,
        trend=trend,
    )


def cycles_for_index(index: IndexParams) -> list[dict[str, Any]]:
    """Thin DB wrapper (one of two DB touches in this module). Hydrates cycles via
    the re-exported ``cycles_from_db`` for the given index."""
    return cycles_from_db(
        symbol=index.symbol,
        index_id=index.index_security_id,
        vix_id=index.vix_id if index.vix_id is not None else "21",
        mode=index.expiry_mode,
    )


# ---------------------------------------------------------------------------
# Regime sidecar — PIT-safe trailing histories for trend + IV-rank (spec 01 §7 opt 2)
# ---------------------------------------------------------------------------
class RegimeSidecar:
    """Self-contained read-only ``index_bars`` reader that supplies per-cycle
    ``trend`` (20DMA slope of index closes) + ``iv_rank`` (252d VIX/100 percentile),
    both strictly ≤ ``entry_date`` (no look-ahead).

    One DB query at construction loads two date→close maps; ``signals_for(cycle)``
    slices each map ≤ the cycle's ``entry_date`` and calls the pure helpers above.
    Fail-open: if the DB / query is unavailable, both signals degrade to ``None``
    (→ fail-safe stand-aside on trend/rank-dependent regimes). The pure path (unit
    tests) never instantiates this — it injects synthetic signals directly.
    """

    def __init__(self, index: IndexParams, *, k: float = DEFAULT_K) -> None:
        self.index = index
        self.k = k
        self._index_closes: list[tuple[date, float]] = []
        self._vix_iv: list[tuple[date, float]] = []
        self._load()

    def _load(self) -> None:
        """Load index closes + VIX/100 closes once (fail-open to empty maps)."""
        try:
            from sqlalchemy import text

            from db import get_session

            sql = text("""
                SELECT (time AT TIME ZONE 'UTC')::date AS d, security_id, close
                FROM index_bars
                WHERE security_id IN (:nid, :vid)
                  AND timeframe = '1d'
                  AND close IS NOT NULL
                ORDER BY (time AT TIME ZONE 'UTC')::date
            """)
            vid = self.index.vix_id if self.index.vix_id is not None else "21"
            with get_session() as session:
                rows = session.execute(
                    sql, {"nid": self.index.index_security_id, "vid": vid}
                ).fetchall()
            index_closes: list[tuple[date, float]] = []
            vix: list[tuple[date, float]] = []
            for d, sid, close in rows:
                try:
                    c = float(close)
                except (TypeError, ValueError):
                    continue
                if c <= 0:
                    continue
                if str(sid) == str(self.index.index_security_id):
                    index_closes.append((d, c))
                elif str(sid) == str(vid):
                    vix.append((d, c / 100.0))  # VIX is quoted in pct → fraction
            self._index_closes = index_closes
            self._vix_iv = vix
        except Exception:  # noqa: BLE001 — sidecar is fail-open (spec 06 §5 posture)
            self._index_closes = []
            self._vix_iv = []

    def signals_for(self, cycle: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
        """Return (iv_rank, trend) for the cycle's entry_date, PIT-safe (≤ entry).

        Degrades to (None, None) when histories are short or the sidecar is empty.
        """
        entry = cycle.get("entry_date")
        if entry is None or not self._index_closes:
            return (None, None)
        closes = [c for d, c in self._index_closes if d <= entry]
        # Cap the IV-rank history to the trailing IV_RANK_WINDOW (~1y) so the
        # percentile is a ROLLING rank, not an ever-growing all-time one (M-IVW).
        iv_hist = [v for d, v in self._vix_iv if d <= entry][-IV_RANK_WINDOW:]
        iv_today = cycle.get("straddle_iv")
        rank = (
            iv_rank(float(iv_today), iv_hist)
            if (iv_today is not None and float(iv_today) > 0)
            else None
        )
        tr = trend_slope(closes)
        return (rank, tr)


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------
class FnoOrchestrator:
    """Regime-aware strategy router. Selection only — never builds / prices legs.

    Composes a ``RoutingPolicy`` (which strategy?) + an ``IndexParams`` + the
    existing vol-gate, and dispatches the chosen strategy to
    ``fno_strategies.run_strategy_backtest`` (one position per index per cycle).
    """

    def __init__(
        self,
        policy: RoutingPolicy,
        *,
        index: IndexParams,
        k: float = DEFAULT_K,
        capital: float = 200_000.0,
        slip_pct: float = 0.005,
        allowed_strategies: Optional[frozenset[str]] = None,
        gate: str = "vol",
        sidecar: Optional[RegimeSidecar] = None,
    ) -> None:
        self.policy = policy
        self.index = index
        self.k = k
        self.capital = capital
        self.slip_pct = slip_pct
        self.gate = gate
        self.sidecar = sidecar
        self.allowed = allowed_strategies if allowed_strategies is not None else DEFAULT_ALLOWED

        # Whitelist guard (spec 03 §7 / 02 §7 closure): every allowed name must
        # exist in the registry AND be defined-risk. Even a buggy policy cannot
        # route to an unbounded-loss structure.
        for name in self.allowed:
            if name not in FNO_STRATEGIES:
                raise ValueError(f"allowed strategy {name!r} not in FNO_STRATEGIES")
            if not FNO_STRATEGIES[name].defined_risk:
                raise ValueError(
                    f"allowed strategy {name!r} is undefined-risk — refused "
                    "(orchestrator is defined-risk only)"
                )

    # ---- pure selection (no DB, no engine) --------------------------------
    def decide(self, signals: RegimeSignals) -> RoutingDecision:
        """Apply the policy to ONE cycle's regime → a RoutingDecision.

        Deterministic, side-effect-free, DB-free. Under ``gate="none"`` the
        vol-gate is NOT a master switch — the router deploys the neutral default
        (or its trend-refined pick) on every cycle, matching the engine's
        ungated A/B baseline (the gate decision is still recorded per trade).
        """
        sig = signals
        if self.gate == "none" and sig.gate_label != SELL_PREMIUM:
            # Ungated baseline: treat the cycle as deployable, but keep the real
            # gate_label on the recorded signals for provenance. Force the policy
            # past R0 by presenting a SELL_PREMIUM view for selection only.
            sig_for_policy = RegimeSignals(
                entry_date=sig.entry_date, expiry_date=sig.expiry_date, dte=sig.dte,
                spot=sig.spot, realized_vol=sig.realized_vol, implied_vol=sig.implied_vol,
                vrp=sig.vrp, iv_ratio=sig.iv_ratio, gate_label=SELL_PREMIUM,
                implied_move=sig.implied_move, iv_rank=sig.iv_rank, trend=sig.trend,
            )
        else:
            sig_for_policy = sig
        strategy, params, reason = self.policy.select(sig_for_policy, self.allowed)
        if strategy is None:
            return RoutingDecision(
                entry_date=signals.entry_date,
                stand_aside=True,
                strategy=None,
                params=None,
                reason=reason or "STAND_ASIDE",
                signals=signals,
            )
        # Defence in depth: the policy must never return a non-allowed name.
        if strategy not in self.allowed:
            raise ValueError(
                f"policy returned non-allowed strategy {strategy!r}; allowed={sorted(self.allowed)}"
            )
        return RoutingDecision(
            entry_date=signals.entry_date,
            stand_aside=False,
            strategy=strategy,
            params=params,
            reason=reason,
            signals=signals,
        )

    # ---- dispatch one chosen strategy to the engine -----------------------
    def dispatch(
        self, decision: RoutingDecision, cycle: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Stand-aside → None. Otherwise run the chosen strategy over the SINGLE
        cycle with ``gate="none"`` (the orchestrator is the gate authority —
        never let the engine's internal gate veto a chosen strategy, which would
        double-gate). Returns the engine's metrics dict for this one cycle."""
        if decision.stand_aside or decision.strategy is None:
            return None
        spec = FNO_STRATEGIES[decision.strategy]
        return run_strategy_backtest(
            spec,
            [cycle],
            decision.params,
            k=self.k,
            capital=self.capital,
            lot=self.index.lot,
            step=self.index.step,
            slip_pct=self.slip_pct,
            gate="none",  # CRITICAL — orchestrator already gated in decide()
        )

    # ---- full backtest loop -----------------------------------------------
    def run(self, cycles: list[dict[str, Any]]) -> OrchestratorResult:
        """Per cycle: regime_from_cycle (+ sidecar trend/iv_rank) → decide →
        dispatch (or skip). One decision + at most one position per cycle
        (one-position discipline, spec 03 §6). Aggregates chosen trades via the
        engine's own helpers."""
        decisions: list[RoutingDecision] = []
        chosen_trades: list[Any] = []
        per_strategy: dict[str, dict] = {}

        for cycle in cycles:
            rank, trend = (None, None)
            if self.sidecar is not None:
                rank, trend = self.sidecar.signals_for(cycle)
            signals = regime_from_cycle(cycle, k=self.k, iv_rank_val=rank, trend=trend)
            decision = self.decide(signals)
            decisions.append(decision)
            if decision.stand_aside:
                continue
            result = self.dispatch(decision, cycle)
            assert decision.strategy is not None  # not stand-aside
            per_strategy.setdefault(decision.strategy, {"deployed": 0, "trades": 0})
            per_strategy[decision.strategy]["deployed"] += 1
            if result:
                trades = result.get("trades", [])
                chosen_trades.extend(trades)
                per_strategy[decision.strategy]["trades"] += len(trades)

        # Sort chronologically before any IS/OOS split (mirrors the engine).
        chosen_trades.sort(key=lambda t: t.entry_date)
        n_aside = sum(1 for d in decisions if d.stand_aside)
        n_traded = len(decisions) - n_aside
        metrics = self._aggregate(chosen_trades, n_cycles=len(cycles), n_traded=n_traded)

        return OrchestratorResult(
            index=self.index.symbol,
            n_cycles=len(cycles),
            n_traded=len(decisions) - n_aside,
            n_stand_aside=n_aside,
            decisions=decisions,
            trades=chosen_trades,
            per_strategy=per_strategy,
            metrics=metrics,
        )

    # ---- §8 aggregation — reuse engine helpers, reimplement nothing -------
    def _aggregate(
        self, trades: list[Any], *, n_cycles: int, n_traded: int
    ) -> dict[str, Any]:
        """Portfolio rollup using the SAME math ``run_strategy_backtest`` uses —
        ``_sharpe_from_pnls`` / ``_max_drawdown`` / ``go_no_go`` are imported,
        never copied. ROM = Σnet_pnl / Σspan; ROM_oos = the OOS-slice ROM (same
        70/30 chronological split as the engine), which gates the verdict.

        ``participation_rate`` = n_traded / n_cycles — where ``n_traded`` is the
        count of DEPLOYED cycles (decisions that were not stand-aside), NOT the
        number of filled trades (M-PART). ``rom_deployment_norm`` (net_pnl /
        (Σspan / participation_rate)) makes the stand-aside / idle-capital cost
        explicit so the comparison vs an always-deployed single is honest
        (spec-flagged M4)."""
        n_trades = len(trades)
        participation_rate = (n_traded / n_cycles) if n_cycles > 0 else 0.0
        if n_trades == 0:
            metrics: dict[str, Any] = {
                "strategy": "ORCHESTRATED",
                "gate": self.gate,
                "trades": [],
                "n_cycles": n_cycles,
                "n_trades": 0,
                "participation_rate": participation_rate,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "sharpe": 0.0,
                "sharpe_is": 0.0,
                "sharpe_oos": 0.0,
                "max_drawdown": 0.0,
                "net_pnl": 0.0,
                "return_on_capital": 0.0,
                "return_on_margin": 0.0,
                "rom_oos": 0.0,
                "rom_deployment_norm": 0.0,
                "mean_span": 0.0,
                "split_idx": 0,
            }
            metrics["go_no_go"] = go_no_go(metrics, capital=self.capital)
            return metrics

        pnls = [t.net_pnl for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        win_rate = len(wins) / n_trades
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")

        sharpe = _sharpe_from_pnls(pnls)
        split_idx = max(1, int(0.7 * n_trades))
        pnls_is = pnls[:split_idx]
        pnls_oos = pnls[split_idx:]
        sharpe_is = _sharpe_from_pnls(pnls_is) if len(pnls_is) >= 2 else 0.0
        sharpe_oos = _sharpe_from_pnls(pnls_oos) if len(pnls_oos) >= 2 else 0.0

        max_dd = _max_drawdown(pnls)
        net_pnl = sum(pnls)
        spans = [t.span for t in trades]
        total_span = sum(spans)
        return_on_margin = net_pnl / total_span if total_span > 0 else 0.0
        mean_span = total_span / n_trades

        rom_oos = _rom_oos_from_trades(trades)

        # Deployment-normalised ROM: penalises idle/stand-aside capital so an
        # orchestrator that trades few cycles cannot inflate ROM vs always-deployed.
        rom_deployment_norm = (
            net_pnl / (total_span / participation_rate)
            if (total_span > 0 and participation_rate > 0)
            else 0.0
        )

        metrics = {
            "strategy": "ORCHESTRATED",
            "gate": self.gate,
            "trades": trades,
            "n_cycles": n_cycles,
            "n_trades": n_trades,
            "participation_rate": participation_rate,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "sharpe": sharpe,
            "sharpe_is": sharpe_is,
            "sharpe_oos": sharpe_oos,
            "max_drawdown": max_dd,
            "net_pnl": net_pnl,
            "return_on_capital": net_pnl / self.capital,
            "return_on_margin": return_on_margin,
            "rom_oos": rom_oos,
            "rom_deployment_norm": rom_deployment_norm,
            "mean_span": mean_span,
            "split_idx": split_idx,
        }
        metrics["go_no_go"] = go_no_go(metrics, capital=self.capital)
        return metrics


# ---------------------------------------------------------------------------
# OOS ROM helper — the same 70/30 chronological split the engine uses (spec 06 §4.4)
# ---------------------------------------------------------------------------
def _rom_oos_from_trades(trades: list[Any]) -> float:
    """OOS (last-30%) ROM = Σ net_pnl(OOS) / Σ span(OOS) over the chronological
    70/30 split — exactly how the engine splits for ``sharpe_oos``. Trades are
    sorted by entry_date first so a single-strategy row computes the SAME OOS
    slice the engine would. Returns 0.0 when the OOS span is zero."""
    if not trades:
        return 0.0
    ordered = sorted(trades, key=lambda t: t.entry_date)
    n = len(ordered)
    split_idx = max(1, int(0.7 * n))
    oos = ordered[split_idx:]
    total_span_oos = sum(t.span for t in oos)
    net_oos = sum(t.net_pnl for t in oos)
    return net_oos / total_span_oos if total_span_oos > 0 else 0.0


# ---------------------------------------------------------------------------
# CLI / backtest entrypoint (spec 06 §1/§4) — DB-init only after arg-parse.
# ---------------------------------------------------------------------------
CAVEATS = (
    "VIX-as-weekly-IV proxy · CLOSE-not-FSP settlement · "
    "EXPIRY-ONLY (tail-blind) · single-σ B76 · "
    "selection in-sample-optimistic → read ROM_oos · PRELIMINARY · PAPER only"
)


def _fmt_pct(x: float) -> str:
    return f"{x:>8.2%}"


def run_comparison(
    cycles: list[dict[str, Any]],
    *,
    index: IndexParams,
    k: float = DEFAULT_K,
    capital: float = 200_000.0,
    slip_pct: float = 0.005,
    policy: Optional[RoutingPolicy] = None,
    gate: str = "vol",
    allowed_strategies: Optional[frozenset[str]] = None,
    extra_params: Optional[dict[str, Any]] = None,
    sidecar: Optional[RegimeSidecar] = None,
) -> dict[str, Any]:
    """ORCHESTRATED vs each single GO strategy + best_single, for ONE gate mode.

    The orchestrated row uses the router (``gate="none"`` internally — the router
    IS the gate). Single rows are run with the SAME ``gate`` mode + ``slip_pct``
    so the A/B is apples-to-apples. Each single's ``rom_oos`` is computed on the
    SAME 70/30 trade split the engine uses, and ``best_single`` is picked by
    **OOS ROM** (the gating metric, spec 06 §4.4), not full-sample ROM.

    Returns a dict with ``orchestrated``, ``orchestrated_result``, ``singles``
    (name->metrics, each carrying ``rom_oos``), ``best_single`` (name), and ``gate``.
    """
    policy = policy or RegimeRoutingPolicy(RoutingParams(gate_k=k))
    allowed = allowed_strategies if allowed_strategies is not None else DEFAULT_ALLOWED
    extra = dict(extra_params or {})
    orch = FnoOrchestrator(
        policy, index=index, k=k, capital=capital, slip_pct=slip_pct,
        allowed_strategies=allowed, gate=gate, sidecar=sidecar,
    )
    orch_result = orch.run(cycles)

    singles: dict[str, dict[str, Any]] = {}
    for name in sorted(allowed):
        m = run_strategy_backtest(
            FNO_STRATEGIES[name],
            cycles,
            extra,
            k=k,
            capital=capital,
            lot=index.lot,
            step=index.step,
            slip_pct=slip_pct,
            gate=gate,
        )
        # Engine singles don't carry rom_oos — compute it on the SAME 70/30 split.
        m["rom_oos"] = _rom_oos_from_trades(m.get("trades", []))
        singles[name] = m

    # best_single by OOS ROM — the gating metric (spec 06 §4.4), not full-sample ROM.
    best_name = max(singles, key=lambda n: singles[n]["rom_oos"]) if singles else ""
    return {
        "orchestrated": orch_result.metrics,
        "orchestrated_result": orch_result,
        "singles": singles,
        "best_single": best_name,
        "gate": gate,
    }


def _print_comparison(
    cmp: dict[str, Any], *, index: IndexParams, k: float, capital: float,
    policy_id: str = "regime",
) -> None:
    singles = cmp["singles"]
    orch = cmp["orchestrated"]
    orch_result: OrchestratorResult = cmp["orchestrated_result"]
    best_name = cmp["best_single"]
    gate = cmp.get("gate", "vol")

    print(
        f"ORCHESTRATOR BACKTEST — index={index.symbol} router={policy_id} "
        f"mode={index.expiry_mode} gate={gate} k={k:.2f} capital=₹{capital:,.0f}"
    )
    print(f"caveats: {CAVEATS}")
    print(f"n_cycles={orch['n_cycles']}\n")

    hdr = (
        f"{'row':<20} {'gate':>5} {'n':>4} {'ncyc':>5} {'net':>12} "
        f"{'ROM':>8} {'ROM_oos':>8} {'win':>7} {'sharpe':>7} {'sh_oos':>7} {'GO':>6}"
    )
    print(hdr)
    print("-" * len(hdr))

    def _row(label: str, m: dict[str, Any], row_gate: str) -> None:
        go = "GO" if m["go_no_go"][0] else "NO-GO"
        rom_oos = m.get("rom_oos")
        rom_oos_s = _fmt_pct(rom_oos) if rom_oos is not None else f"{'-':>8}"
        print(
            f"{label:<20} {row_gate:>5} {m['n_trades']:>4} {m.get('n_cycles', 0):>5} "
            f"{m['net_pnl']:>12,.0f} {_fmt_pct(m['return_on_margin'])} {rom_oos_s} "
            f"{m['win_rate']:>6.1%} {m['sharpe']:>7.2f} {m.get('sharpe_oos', 0.0):>7.2f} {go:>6}"
        )

    for name in sorted(singles):
        _row(name, singles[name], gate)
    print("-" * len(hdr))
    _row("ORCHESTRATED", orch, gate)

    picks = " ".join(
        f"{n}:{d['deployed']}" for n, d in sorted(orch_result.per_strategy.items())
    )
    print(
        f"\npicks: {picks or '(none)'}  aside:{orch_result.n_stand_aside}  "
        f"traded:{orch_result.n_traded}  "
        f"participation:{orch.get('participation_rate', 0.0):.1%}"
    )
    # Surface the deployment-normalised ROM (idle/stand-aside-penalised) so the
    # always-deployed-single comparison is honest (M-NORM).
    print(
        f"rom_deployment_norm:{_fmt_pct(orch.get('rom_deployment_norm', 0.0))}"
        f"  (vs ROM {_fmt_pct(orch.get('return_on_margin', 0.0))} — idle-capital penalised)"
    )

    # best_single summary line (clearly labelled — NOT a duplicate table row).
    if best_name:
        best = singles[best_name]
        print(
            f"best_single = {best_name}: "
            f"ROM {best['return_on_margin']:.2%}  ROM_oos {best['rom_oos']:.2%}  "
            f"sharpe_oos {best.get('sharpe_oos', 0.0):.2f}"
        )


def _print_verdict(
    cmp_vol: dict[str, Any], cmp_none: Optional[dict[str, Any]] = None
) -> None:
    """VERDICT on OOS numbers (spec 06 §4.4): orchestrated@vol.ROM_oos vs
    best_single@vol.ROM_oos (the headline), plus the gate A/B if available."""
    orch = cmp_vol["orchestrated"]
    singles = cmp_vol["singles"]
    best_name = cmp_vol["best_single"]
    o_rom_oos = orch.get("rom_oos", 0.0)
    o_sharpe_oos = orch.get("sharpe_oos", 0.0)

    if best_name:
        best = singles[best_name]
        b_rom_oos = best.get("rom_oos", 0.0)
        b_sharpe_oos = best.get("sharpe_oos", 0.0)
        adds_edge = (
            o_rom_oos > b_rom_oos
            and o_sharpe_oos > b_sharpe_oos
            and orch["go_no_go"][0]
        )
        print(
            f"\nVERDICT (PRELIMINARY): ORCHESTRATED ROM_oos {o_rom_oos:.2%} vs "
            f"best_single({best_name}) ROM_oos {b_rom_oos:.2%} "
            f"(Δ {(o_rom_oos - b_rom_oos) * 100:+.2f}pp) — "
            f"{'selection adds edge' if adds_edge else 'no clear selection edge'}. "
            "Real-IV forward paper-log is the truth test. PAPER only."
        )
    else:
        print("\nVERDICT (PRELIMINARY): no single-strategy baseline available.")

    if cmp_none is not None:
        n_rom_oos = cmp_none["orchestrated"].get("rom_oos", 0.0)
        print(
            f"         gate A/B: orchestrated@vol ROM_oos {o_rom_oos:.2%} vs "
            f"orchestrated@none ROM_oos {n_rom_oos:.2%} → "
            f"{'vol-gate adds edge inside the router' if o_rom_oos > n_rom_oos else 'gate does not help the router'}."
        )

    print(f"\nORCHESTRATED go_no_go: {orch['go_no_go'][1]}")


# ---------------------------------------------------------------------------
# S3 archival (spec 06 §5) — fail-open, bucket from env (mirrors finetune/kronos).
# ---------------------------------------------------------------------------
def _result_to_json(cmp: dict[str, Any]) -> dict[str, Any]:
    """Strip the non-serialisable StrategyTrade objects → a JSON-safe row dict."""
    def _clean(m: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in m.items() if k != "trades"}
        gng = out.get("go_no_go")
        if isinstance(gng, tuple):
            out["go_no_go"] = [gng[0], gng[1]]
        if out.get("profit_factor") == float("inf"):
            out["profit_factor"] = None  # JSON has no Infinity
        return out

    orch_result: OrchestratorResult = cmp["orchestrated_result"]
    return {
        "gate": cmp.get("gate", "vol"),
        "orchestrated": _clean(cmp["orchestrated"]),
        "singles": {n: _clean(m) for n, m in cmp["singles"].items()},
        "best_single": cmp["best_single"],
        "picks": {n: d["deployed"] for n, d in orch_result.per_strategy.items()},
        "n_stand_aside": orch_result.n_stand_aside,
        "n_traded": orch_result.n_traded,
    }


def archive_s3(
    comparisons: dict[str, dict[str, Any]],
    *,
    index: IndexParams,
    policy_id: str,
    args: dict[str, Any],
    summary_text: str,
) -> Optional[str]:  # pragma: no cover — needs network/boto3
    """Write results.json + summary.txt + manifest.json under
    ``s3://<S3_BUCKET>/kronos/m3/orchestrator/index=<I>/router=<R>/<ts>/``.

    Fail-open: a missing bucket or any S3 error logs + warns but never crashes the
    run (the stdout table is the primary deliverable). Mirrors the kronos gate /
    finetune ``--upload-s3`` posture; bucket resolves from the ``S3_BUCKET`` env
    var (NOT hardcoded)."""
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        print("WARNING: --upload-s3 set but S3_BUCKET is empty — skipping upload.")
        return None
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    prefix = f"kronos/m3/orchestrator/index={index.symbol}/router={policy_id}/{run_ts}"
    results = {gate: _result_to_json(cmp) for gate, cmp in comparisons.items()}
    manifest = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "args": args,
        "caveats": CAVEATS.split(" · "),
        "decision": "PRELIMINARY",
    }
    try:
        import boto3

        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=bucket, Key=f"{prefix}/results.json",
            Body=json.dumps(results, indent=2, default=str).encode(),
        )
        s3.put_object(Bucket=bucket, Key=f"{prefix}/summary.txt", Body=summary_text.encode())
        s3.put_object(
            Bucket=bucket, Key=f"{prefix}/manifest.json",
            Body=json.dumps(manifest, indent=2, default=str).encode(),
        )
        uri = f"s3://{bucket}/{prefix}/"
        print(f"Archived orchestrator results to {uri}")
        return uri
    except Exception as exc:  # noqa: BLE001 — fail-open
        print(f"WARNING: S3 archival failed ({exc}) — results NOT uploaded.")
        return None


def main() -> None:  # pragma: no cover
    """``python -m research.backtest.fno_orchestrator --index NIFTY --gate both``."""
    parser = argparse.ArgumentParser(
        description="F&O Strategy Orchestrator — regime router + index backtest",
    )
    parser.add_argument("--index", default="NIFTY", choices=list(INDEX_REGISTRY))
    parser.add_argument("--mode", default=None, choices=["weekly", "expiry_calendar"],
                        help="cycle-boundary mode (default: the index registry's expiry_mode)")
    parser.add_argument("--gate", default="both", choices=["vol", "none", "both"],
                        help="vol = gated · none = ungated · both = print both rows (default)")
    parser.add_argument("--policy", default="regime", choices=["regime", "vrp_default"])
    parser.add_argument("--strategies", default="all",
                        help="GO-set subset to route among: 'all' or a CSV of GO names")
    parser.add_argument("--k", type=float, default=DEFAULT_K)
    parser.add_argument("--capital", type=float, default=200_000.0)
    parser.add_argument("--span-pct", type=float, default=None)
    parser.add_argument("--slip-pct", type=float, default=0.005)
    parser.add_argument("--param", action="append", metavar="KEY=VAL", default=[],
                        help="extra builder params key=val (repeatable; float-if-possible)")
    parser.add_argument("--upload-s3", action="store_true", dest="upload_s3",
                        help="archive results.json + summary.txt to s3://$S3_BUCKET/kronos/m3/orchestrator/")
    args = parser.parse_args()

    index = INDEX_REGISTRY[args.index]
    if not index.has_history:
        raise NotImplementedError(
            f"No historical data for {args.index} yet — ingest its index/VIX bars first "
            "before running the orchestrator backtest (NIFTY is the only ready index today)."
        )
    if args.mode is not None and args.mode != index.expiry_mode:
        index = IndexParams(
            symbol=index.symbol, index_security_id=index.index_security_id,
            vix_id=index.vix_id, lot=index.lot, step=index.step,
            iv_source=index.iv_source, expiry_mode=args.mode, has_history=index.has_history,
        )

    # allowed-strategies subset (GO set only; defined-risk guard runs in the ctor).
    if args.strategies.strip().lower() == "all":
        allowed = DEFAULT_ALLOWED
    else:
        names = [s.strip() for s in args.strategies.split(",") if s.strip()]
        allowed = frozenset(names)

    extra: dict[str, Any] = {}
    if args.span_pct is not None:
        extra["span_pct"] = args.span_pct
    for kv in args.param:
        kk, _, vv = kv.partition("=")
        try:
            extra[kk.strip()] = float(vv)
        except ValueError:
            extra[kk.strip()] = vv

    # DB-init only after arg-parse so --help never touches it (mirrors fno_strategies.main).
    from config import get_config
    from db import init_db

    init_db(get_config().db_url)
    cycles = cycles_for_index(index)
    sidecar = RegimeSidecar(index, k=args.k)

    def _policy() -> RoutingPolicy:
        rp = RoutingParams(gate_k=args.k, enabled=frozenset(allowed))
        return VrpDefaultPolicy(rp) if args.policy == "vrp_default" else RegimeRoutingPolicy(rp)

    gate_modes = ["vol", "none"] if args.gate == "both" else [args.gate]
    comparisons: dict[str, dict[str, Any]] = {}
    for gm in gate_modes:
        cmp = run_comparison(
            cycles, index=index, k=args.k, capital=args.capital, slip_pct=args.slip_pct,
            policy=_policy(), gate=gm, allowed_strategies=allowed, extra_params=extra,
            sidecar=sidecar,
        )
        comparisons[gm] = cmp

    # Render the comparison + verdict ONCE, capturing the exact stdout text into a
    # buffer so the S3 summary.txt is the REAL report, not a "(see stdout)" stub
    # (L-SUM). Tee: print to the console AND keep the lines for archival.
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for gm in gate_modes:
            _print_comparison(
                comparisons[gm], index=index, k=args.k, capital=args.capital,
                policy_id=args.policy,
            )
            print()
        _print_verdict(
            comparisons.get("vol", comparisons[gate_modes[0]]), comparisons.get("none")
        )
    summary_text = buf.getvalue()
    print(summary_text, end="")

    if args.upload_s3:
        archive_s3(
            comparisons, index=index, policy_id=args.policy,
            args=vars(args), summary_text=summary_text or "(see stdout)",
        )


if __name__ == "__main__":  # pragma: no cover
    main()
