"""
RAVEN — Regime-Adaptive, skew-aware asymmetric Vega-short condor (Phase-1).

WHAT IT IS
----------
RAVEN is a defined-risk, short-vega iron condor with two upgrades over the
symmetric baseline condor (``research.backtest.fno_condor`` / the ``iron_condor``
``StrategySpec`` in ``research.backtest.fno_strategies``):

  (a) REGIME-GATED — deploy ONLY when the GATE-V2 verdict
      (``ml.fno_vol_gate.gate_v2_decision``) is ``SELL_PREMIUM``: VRP-rich (the
      IV-RV spread sits in a rich percentile of its trailing window), term
      structure NOT in backwardation, and NOT an event/expiry stand-aside day.
      This is strictly MORE conservative than the v1 vol-gate the baseline condor
      uses — a v1 SELL that GATE-V2 vetoes becomes a stand-aside.

  (b) SKEW-AWARE / ASYMMETRIC — index equity-options carry a structural PUT
      SKEW: out-of-the-money puts trade at a richer implied vol than the
      symmetric call wing (crash insurance demand). The symmetric condor places
      both shorts at the SAME |delta|, leaving the over-priced put skew partly
      unharvested. RAVEN places the PUT short at a DEEPER target delta (further
      OTM, where the skew premium is fattest) than the CALL short, and can widen
      the put wing independently. The short strikes are chosen by FIXED-DELTA
      selection (``fno_condor.select_short_strike_by_delta``) using REAL
      per-strike IV (``cycle["per_strike_iv"]`` / ``option_chain_snapshot``,
      projected onto the cycle) when present, falling back to a single flat ATM
      IV otherwise. With real per-strike IV a given target |delta| is reached at a
      strike whose vol REFLECTS the skew, so the put short is placed where the
      skew is actually fatter; with flat IV the asymmetry is delta-driven only
      (still a valid wider-put structure, but it cannot SEE the skew — flagged).
      NOTE: RAVEN selects by DELTA, not by an explicit skew-richness ranking — it
      harvests skew only insofar as the supplied per-strike IV encodes it; the
      backtest pulls the cycle's ``per_strike_iv`` through the engine's per-cycle
      skew bridge (added to ``run_strategy_backtest``) so the backtested legs —
      not just the diagnostics — price on the real skew when it is present.

LANE DISCIPLINE
---------------
Pure / testable. Touches only ``research/backtest`` + ``ml.fno_vol_gate``
(GATE-V2 is index-agnostic). It REUSES — never re-implements — the condor strike
math (``select_short_strike_by_delta``), the multi-leg backtest engine
(``run_strategy_backtest`` via a registered ``StrategySpec``) and the honest edge
restatement (``fno_edge_restate``). The one supporting change to
``fno_strategies.py`` is a GENERIC per-cycle ``per_strike_iv`` bridge in
``run_strategy_backtest`` (any skew-aware builder benefits; symmetric builders
ignore the key) — RAVEN does not special-case the engine. The only DB touch is
``cycles_from_db`` inside the CLI.

INDEX-AGNOSTIC ([[index-agnostic-fno]])
---------------------------------------
No NIFTY assumptions are baked into the LOGIC. The ``lot`` / ``step`` are
PARAMETERS (defaulting to NIFTY values — ``NIFTY_LOT`` / ``step=50`` — but
overridable per call for BANKNIFTY/FINNIFTY/etc.), the gate thresholds ride on a
``GateV2Config`` (per-index overridable), and the cycle dicts are the same
index-agnostic shape ``cycles_from_db`` produces for ANY ``IndexConfig``. A
non-NIFTY underlying drives RAVEN by passing its own cycles + ``GateV2Config``.

HONESTY / PRELIMINARY CAVEATS (inherited verbatim — never dropped)
------------------------------------------------------------------
VIX-as-weekly-IV proxy · CLOSE-not-FSP settlement · EXPIRY-ONLY (tail-blind;
defined-risk loss is wing-capped) · single-σ Black-76 unless real per-strike IV
is supplied · small sample (n ≈ 166–233 weekly cycles) → wide CIs · selection /
config trials inflate Sharpe → deflate it · PAPER / research only. The skew edge
is UNVERIFIED until real per-strike option IV (``per_strike_iv``) is collected
forward — under the flat-IV fallback the put/call shorts are merely placed at
different deltas, NOT on a measured skew. Flagged PRELIMINARY throughout.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

# ---------------------------------------------------------------------------
# Reused surface — import, never re-implement (lane discipline)
# ---------------------------------------------------------------------------
from ml.fno_vol_gate import (
    SELL_PREMIUM,
    GateV2Config,
    gate_v2_decision,
)
from research.backtest.fno_condor import (
    DELTA_IV_SOURCE_FLAT,
    DELTA_IV_SOURCE_REAL,
    resolve_iv_source,
    select_short_strike_by_delta,
)
from research.backtest.fno_costs import NIFTY_LOT
from research.backtest.fno_edge_restate import (
    CycleSeries,
    restate_edge,
)
from research.backtest.fno_strategies import (
    FNO_STRATEGIES,
    Leg,
    StrategySpec,
    run_strategy_backtest,
)

logger = logging.getLogger("dhan.backtest.fno_raven")

# Strategy key registered into a private copy of the strategy registry.
RAVEN_STRATEGY_NAME = "raven"

# Default RAVEN structure parameters. The PUT short sits DEEPER (smaller delta =
# further OTM) than the CALL short to harvest the richer OTM put skew; the put
# wing can be wider than the call wing for the same reason.
DEFAULT_PUT_SHORT_DELTA = 0.12   # deeper OTM put short (richer skew)
DEFAULT_CALL_SHORT_DELTA = 0.18  # shallower OTM call short (thinner skew)
DEFAULT_PUT_WING_STRIKES = 3     # wider protective put wing
DEFAULT_CALL_WING_STRIKES = 2    # tighter protective call wing

# The symmetric baseline both shorts share (mid of the two RAVEN deltas) so the
# comparison isolates the ASYMMETRY, not just a different delta level.
DEFAULT_BASELINE_DELTA = 0.16
DEFAULT_BASELINE_WING_STRIKES = 2

CAVEATS = (
    "VIX-as-weekly-IV proxy · CLOSE-not-FSP · EXPIRY-ONLY (tail-blind; "
    "defined-risk wing-capped) · single-σ B76 unless real per-strike IV · "
    "small sample → wide CIs · trials inflate Sharpe → deflated · skew edge "
    "UNVERIFIED under flat-IV fallback · PAPER only"
)


# ===========================================================================
# RAVEN structure construction (skew-asymmetric, fixed-delta, defined-risk)
# ===========================================================================
@dataclass(frozen=True)
class RavenParams:
    """RAVEN structure knobs (index-agnostic — no NIFTY constants).

    Attributes
    ----------
    put_short_delta:
        Target |delta| of the SHORT PUT leg (deeper OTM → richer skew). Default
        :data:`DEFAULT_PUT_SHORT_DELTA` (0.12).
    call_short_delta:
        Target |delta| of the SHORT CALL leg (shallower OTM). Default
        :data:`DEFAULT_CALL_SHORT_DELTA` (0.18). ``put_short_delta <
        call_short_delta`` ⇒ the put short is further OTM (the asymmetry).
    put_wing_strikes:
        Grid steps from the short put down to the LONG (protective) put.
    call_wing_strikes:
        Grid steps from the short call up to the LONG (protective) call.
    """

    put_short_delta: float = DEFAULT_PUT_SHORT_DELTA
    call_short_delta: float = DEFAULT_CALL_SHORT_DELTA
    put_wing_strikes: int = DEFAULT_PUT_WING_STRIKES
    call_wing_strikes: int = DEFAULT_CALL_WING_STRIKES

    def __post_init__(self) -> None:
        for name, v in (
            ("put_short_delta", self.put_short_delta),
            ("call_short_delta", self.call_short_delta),
        ):
            if not (0.0 < v < 1.0):
                raise ValueError(f"RavenParams.{name} must be in (0, 1), got {v}")
        for name, v in (
            ("put_wing_strikes", self.put_wing_strikes),
            ("call_wing_strikes", self.call_wing_strikes),
        ):
            if v < 1:
                raise ValueError(f"RavenParams.{name} must be >= 1, got {v}")


def build_raven_strikes(
    spot: float,
    flat_iv: float,
    dte: int,
    *,
    params: RavenParams | None = None,
    step: int = 50,
    per_strike_iv: dict[int, float] | None = None,
) -> tuple[dict[str, int], str]:
    """Construct the four RAVEN strikes — asymmetric, fixed-delta, defined-risk.

    The PUT short is selected at ``params.put_short_delta`` (deeper OTM, richer
    skew) and the CALL short at ``params.call_short_delta`` (shallower), each via
    :func:`fno_condor.select_short_strike_by_delta` using REAL per-strike IV
    (``per_strike_iv``) when present and the flat ATM IV otherwise. The protective
    wings sit ``params.put_wing_strikes`` / ``params.call_wing_strikes`` grid
    steps further OTM — independently, so the put wing can be wider.

    Returns ``(strikes, delta_iv_source)`` where ``strikes`` has the canonical
    keys ``short_put_k / long_put_k / short_call_k / long_call_k`` and
    ``delta_iv_source`` is :data:`fno_condor.DELTA_IV_SOURCE_REAL` if EITHER short
    resolved a real per-strike IV, else :data:`fno_condor.DELTA_IV_SOURCE_FLAT`
    (the conservative, skew-blind fallback).

    DEFINED-RISK GUARDS
    -------------------
    Two degenerate inputs would otherwise collapse the structure and are rejected
    with a ``ValueError`` (QA finding): (1) ``dte <= 0`` — at/after expiry the
    Black-76 delta degenerates to a pure moneyness step, so both shorts snap to
    the same ATM strike (``short_put_k == short_call_k`` → not a condor); a real
    cycle always enters with ``dte > 0``. (2) a non-positive long-put strike
    (``long_put_k <= 0``) — only reachable for an absurdly small ``spot`` (sub-grid
    index), it would mean a negative strike. Both are impossible for valid index
    cycles; raising makes the defined-risk invariant unconditional rather than
    silently emitting a malformed structure.
    """
    if dte <= 0:
        raise ValueError(f"build_raven_strikes: dte must be > 0, got {dte}")
    p = params or RavenParams()

    short_put_k, sp_src = select_short_strike_by_delta(
        spot, flat_iv, dte, "PE", p.put_short_delta,
        step=step, per_strike_iv=per_strike_iv,
    )
    short_call_k, sc_src = select_short_strike_by_delta(
        spot, flat_iv, dte, "CE", p.call_short_delta,
        step=step, per_strike_iv=per_strike_iv,
    )
    long_put_k = short_put_k - p.put_wing_strikes * step
    long_call_k = short_call_k + p.call_wing_strikes * step

    # Unconditional defined-risk invariant: longs strictly OTM of their shorts and
    # the two shorts strictly separated (a genuine condor, not a collapsed fly).
    if not (long_put_k < short_put_k < short_call_k < long_call_k):
        raise ValueError(
            "build_raven_strikes: degenerate strikes violate the defined-risk "
            f"invariant long_put<short_put<short_call<long_call: "
            f"({long_put_k}, {short_put_k}, {short_call_k}, {long_call_k}) "
            f"— spot={spot}, iv={flat_iv}, dte={dte}, step={step}"
        )

    delta_iv_source = (
        DELTA_IV_SOURCE_REAL
        if DELTA_IV_SOURCE_REAL in (sp_src, sc_src)
        else DELTA_IV_SOURCE_FLAT
    )
    return (
        {
            "short_put_k": short_put_k,
            "long_put_k": long_put_k,
            "short_call_k": short_call_k,
            "long_call_k": long_call_k,
        },
        delta_iv_source,
    )


def _strikes_to_legs(strikes: dict[str, int]) -> list[Leg]:
    """Convert a condor strikes dict to the canonical 4-Leg list.

    Order matches ``fno_strategies.build_iron_condor`` (SELL PE, BUY PE, SELL CE,
    BUY CE) so the engine prices / resolves / costs RAVEN identically to the
    baseline condor.
    """
    return [
        Leg("PE", "SELL", float(strikes["short_put_k"]), 1),
        Leg("PE", "BUY", float(strikes["long_put_k"]), 1),
        Leg("CE", "SELL", float(strikes["short_call_k"]), 1),
        Leg("CE", "BUY", float(strikes["long_call_k"]), 1),
    ]


def build_raven(
    spot: float,
    atm_strike: int,  # noqa: ARG001 — kept for the StrategyBuilder signature
    step: int,
    dte: int,
    sigma: float,
    params: dict[str, Any],
) -> list[Leg]:
    """``StrategyBuilder``-signature wrapper that builds the RAVEN Leg list.

    Conforms to ``fno_strategies.StrategySpec.build`` exactly
    ``(spot, atm_strike, step, dte, sigma, params) -> list[Leg]`` so RAVEN drops
    straight into ``run_strategy_backtest``. The skew-asymmetric strikes come from
    :func:`build_raven_strikes`; ``sigma`` is the cycle's resolved flat ATM IV and
    ``params["per_strike_iv"]`` (optional) supplies real per-strike IV.

    DEFINED-RISK INVARIANT: both wings are placed strictly OTM of their short
    (long put below the short put, long call above the short call), so the
    structure is always a bounded-loss condor — the engine's "defined" SPAN model
    therefore correctly bounds the margin.
    """
    rp = RavenParams(
        put_short_delta=float(params.get("put_short_delta", DEFAULT_PUT_SHORT_DELTA)),
        call_short_delta=float(params.get("call_short_delta", DEFAULT_CALL_SHORT_DELTA)),
        put_wing_strikes=int(params.get("put_wing_strikes", DEFAULT_PUT_WING_STRIKES)),
        call_wing_strikes=int(params.get("call_wing_strikes", DEFAULT_CALL_WING_STRIKES)),
    )
    per_strike_iv = params.get("per_strike_iv")
    strikes, _src = build_raven_strikes(
        spot, sigma, dte, params=rp, step=step, per_strike_iv=per_strike_iv,
    )
    return _strikes_to_legs(strikes)


def build_symmetric_baseline(
    spot: float,
    atm_strike: int,  # noqa: ARG001
    step: int,
    dte: int,
    sigma: float,
    params: dict[str, Any],
) -> list[Leg]:
    """Symmetric same-delta condor baseline (the bar RAVEN must beat).

    Both shorts at the SAME |delta| (``params["baseline_delta"]``) and EQUAL wing
    widths, built by the SAME fixed-delta machinery as RAVEN so the ONLY
    difference is the asymmetry/skew-awareness — not the pricing, costs, or gate.
    """
    delta = float(params.get("baseline_delta", DEFAULT_BASELINE_DELTA))
    wings = int(params.get("baseline_wing_strikes", DEFAULT_BASELINE_WING_STRIKES))
    per_strike_iv = params.get("per_strike_iv")
    rp = RavenParams(
        put_short_delta=delta,
        call_short_delta=delta,
        put_wing_strikes=wings,
        call_wing_strikes=wings,
    )
    strikes, _src = build_raven_strikes(
        spot, sigma, dte, params=rp, step=step, per_strike_iv=per_strike_iv,
    )
    return _strikes_to_legs(strikes)


# RAVEN + the symmetric baseline as registered StrategySpecs. Both are
# defined-risk, sell-premium, single-expiry, "defined" SPAN — so the engine's
# margin / cost / resolution paths treat them identically and the comparison is
# clean.
RAVEN_SPEC = StrategySpec(
    name=RAVEN_STRATEGY_NAME,
    build=build_raven,
    defined_risk=True,
    sell_premium=True,
    needs_multi_expiry=False,
    default_params={
        "put_short_delta": DEFAULT_PUT_SHORT_DELTA,
        "call_short_delta": DEFAULT_CALL_SHORT_DELTA,
        "put_wing_strikes": DEFAULT_PUT_WING_STRIKES,
        "call_wing_strikes": DEFAULT_CALL_WING_STRIKES,
    },
    span_model="defined",
)

BASELINE_SPEC = StrategySpec(
    name="raven_symmetric_baseline",
    build=build_symmetric_baseline,
    defined_risk=True,
    sell_premium=True,
    needs_multi_expiry=False,
    default_params={
        "baseline_delta": DEFAULT_BASELINE_DELTA,
        "baseline_wing_strikes": DEFAULT_BASELINE_WING_STRIKES,
    },
    span_model="defined",
)


# ===========================================================================
# Regime gate (GATE-V2) — pre-filter cycles RAVEN may deploy on
# ===========================================================================
def _trailing_spreads_before(
    cycles: list[dict[str, Any]],
    idx: int,
    cfg: GateV2Config,
) -> list[float | None]:
    """PIT trailing IV-RV spreads for the GATE-V2 percentile sub-gate.

    Returns the calendar-rebased IV-RV spread of every cycle STRICTLY BEFORE
    ``idx`` (no look-ahead — the current cycle's own spread is excluded). Each
    entry is computed from that earlier cycle's resolved IV + realized vol via the
    SAME gate helpers, so the percentile rank is apples-to-apples. Cycles with an
    unusable IV/RV contribute ``None`` (the gate filters those).
    """
    from ml.fno_vol_gate import _rebase_to_calendar, vrp_spread  # noqa: PLC0415

    out: list[float | None] = []
    for j in range(idx):
        c = cycles[j]
        iv, _src = resolve_iv_source(c)
        rvol = c.get("realized_vol_20d")
        if iv is None or rvol is None:
            out.append(None)
            continue
        rv_cal = _rebase_to_calendar(float(rvol))
        out.append(vrp_spread(rv_cal, iv))
    return out


def gate_cycles_v2(
    cycles: list[dict[str, Any]],
    *,
    cfg: GateV2Config | None = None,
    event_dates: list[date] | None = None,
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Apply GATE-V2 per cycle (chronological), returning the SELL_PREMIUM subset.

    For each cycle (in order) computes the GATE-V2 verdict from its resolved IV +
    realized vol, the PIT trailing spreads of all earlier cycles
    (:func:`_trailing_spreads_before`), the term structure (front IV = this
    cycle's IV; next IV = ``cycle["next_iv"]`` when present), and the event/expiry
    veto. RAVEN deploys ONLY on ``SELL_PREMIUM`` cycles.

    Cycles MUST be chronological (the percentile sub-gate's PIT window relies on
    order). Returns ``(kept_cycles, verdicts)`` parallel by kept index — verdicts
    are the :class:`GateV2Result` objects for the KEPT cycles (for diagnostics).
    """
    config = cfg or GateV2Config()
    kept: list[dict[str, Any]] = []
    verdicts: list[Any] = []
    for i, c in enumerate(cycles):
        iv, _src = resolve_iv_source(c)
        rvol = c.get("realized_vol_20d")
        if iv is None or rvol is None:
            continue
        trailing = _trailing_spreads_before(cycles, i, config)
        result = gate_v2_decision(
            float(rvol),
            iv,
            trailing_spreads=trailing,
            front_iv=iv,
            next_iv=c.get("next_iv"),
            cycle_date=c.get("entry_date"),
            expiry_date=c.get("expiry_date"),
            event_dates=event_dates,
            rv_is_trading_basis=True,
            cfg=config,
        )
        if result.decision == SELL_PREMIUM:
            kept.append(c)
            verdicts.append(result)
    return kept, verdicts


# ===========================================================================
# Backtest — RAVEN vs symmetric baseline, gated + honest restate
# ===========================================================================
@dataclass
class RavenResult:
    """Full RAVEN backtest output: per-leg metrics + the honest edge restatement."""

    raven_metrics: dict[str, Any]
    baseline_metrics: dict[str, Any]
    restate: dict[str, Any]
    n_input_cycles: int
    n_gated_cycles: int
    raven_params: RavenParams
    gate_config: GateV2Config
    delta_iv_sources: dict[str, int] = field(default_factory=dict)


def run_raven_backtest(
    cycles: list[dict[str, Any]],
    *,
    raven_params: RavenParams | None = None,
    gate_config: GateV2Config | None = None,
    event_dates: list[date] | None = None,
    capital: float = 200_000.0,
    lot: int = NIFTY_LOT,
    step: int = 50,
    slip_pct: float = 0.005,
    baseline_delta: float = DEFAULT_BASELINE_DELTA,
    baseline_wing_strikes: int = DEFAULT_BASELINE_WING_STRIKES,
    n_configs_tried: int = 1,
    haircut_iv_mult: float | None = 0.80,
) -> RavenResult:
    """Backtest RAVEN (regime-gated, skew-asymmetric) vs the symmetric baseline.

    Steps:
      1. GATE-V2 pre-filter (:func:`gate_cycles_v2`) → the SELL_PREMIUM subset
         RAVEN deploys on. The SAME gated subset drives both legs so the only
         difference is the structure (asymmetry), not which cycles are traded.
      2. Run RAVEN + the symmetric baseline over the gated subset via the shared
         multi-leg engine (``run_strategy_backtest`` with ``gate="none"`` — the
         regime gating is already applied in step 1; the engine must not re-gate).
      3. Honest edge restatement (``fno_edge_restate.restate_edge``): RAVEN's
         per-cycle P&L vs the symmetric baseline as the α benchmark, the
         short-vol/VRP factor (β), tail (γ) + the falsification battery. An
         optional 0.80× IV haircut series feeds the IV_HAIRCUT gate.

    α-BENCHMARK FRAMING (honest caveat)
    -----------------------------------
    The α benchmark is the SYMMETRIC condor on the SAME GATE-V2-filtered cycles —
    NOT an always-on ungated condor. So ``alpha_vs_passive`` answers strictly "does
    the ASYMMETRY add return beyond the same-delta condor under the SAME gate?"; it
    does NOT re-test the gate's own selection value (the symmetric baseline already
    inherits that). A positive α here is a narrower, more honest claim than "beats a
    passive short-premium book" — the latter is the bar the orchestrator MVP failed
    and is not re-litigated here. Read α as asymmetry-vs-symmetric, gate held equal.

    Returns a :class:`RavenResult`. PRELIMINARY caveats are carried in the restate
    verdict + this module's :data:`CAVEATS`.
    """
    rp = raven_params or RavenParams()
    cfg = gate_config or GateV2Config()

    gated, _verdicts = gate_cycles_v2(cycles, cfg=cfg, event_dates=event_dates)

    raven_pdict: dict[str, Any] = {
        "put_short_delta": rp.put_short_delta,
        "call_short_delta": rp.call_short_delta,
        "put_wing_strikes": rp.put_wing_strikes,
        "call_wing_strikes": rp.call_wing_strikes,
    }
    base_pdict: dict[str, Any] = {
        "baseline_delta": baseline_delta,
        "baseline_wing_strikes": baseline_wing_strikes,
    }

    # gate="none": the regime gate is already applied (step 1). The engine's own
    # vol-gate must NOT re-filter, or RAVEN would be double-gated (and the
    # baseline single-gated) — that would corrupt the like-for-like comparison.
    raven_metrics = run_strategy_backtest(
        RAVEN_SPEC, gated, raven_pdict,
        capital=capital, lot=lot, step=step, slip_pct=slip_pct, gate="none",
    )
    baseline_metrics = run_strategy_backtest(
        BASELINE_SPEC, gated, base_pdict,
        capital=capital, lot=lot, step=step, slip_pct=slip_pct, gate="none",
    )

    # Per-leg delta-IV provenance: how many RAVEN cycles priced their short
    # strikes off REAL per-strike IV (skew SEEN) vs the flat-IV fallback
    # (skew-blind). Drives the PRELIMINARY flag.
    delta_iv_sources = _delta_iv_source_counts(gated, rp, step)

    restate = _restate_raven_vs_baseline(
        raven_metrics,
        baseline_metrics,
        cycles=gated,
        capital=capital,
        n_configs_tried=n_configs_tried,
        haircut_iv_mult=haircut_iv_mult,
        raven_params=rp,
        gate_config=cfg,
        event_dates=event_dates,
        lot=lot,
        step=step,
        slip_pct=slip_pct,
    )

    return RavenResult(
        raven_metrics=raven_metrics,
        baseline_metrics=baseline_metrics,
        restate=restate,
        n_input_cycles=len(cycles),
        n_gated_cycles=len(gated),
        raven_params=rp,
        gate_config=cfg,
        delta_iv_sources=delta_iv_sources,
    )


def _delta_iv_source_counts(
    cycles: list[dict[str, Any]],
    rp: RavenParams,
    step: int,
) -> dict[str, int]:
    """Count how many cycles RAVEN priced off real per-strike IV vs flat fallback."""
    counts = {DELTA_IV_SOURCE_REAL: 0, DELTA_IV_SOURCE_FLAT: 0}
    for c in cycles:
        iv, _src = resolve_iv_source(c)
        if iv is None:
            continue
        dte = c.get("dte")
        spot = c.get("spot")
        if dte is None or spot is None:
            continue
        _strikes, dsrc = build_raven_strikes(
            float(spot), iv, int(dte), params=rp, step=step,
            per_strike_iv=c.get("per_strike_iv"),
        )
        counts[dsrc] = counts.get(dsrc, 0) + 1
    return counts


def _strategy_series(
    trades: list[Any],
    *,
    passive_trades: list[Any] | None = None,
    capital: float,
    n_configs_tried: int,
) -> CycleSeries:
    """Build a :class:`CycleSeries` from ``StrategyTrade`` records.

    ``fno_edge_restate.series_from_trades`` reads ``CondorTrade.max_loss``; the
    multi-leg engine emits ``StrategyTrade`` whose defined-risk margin is ``span``
    (the engine's SPAN approximation) — so we build the series here, margin=span,
    iv_source derived from each trade's ``straddle_iv`` provenance. ``passive_trades``
    (the symmetric baseline on the SAME gated cycles) supplies the α-vs-passive
    benchmark, aligned by ``entry_date``; a duplicate entry_date is rejected
    (ambiguous alignment), mirroring ``series_from_trades``.

    iv_source per trade: a ``StrategyTrade`` does not carry an explicit IV source,
    so we conservatively label every traded cycle ``IV_SOURCE_VIX_PROXY`` (drives
    the restate PRELIMINARY flag) — the real-vs-proxy split is reported separately
    via :func:`_delta_iv_source_counts`.
    """
    from research.backtest.fno_condor import IV_SOURCE_VIX_PROXY  # noqa: PLC0415

    ordered = sorted(trades, key=lambda t: t.entry_date)
    pnl = [float(t.net_pnl) for t in ordered]
    margin = [float(t.span) for t in ordered]
    factor = _short_vol_factor(ordered)
    regime = _regime_labels(ordered)
    iv_source = [IV_SOURCE_VIX_PROXY] * len(ordered)

    passive_pnl: list[float] | None = None
    if passive_trades is not None:
        gated_dates = [t.entry_date for t in ordered]
        if len(set(gated_dates)) != len(gated_dates):
            raise ValueError(
                "_strategy_series: duplicate entry_date in RAVEN trades — by-date "
                "passive alignment is ambiguous; build the CycleSeries positionally."
            )
        passive_dates = [t.entry_date for t in passive_trades]
        if len(set(passive_dates)) != len(passive_dates):
            raise ValueError(
                "_strategy_series: duplicate entry_date in baseline trades — "
                "by-date alignment is ambiguous."
            )
        by_date = {t.entry_date: float(t.net_pnl) for t in passive_trades}
        passive_pnl = [by_date.get(t.entry_date, 0.0) for t in ordered]

    return CycleSeries(
        pnl=pnl,
        margin=margin,
        short_vol_factor=factor,
        passive_pnl=passive_pnl,
        regime=regime,
        iv_source=iv_source,
        capital=capital,
        n_configs_tried=n_configs_tried,
    )


def _short_vol_factor(trades: list[Any]) -> list[float]:
    """β factor per RAVEN trade: the IV-RV spread (implied − realized) at entry.

    A short-vol book's P&L SHOULD load on this factor; the β lens then tests
    whether RAVEN's return is just that loading or carries residual α.
    """
    factor: list[float] = []
    for t in trades:
        iv = float(getattr(t, "straddle_iv", 0.0) or 0.0)
        rv = float(getattr(t, "realized_vol_20d", 0.0) or 0.0)
        factor.append(iv - rv)
    return factor


def _regime_labels(trades: list[Any]) -> list[str]:
    """VIX-tercile regime label per trade ("calm"/"mid"/"stress") by entry IV.

    Terciles are computed over THIS trade set (chronological order preserved). A
    degenerate (all-equal IV) set labels everything "mid" so the regime gate is
    simply skipped rather than mislabelling.
    """
    ivs = [float(getattr(t, "straddle_iv", 0.0) or 0.0) for t in trades]
    if not ivs:
        return []
    ordered = sorted(ivs)
    n = len(ordered)
    lo = ordered[max(0, n // 3 - 1)]
    hi = ordered[min(n - 1, (2 * n) // 3)]
    if hi <= lo:
        return ["mid"] * len(ivs)
    labels: list[str] = []
    for v in ivs:
        if v <= lo:
            labels.append("calm")
        elif v >= hi:
            labels.append("stress")
        else:
            labels.append("mid")
    return labels


def _restate_raven_vs_baseline(
    raven_metrics: dict[str, Any],
    baseline_metrics: dict[str, Any],
    *,
    cycles: list[dict[str, Any]],
    capital: float,
    n_configs_tried: int,
    haircut_iv_mult: float | None,
    raven_params: RavenParams,
    gate_config: GateV2Config,
    event_dates: list[date] | None,
    lot: int,
    step: int,
    slip_pct: float,
) -> dict[str, Any]:
    """Honest edge restate of RAVEN with the symmetric baseline as the α-passive.

    RAVEN's trades are the gated series; the symmetric baseline (same gated
    cycles) is the PASSIVE benchmark — so α-vs-passive answers "does the ASYMMETRY
    add return beyond the same-delta symmetric condor?" (the exact bar that
    matters here). β uses the IV-RV factor, regime uses the VIX-tercile label, and
    an optional 0.80× IV haircut series feeds the IV_HAIRCUT gate.
    """
    raven_trades = raven_metrics.get("trades", [])
    baseline_trades = baseline_metrics.get("trades", [])

    if not raven_trades:
        # No gated cycles → restate the empty series so consumers get the stable
        # key set + NO-GO verdict instead of a KeyError.
        empty = CycleSeries(pnl=[], margin=[])
        return restate_edge(empty)

    series = _strategy_series(
        raven_trades,
        passive_trades=baseline_trades,
        capital=capital,
        n_configs_tried=n_configs_tried,
    )

    haircut_series: CycleSeries | None = None
    if haircut_iv_mult is not None and haircut_iv_mult > 0:
        haircut_series = _haircut_series(
            cycles,
            mult=haircut_iv_mult,
            raven_params=raven_params,
            gate_config=gate_config,
            event_dates=event_dates,
            capital=capital,
            lot=lot,
            step=step,
            slip_pct=slip_pct,
            n_configs_tried=n_configs_tried,
        )

    return restate_edge(series, haircut_series=haircut_series)


def _haircut_series(
    cycles: list[dict[str, Any]],
    *,
    mult: float,
    raven_params: RavenParams,
    gate_config: GateV2Config,
    event_dates: list[date] | None,
    capital: float,
    lot: int,
    step: int,
    slip_pct: float,
    n_configs_tried: int,
) -> CycleSeries | None:
    """Re-run RAVEN at ``mult``× IV (e.g. 0.80×) for the IV_HAIRCUT survival gate.

    Scales the flat ATM IV (``atm_straddle_iv`` / ``straddle_iv``), the term-
    structure ``next_iv``, AND (when present) the per-strike IV of EVERY cycle by
    ``mult``, then re-gates + re-runs RAVEN. The flat-IV scale narrows the credit
    AND moves the gate (less VRP); the per-strike-IV scale flows into the
    re-priced legs via the engine's per-cycle skew bridge (so under a real skew
    map the haircut also stresses the skew-aware strike placement, not just the
    flat credit). Returns ``None`` when the haircut leaves no gated cycles (the
    IV_HAIRCUT gate then fails closed, which is the honest outcome).
    """
    cut = [_scale_cycle_iv(c, mult) for c in cycles]
    gated_cut, _v = gate_cycles_v2(cut, cfg=gate_config, event_dates=event_dates)
    if not gated_cut:
        return None
    pdict: dict[str, Any] = {
        "put_short_delta": raven_params.put_short_delta,
        "call_short_delta": raven_params.call_short_delta,
        "put_wing_strikes": raven_params.put_wing_strikes,
        "call_wing_strikes": raven_params.call_wing_strikes,
    }
    metrics = run_strategy_backtest(
        RAVEN_SPEC, gated_cut, pdict,
        capital=capital, lot=lot, step=step, slip_pct=slip_pct, gate="none",
    )
    trades = metrics.get("trades", [])
    if not trades:
        return None
    return _strategy_series(
        trades,
        capital=capital,
        n_configs_tried=n_configs_tried,
    )


def _scale_cycle_iv(cycle: dict[str, Any], mult: float) -> dict[str, Any]:
    """Return a copy of ``cycle`` with every IV field scaled by ``mult``.

    Scales ``atm_straddle_iv`` / ``straddle_iv`` (whichever resolve to the flat
    IV) and each value of ``per_strike_iv`` so the haircut hits BOTH the credit
    and the gate. Non-IV fields pass through untouched.
    """
    out = dict(cycle)
    for key in ("atm_straddle_iv", "straddle_iv", "next_iv"):
        v = out.get(key)
        if v is not None:
            try:
                out[key] = float(v) * mult
            except (TypeError, ValueError):
                pass
    psi = out.get("per_strike_iv")
    if isinstance(psi, dict):
        scaled: dict[int, float] = {}
        for k, v in psi.items():
            try:
                scaled[int(k)] = float(v) * mult
            except (TypeError, ValueError):
                continue
        out["per_strike_iv"] = scaled
    return out


# ===========================================================================
# Reporting
# ===========================================================================
def _iv_proxy_note(result: RavenResult) -> str:
    real = result.delta_iv_sources.get(DELTA_IV_SOURCE_REAL, 0)
    flat = result.delta_iv_sources.get(DELTA_IV_SOURCE_FLAT, 0)
    total = real + flat
    if total == 0:
        return "skew IV: no priced cycles"
    if flat == 0:
        return f"skew IV: all {total} on REAL per-strike IV (skew SEEN)"
    return (
        f"skew IV: {real}/{total} real per-strike, {flat} on flat-IV fallback "
        "(skew-blind — asymmetry is delta-only; edge UNVERIFIED)"
    )


def render_report(result: RavenResult) -> str:
    """Human-readable RAVEN report — both legs + the honest restate verdict."""
    rm = result.raven_metrics
    bm = result.baseline_metrics
    restate = result.restate

    def _g(m: dict[str, Any], key: str, default: Any = 0.0) -> Any:
        return m.get(key, default)

    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("RAVEN — regime-adaptive skew-aware asymmetric condor (PRELIMINARY / PAPER)")
    lines.append("=" * 78)
    lines.append(
        f"input cycles={result.n_input_cycles}  "
        f"GATE-V2 SELL_PREMIUM cycles={result.n_gated_cycles}"
    )
    lines.append(
        f"RAVEN params: put_short_Δ={result.raven_params.put_short_delta:.2f} "
        f"call_short_Δ={result.raven_params.call_short_delta:.2f} "
        f"put_wing={result.raven_params.put_wing_strikes} "
        f"call_wing={result.raven_params.call_wing_strikes}"
    )
    lines.append(_iv_proxy_note(result))
    lines.append("-" * 78)
    hdr = f"{'leg':<26}{'n':>5}{'net_pnl':>14}{'ROM':>9}{'win':>7}{'sharpe':>9}"
    lines.append(hdr)
    for label, m in (("RAVEN (asymmetric)", rm), ("symmetric baseline", bm)):
        lines.append(
            f"{label:<26}{_g(m, 'n_trades', 0):>5}"
            f"{_g(m, 'net_pnl'):>14,.0f}"
            f"{_g(m, 'return_on_margin'):>9.2%}"
            f"{_g(m, 'win_rate'):>7.0%}"
            f"{_g(m, 'sharpe'):>9.2f}"
        )
    lines.append("-" * 78)
    lines.append(f"HONEST RESTATE: {restate.get('verdict', 'n/a')}")
    lines.append("=" * 78)
    return "\n".join(lines)


# ===========================================================================
# CLI
# ===========================================================================
def _register_specs() -> None:
    """Expose RAVEN in the shared strategy registry (idempotent, non-clobbering).

    Lets ``python -m research.backtest.fno_strategies --strategy raven`` also see
    RAVEN without editing fno_strategies.py. Never overwrites an existing key.
    """
    FNO_STRATEGIES.setdefault(RAVEN_STRATEGY_NAME, RAVEN_SPEC)
    FNO_STRATEGIES.setdefault(BASELINE_SPEC.name, BASELINE_SPEC)


def main() -> None:  # pragma: no cover — needs the dhan_trading DB
    """Entry point for ``python -m research.backtest.fno_raven``."""
    parser = argparse.ArgumentParser(
        description="RAVEN — regime-adaptive skew-aware asymmetric condor backtest",
    )
    parser.add_argument("--mode", default="weekly", choices=["weekly", "expiry_calendar"])
    parser.add_argument("--capital", type=float, default=200_000.0)
    parser.add_argument("--k", type=float, default=0.9, help="v1 VRP threshold inside GATE-V2")
    parser.add_argument("--entry-dte", type=int, default=None, dest="entry_dte")
    parser.add_argument("--put-short-delta", type=float, default=DEFAULT_PUT_SHORT_DELTA)
    parser.add_argument("--call-short-delta", type=float, default=DEFAULT_CALL_SHORT_DELTA)
    parser.add_argument("--put-wing-strikes", type=int, default=DEFAULT_PUT_WING_STRIKES)
    parser.add_argument("--call-wing-strikes", type=int, default=DEFAULT_CALL_WING_STRIKES)
    parser.add_argument("--baseline-delta", type=float, default=DEFAULT_BASELINE_DELTA)
    parser.add_argument("--baseline-wing-strikes", type=int, default=DEFAULT_BASELINE_WING_STRIKES)
    parser.add_argument(
        "--n-configs-tried", type=int, default=1, dest="n_configs_tried",
        help="trials count for the deflated-Sharpe haircut",
    )
    parser.add_argument(
        "--no-haircut", action="store_true",
        help="skip the 0.80× IV haircut survival gate (it then fails closed)",
    )
    args = parser.parse_args()

    _register_specs()

    # Initialise the DB only after args are parsed so --help never touches it.
    from config import get_config  # noqa: PLC0415
    from db import init_db  # noqa: PLC0415

    from research.backtest.fno_condor import cycles_from_db  # noqa: PLC0415

    init_db(get_config().db_url)
    cycles = cycles_from_db(mode=args.mode, entry_dte=args.entry_dte)

    rp = RavenParams(
        put_short_delta=args.put_short_delta,
        call_short_delta=args.call_short_delta,
        put_wing_strikes=args.put_wing_strikes,
        call_wing_strikes=args.call_wing_strikes,
    )
    cfg = GateV2Config(k=args.k)

    result = run_raven_backtest(
        cycles,
        raven_params=rp,
        gate_config=cfg,
        capital=args.capital,
        baseline_delta=args.baseline_delta,
        baseline_wing_strikes=args.baseline_wing_strikes,
        n_configs_tried=args.n_configs_tried,
        haircut_iv_mult=None if args.no_haircut else 0.80,
    )
    print(render_report(result))
    print(f"\nCAVEATS: {CAVEATS}")


if __name__ == "__main__":  # pragma: no cover
    main()
