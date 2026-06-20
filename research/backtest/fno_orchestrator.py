"""
F&O Strategy Orchestrator — MVP (PR-1).

The **selection layer** that sits ON TOP of ``research/backtest/fno_strategies.py``.
Per cycle, the orchestrator computes the regime (vol-gate state + VRP + DTE), applies a
routing policy to pick exactly ONE defined-risk GO strategy — or to STAND ASIDE — and
dispatches the chosen strategy to the frozen ``fno_strategies`` engine for construction,
pricing, resolution, SPAN and metrics.

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
headline. Defined-risk only. PAPER / research only — no live order paths.

MVP scope (thin versions of specs 01/02/03/06). Explicit debt with follow-up PRs:
  * NIFTY lot/step constants flow via ``IndexParams`` (full registry → PR-2).
  * Thin 2-field regime (gate + DTE) → full feature vector in PR-3.
  * The four-way routing policy from ``02_routing_policy.md`` is implemented here as
    ``RegimeRoutingPolicy``; the full IV-rank/trend refinements arrive with PR-3/PR-4.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date
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

    symbol: str            # "NIFTY"
    nifty_id: str          # index_bars security_id, e.g. "13"
    vix_id: Optional[str]  # implied-vol proxy security_id, e.g. "21"
    lot: int               # contract lot size (NIFTY_LOT)
    step: int              # strike grid spacing (50 for NIFTY)
    iv_source: str         # "vix" | "atm"
    expiry_mode: str       # "weekly" | "expiry_calendar" (cycles_from_db.mode)
    has_history: bool      # False → forward-only (no historical option chains)


INDEX_REGISTRY: dict[str, IndexParams] = {
    "NIFTY": IndexParams(
        symbol="NIFTY",
        nifty_id="13",
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

    MVP carries the vol-gate decision + VRP + DTE (spec 01 thin). ``iv_rank`` /
    ``trend`` are optional extension points (PR-3); a policy may ignore them.
    """

    entry_date: date
    expiry_date: date
    dte: int
    spot: float
    realized_vol: float        # cycle["realized_vol_20d"] (annualised fraction)
    implied_vol: float         # cycle["straddle_iv"]      (annualised fraction)
    vrp: float                 # implied_vol - realized_vol (signed VRP edge, vol pts)
    iv_ratio: float            # realized_vol / implied_vol (gate ratio; lower = richer IV)
    gate_label: str            # SELL_PREMIUM | BUY_PREMIUM | STAND_ASIDE
    implied_move: float        # core.fno_derived.implied_move(spot, iv, dte) or 0.0
    # Optional / extensible (filled if available; MVP policy ignores):
    iv_rank: Optional[float] = None
    trend: Optional[float] = None


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
    # trend thresholds (R4–R7) — used only when a trend signal is present
    trend_strong: float = 0.60      # |trend| >= this => directional branch
    trend_skew: float = 0.30        # [skew, strong) => broken_wing
    iv_rank_aggressive: float = 0.70  # uptrend + iv_rank>=this => credit_put_spread
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
    """

    def __init__(self, params: Optional[RoutingParams] = None) -> None:
        self.params = params or RoutingParams()

    def _strat(
        self, name: str, allowed: frozenset[str]
    ) -> Optional[str]:
        """Return name if it is allowed + enabled, else None (disabled-safe)."""
        return name if name in allowed else None

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

        # R2 — MINIMUM EDGE (absolute VRP floor)
        if s.vrp < p.vrp_min:
            return (*aside[:2], f"R2/vrp={s.vrp:.4f} < {p.vrp_min}->STAND_ASIDE")

        # R3 — IV FLOOR
        if s.implied_vol < p.iv_floor:
            return (*aside[:2], f"R3/iv={s.implied_vol:.4f} < {p.iv_floor}->STAND_ASIDE")

        # --- gate GO, DTE in-window, edge & IV clear floors ---
        trend = s.trend  # None in the MVP regime → falls through to the neutral default
        strength = abs(trend) if trend is not None else 0.0

        # R4 — STRONG DOWNTREND → iron_condor (never a bullish put spread)
        if trend is not None and trend < 0 and strength >= p.trend_strong:
            ic = self._strat("iron_condor", allowed)
            if ic:
                return (ic, dict(p.iron_condor_params), "R4/strong-downtrend->iron_condor")
            return (*aside[:2], "R4/strong-downtrend, iron_condor disabled->STAND_ASIDE")

        # R5 — CLEAR UPTREND → bull_put (or credit_put when IV rank is rich)
        if trend is not None and trend > 0 and strength >= p.trend_strong:
            if s.iv_rank is not None and s.iv_rank >= p.iv_rank_aggressive:
                cps = self._strat("credit_put_spread", allowed)
                if cps:
                    return (cps, dict(p.credit_put_params),
                            "R5/uptrend+rich-iv->credit_put_spread")
            bps = self._strat("bull_put_spread", allowed)
            if bps:
                return (bps, dict(p.bull_put_params), "R5/uptrend->bull_put_spread")
            # bull_put disabled — fall through to the safe default condor below.

        # R6 — NEUTRAL-WITH-SKEW → broken_wing_condor
        if trend is not None and trend != 0 and p.trend_skew <= strength < p.trend_strong:
            bwc = self._strat("broken_wing_condor", allowed)
            if bwc:
                return (bwc, dict(p.broken_wing_params), "R6/neutral-skew->broken_wing_condor")

        # R7 — NEUTRAL DEFAULT → iron_condor (the workhorse + catch-all)
        ic = self._strat("iron_condor", allowed)
        if ic:
            return (ic, dict(p.iron_condor_params), "R7/neutral-default->iron_condor")
        return (*aside[:2], "R7/no enabled strategy->STAND_ASIDE")


class VrpDefaultPolicy:
    """Trivial placeholder policy (spec 03 §5): SELL_PREMIUM → iron_condor, else
    stand aside. Kept for wiring / sanity tests; the live MVP uses
    ``RegimeRoutingPolicy``."""

    def __init__(self, params: Optional[RoutingParams] = None) -> None:
        self.params = params or RoutingParams()

    def select(
        self, signals: RegimeSignals, allowed: frozenset[str]
    ) -> tuple[Optional[str], Optional[dict[str, Any]], str]:
        if signals.gate_label == SELL_PREMIUM and "iron_condor" in allowed:
            return ("iron_condor", dict(self.params.iron_condor_params),
                    "vrp-default/SELL_PREMIUM->iron_condor")
        return (None, None, f"vrp-default/gate={signals.gate_label}->STAND_ASIDE")


# ---------------------------------------------------------------------------
# Free functions (pure, module-level)
# ---------------------------------------------------------------------------
def regime_from_cycle(
    cycle: dict[str, Any],
    *,
    k: float = DEFAULT_K,
    iv_rank: Optional[float] = None,
    trend: Optional[float] = None,
) -> RegimeSignals:
    """Build ``RegimeSignals`` from a raw cycle dict (the ``cycles_from_db``
    shape). Calls the real ``gate_decision`` + ``implied_move`` — the regime rule
    is consumed, never re-derived. ``iv_rank`` / ``trend`` are injected by the
    caller (spec 01 computes them from trailing windows; the orchestrator stays
    single-cycle-pure). Reads ONLY entry-date observables — never ``expiry_spot``.
    """
    rv = cycle.get("realized_vol_20d")
    iv = cycle.get("straddle_iv")
    spot = cycle.get("spot")
    dte = cycle.get("dte")

    rv_f = float(rv) if rv is not None else 0.0
    iv_f = float(iv) if iv is not None else 0.0
    spot_f = float(spot) if spot is not None else 0.0
    dte_i = int(dte) if dte is not None else 0

    gate_label = gate_decision(rv, iv, k=k)
    im = _implied_move(spot, iv, dte) if (spot and iv and dte is not None) else None
    iv_ratio = (rv_f / iv_f) if iv_f > 0 else 0.0
    vrp = iv_f - rv_f

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
        iv_rank=iv_rank,
        trend=trend,
    )


def cycles_for_index(index: IndexParams) -> list[dict[str, Any]]:
    """Thin DB wrapper (the ONLY DB touch in this module). Hydrates cycles via
    the re-exported ``cycles_from_db`` for the given index."""
    return cycles_from_db(
        symbol=index.symbol,
        nifty_id=index.nifty_id,
        vix_id=index.vix_id if index.vix_id is not None else "21",
        mode=index.expiry_mode,
    )


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
    ) -> None:
        self.policy = policy
        self.index = index
        self.k = k
        self.capital = capital
        self.slip_pct = slip_pct
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
        Deterministic, side-effect-free, DB-free."""
        strategy, params, reason = self.policy.select(signals, self.allowed)
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
        """Per cycle: regime_from_cycle → decide → dispatch (or skip).
        One decision + at most one position per cycle (one-position discipline,
        spec 03 §6). Aggregates chosen trades via the engine's own helpers."""
        decisions: list[RoutingDecision] = []
        chosen_trades: list[Any] = []
        per_strategy: dict[str, dict] = {}

        for cycle in cycles:
            signals = regime_from_cycle(cycle, k=self.k)
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
        metrics = self._aggregate(chosen_trades, n_cycles=len(cycles))

        n_aside = sum(1 for d in decisions if d.stand_aside)
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
    def _aggregate(self, trades: list[Any], *, n_cycles: int) -> dict[str, Any]:
        """Portfolio rollup using the SAME math ``run_strategy_backtest`` uses —
        ``_sharpe_from_pnls`` / ``_max_drawdown`` / ``go_no_go`` are imported,
        never copied. ROM = Σnet_pnl / Σspan, exactly as the engine computes it."""
        n_trades = len(trades)
        if n_trades == 0:
            metrics: dict[str, Any] = {
                "strategy": "ORCHESTRATED",
                "gate": "vol",
                "trades": [],
                "n_cycles": n_cycles,
                "n_trades": 0,
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
                "mean_span": 0.0,
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

        spans_oos = spans[split_idx:]
        total_span_oos = sum(spans_oos)
        net_oos = sum(pnls_oos)
        rom_oos = net_oos / total_span_oos if total_span_oos > 0 else 0.0

        metrics = {
            "strategy": "ORCHESTRATED",
            "gate": "vol",
            "trades": trades,
            "n_cycles": n_cycles,
            "n_trades": n_trades,
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
            "mean_span": mean_span,
        }
        metrics["go_no_go"] = go_no_go(metrics, capital=self.capital)
        return metrics


# ---------------------------------------------------------------------------
# CLI / backtest entrypoint (spec 06 §4.3) — DB-init only after arg-parse.
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
    policy: Optional[RoutingPolicy] = None,
) -> dict[str, Any]:
    """ORCHESTRATED vs each single GO strategy (gate='vol') + best_single.

    Singles use the engine directly (gate='vol'); orchestrated uses the router
    (gate='none' internally — the router IS the gate). Returns a dict with
    ``orchestrated``, ``singles`` (name->metrics), ``best_single``, and the
    OrchestratorResult for the pick/aside counts.
    """
    policy = policy or RegimeRoutingPolicy(RoutingParams(gate_k=k))
    orch = FnoOrchestrator(policy, index=index, k=k, capital=capital)
    orch_result = orch.run(cycles)

    singles: dict[str, dict[str, Any]] = {}
    for name in GO_SET:
        singles[name] = run_strategy_backtest(
            FNO_STRATEGIES[name],
            cycles,
            k=k,
            capital=capital,
            lot=index.lot,
            step=index.step,
            gate="vol",
        )

    best_name = max(singles, key=lambda n: singles[n]["return_on_margin"])
    return {
        "orchestrated": orch_result.metrics,
        "orchestrated_result": orch_result,
        "singles": singles,
        "best_single": best_name,
    }


def _print_comparison(cmp: dict[str, Any], *, index: IndexParams, k: float, capital: float) -> None:
    singles = cmp["singles"]
    orch = cmp["orchestrated"]
    orch_result: OrchestratorResult = cmp["orchestrated_result"]
    best_name = cmp["best_single"]

    print(
        f"ORCHESTRATOR BACKTEST — index={index.symbol} router=regime "
        f"mode={index.expiry_mode} k={k:.2f} capital=₹{capital:,.0f}"
    )
    print(f"caveats: {CAVEATS}")
    print(f"n_cycles={orch['n_cycles']}\n")

    hdr = f"{'row':<20} {'n':>4} {'net':>12} {'ROM':>8} {'ROM_oos':>8} {'win':>7} {'sharpe':>7} {'GO':>6}"
    print(hdr)
    print("-" * len(hdr))

    def _row(label: str, m: dict[str, Any]) -> None:
        go = "GO" if m["go_no_go"][0] else "NO-GO"
        rom_oos = m.get("rom_oos")
        rom_oos_s = _fmt_pct(rom_oos) if rom_oos is not None else f"{'-':>8}"
        print(
            f"{label:<20} {m['n_trades']:>4} {m['net_pnl']:>12,.0f} "
            f"{_fmt_pct(m['return_on_margin'])} {rom_oos_s} "
            f"{m['win_rate']:>6.1%} {m['sharpe']:>7.2f} {go:>6}"
        )

    for name in GO_SET:
        _row(name, singles[name])
    _row(f"best_single={best_name}", singles[best_name])
    print("-" * len(hdr))
    _row("ORCHESTRATED", orch)

    picks = " ".join(
        f"{n}:{d['deployed']}" for n, d in sorted(orch_result.per_strategy.items())
    )
    print(
        f"\npicks: {picks or '(none)'}  aside:{orch_result.n_stand_aside}  "
        f"traded:{orch_result.n_traded}"
    )

    best = singles[best_name]
    o_rom = orch["return_on_margin"]
    b_rom = best["return_on_margin"]
    print(
        f"\nVERDICT (PRELIMINARY): ORCHESTRATED ROM {o_rom:.2%} vs "
        f"best_single({best_name}) ROM {b_rom:.2%} "
        f"(Δ {o_rom - b_rom:+.2%}) — "
        f"{'selection adds edge' if o_rom > b_rom else 'no selection edge'}. "
        "Real-IV forward paper-log is the truth test. PAPER only."
    )
    print(f"\nORCHESTRATED go_no_go: {orch['go_no_go'][1]}")


def main() -> None:  # pragma: no cover
    """``python -m research.backtest.fno_orchestrator --index NIFTY --k 0.9``."""
    parser = argparse.ArgumentParser(
        description="F&O Strategy Orchestrator — regime-aware router + NIFTY backtest",
    )
    parser.add_argument("--index", default="NIFTY", choices=list(INDEX_REGISTRY))
    parser.add_argument("--k", type=float, default=DEFAULT_K)
    parser.add_argument("--capital", type=float, default=200_000.0)
    parser.add_argument("--policy", default="regime", choices=["regime", "vrp_default"])
    args = parser.parse_args()

    index = INDEX_REGISTRY[args.index]
    if not index.has_history:
        raise NotImplementedError(
            f"no historical data for {args.index}; ingest first (see _CONTEXT.md #1)"
        )

    # DB-init only after arg-parse so --help never touches it (mirrors fno_strategies.main).
    from config import get_config
    from db import init_db

    init_db(get_config().db_url)
    cycles = cycles_for_index(index)

    policy: RoutingPolicy = (
        VrpDefaultPolicy(RoutingParams(gate_k=args.k))
        if args.policy == "vrp_default"
        else RegimeRoutingPolicy(RoutingParams(gate_k=args.k))
    )
    cmp = run_comparison(
        cycles, index=index, k=args.k, capital=args.capital, policy=policy
    )
    _print_comparison(cmp, index=index, k=args.k, capital=args.capital)


if __name__ == "__main__":  # pragma: no cover
    main()
