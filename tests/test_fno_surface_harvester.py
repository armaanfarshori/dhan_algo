"""Tests for research/backtest/fno_surface_harvester.py — vega-balanced surface
harvester (Skew-Lean Diagonal + RAVEN↔diagonal regime router).

Pure-function tests only. The module's single DB touch (``cycles_from_db`` in the
CLI) is never exercised here; everything is driven with synthetic in-memory cycles.

TZ-safe: every date is an explicit literal (no date.today()/now() for injected
state), so the suite is deterministic on a UTC CI runner as well as the IST dev
Mac (see memory `ci-ist-date-trap`).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

try:
    from ml.fno_vol_gate import GateV2Config
    from research.backtest.fno_condor import (
        DELTA_IV_SOURCE_FLAT,
        DELTA_IV_SOURCE_REAL,
    )
    from research.backtest.fno_surface_harvester import (
        EXTREME_IV_CEIL,
        EXTREME_IV_FLOOR,
        MIN_TERM_GAP_DAYS,
        ROUTE_DIAGONAL,
        ROUTE_RAVEN,
        ROUTE_STAND_ASIDE,
        DiagonalParams,
        DiagonalTrade,
        _b76_vega_per_unit,
        _diagonal_margin,
        _iv_legible,
        _leg_intrinsic,
        _PitTrailingSpreads,
        _resolve_far_dte,
        build_skew_lean_diagonal,
        gate_diagonal_cycles,
        price_and_resolve_diagonal,
        regime_route,
        render_report,
        route_cycles,
        run_diagonal_backtest,
        run_surface_harvester_backtest,
    )
    from research.backtest.fno_costs import NIFTY_LOT

    _HAS_MOD = True
    _import_error = ""
except Exception as _e:  # noqa: BLE001
    _HAS_MOD = False
    _import_error = str(_e)

pytestmark = pytest.mark.skipif(
    not _HAS_MOD, reason=f"fno_surface_harvester not importable: {_import_error!r}"
)

# Advisory percentile config: history accrues slowly in synthetic sets; turning the
# percentile sub-gate advisory lets the v1 VRP spine drive the SELL decision
# deterministically without look-ahead concerns.
_ADVISORY = GateV2Config(require_percentile=False)


# ---------------------------------------------------------------------------
# Synthetic cycle helpers
# ---------------------------------------------------------------------------
def _backwardation_cycle(
    entry: date,
    *,
    spot: float = 20000.0,
    front: float = 0.30,
    nxt: float = 0.20,
    rvol: float = 0.10,
    near_dte: int = 7,
    far_gap: int = 7,
) -> dict:
    """A single backwardation cycle (front IV > next IV) with a 2-expiry term."""
    return {
        "entry_date": entry,
        "expiry_date": entry + timedelta(days=near_dte),
        "next_expiry_date": entry + timedelta(days=near_dte + far_gap),
        "spot": spot,
        "straddle_iv": front,
        "atm_straddle_iv": front,
        "next_iv": nxt,
        "realized_vol_20d": rvol,
        "dte": near_dte,
        "expiry_spot": spot - 200.0,
    }


def _contango_cycle(
    entry: date,
    *,
    spot: float = 20000.0,
    front: float = 0.14,
    nxt: float = 0.18,
    rvol: float = 0.06,
    near_dte: int = 7,
) -> dict:
    """A single contango cycle (front IV < next IV), realized << implied → SELL."""
    return {
        "entry_date": entry,
        "expiry_date": entry + timedelta(days=near_dte),
        "next_expiry_date": entry + timedelta(days=near_dte + 7),
        "spot": spot,
        "straddle_iv": front,
        "atm_straddle_iv": front,
        "next_iv": nxt,
        "realized_vol_20d": rvol,
        "dte": near_dte,
        "expiry_spot": spot + 30.0,
    }


def _mixed_cycles(n: int = 40, *, backwardation_every: int = 4) -> list[dict]:
    """n chronological weekly cycles; every ``backwardation_every``-th is inverted."""
    base = date(2026, 1, 6)
    cs = []
    for i in range(n):
        ed = base + timedelta(days=7 * i)
        if i % backwardation_every == 0:
            cs.append(_backwardation_cycle(ed, spot=20000.0 + (i % 5) * 50))
        else:
            cs.append(_contango_cycle(ed, spot=20000.0 + (i % 5) * 50))
    return cs


def _skew_per_strike_iv(spot=20000.0, step=50, *, put_iv=0.34, call_iv=0.16):
    """A rich PUT-skew per-strike IV map: OTM puts >> OTM calls."""
    psi = {}
    k = int(round((spot - 4000) / step)) * step
    top = int(round((spot + 4000) / step)) * step
    while k <= top:
        psi[k] = put_iv if k < spot else call_iv
        k += step
    return psi


# ===========================================================================
# 1. Diagonal construction — strikes, defined-risk, long-vega sign
# ===========================================================================
def test_pe_diagonal_long_leg_further_otm_lower():
    """PUT diagonal: the long far-leg sits LOWER (further OTM) than the short near."""
    strikes, _src = build_skew_lean_diagonal(20000.0, 0.30, 7, params=DiagonalParams())
    assert strikes["option_type"] == "PE"
    assert strikes["long_far_k"] < strikes["short_near_k"]
    # offset is 2 grid steps by default
    assert strikes["short_near_k"] - strikes["long_far_k"] == 2 * 50


def test_ce_diagonal_long_leg_further_otm_higher():
    """CALL diagonal: the long far-leg sits HIGHER (further OTM) than the short near."""
    strikes, _src = build_skew_lean_diagonal(
        20000.0, 0.30, 7, params=DiagonalParams(option_type="CE"),
    )
    assert strikes["option_type"] == "CE"
    assert strikes["long_far_k"] > strikes["short_near_k"]


def test_diagonal_pure_calendar_same_strike():
    """offset 0 ⇒ a pure calendar: short near and long far share the strike."""
    strikes, _src = build_skew_lean_diagonal(
        20000.0, 0.30, 7, params=DiagonalParams(long_far_offset_strikes=0),
    )
    assert strikes["long_far_k"] == strikes["short_near_k"]


def test_diagonal_rejects_nonpositive_near_dte():
    """At/after near-expiry the delta degenerates — reject near_dte <= 0."""
    with pytest.raises(ValueError):
        build_skew_lean_diagonal(20000.0, 0.30, 0, params=DiagonalParams())
    with pytest.raises(ValueError):
        build_skew_lean_diagonal(20000.0, 0.30, -3, params=DiagonalParams())


def test_diagonal_params_validation():
    """DiagonalParams rejects bad option_type / delta / offset."""
    with pytest.raises(ValueError):
        DiagonalParams(option_type="XX")
    with pytest.raises(ValueError):
        DiagonalParams(short_near_delta=0.0)
    with pytest.raises(ValueError):
        DiagonalParams(short_near_delta=1.0)
    with pytest.raises(ValueError):
        DiagonalParams(long_far_offset_strikes=-1)


def test_diagonal_is_net_long_vega():
    """The diagonal must be NET LONG VEGA (far-leg vega > near-leg vega)."""
    cyc = _backwardation_cycle(date(2026, 1, 6))
    t = price_and_resolve_diagonal(cyc, params=DiagonalParams())
    assert t is not None
    assert t.long_vega > 0.0, "skew-lean diagonal must be net long vega"


def test_diagonal_defined_risk_margin_bounded():
    """Margin (capital at risk) is finite, positive, and bounded by the strike gap
    / net-debit cap — never unbounded."""
    cyc = _backwardation_cycle(date(2026, 1, 6))
    t = price_and_resolve_diagonal(cyc, params=DiagonalParams())
    assert t is not None
    assert t.margin > 0.0
    # The defined-risk margin must be the max of the debit cap and the strike-gap
    # cap (with a floor). It can never exceed |near-far| gap + the debit jointly
    # blowing up; for a 2-step PE diagonal that is a bounded number.
    gap = abs(t.strikes["short_near_k"] - t.strikes["long_far_k"])
    assert t.margin >= gap  # spread cap is per-unit×lot; margin >= gap (lot >= 1)


def test_diagonal_real_per_strike_iv_path_flag():
    """With a real per-strike IV map the short near strike is priced REAL (skew SEEN)."""
    psi = _skew_per_strike_iv()
    _strikes, src = build_skew_lean_diagonal(
        20000.0, 0.30, 7, params=DiagonalParams(), per_strike_iv=psi,
    )
    assert src == DELTA_IV_SOURCE_REAL


def test_diagonal_flat_iv_fallback_flag():
    """With NO per-strike IV the short near strike falls back to flat IV (skew-blind)."""
    _strikes, src = build_skew_lean_diagonal(20000.0, 0.30, 7, params=DiagonalParams())
    assert src == DELTA_IV_SOURCE_FLAT


# ===========================================================================
# 2. Single-cycle pricing/resolution — data honesty
# ===========================================================================
def test_diagonal_forward_paper_when_no_next_iv():
    """Missing next_iv ⇒ FORWARD-PAPER (not backtestable) ⇒ None (never fabricated)."""
    cyc = _backwardation_cycle(date(2026, 1, 6))
    cyc.pop("next_iv")
    assert price_and_resolve_diagonal(cyc, params=DiagonalParams()) is None


def test_diagonal_forward_paper_when_no_term_gap():
    """A far DTE not exceeding the near DTE by MIN_TERM_GAP_DAYS ⇒ not a diagonal."""
    cyc = _backwardation_cycle(date(2026, 1, 6), near_dte=7)
    # Force the far expiry to be only 1 day past the near → gap < MIN_TERM_GAP_DAYS.
    cyc["next_dte"] = 7 + (MIN_TERM_GAP_DAYS - 1)
    assert price_and_resolve_diagonal(cyc, params=DiagonalParams()) is None


def test_diagonal_explicit_next_dte_used():
    """An explicit next_dte sets the far leg; far_dte must exceed near_dte."""
    cyc = _backwardation_cycle(date(2026, 1, 6), near_dte=7)
    cyc["next_dte"] = 21
    t = price_and_resolve_diagonal(cyc, params=DiagonalParams())
    assert t is not None
    assert t.far_dte == 21
    assert t.near_dte == 7


def test_diagonal_skips_degenerate_iv():
    """Non-positive / missing core inputs ⇒ None (fail-open)."""
    cyc = _backwardation_cycle(date(2026, 1, 6))
    cyc["next_iv"] = 0.0
    assert price_and_resolve_diagonal(cyc, params=DiagonalParams()) is None
    cyc2 = _backwardation_cycle(date(2026, 1, 6))
    cyc2["spot"] = 0.0
    assert price_and_resolve_diagonal(cyc2, params=DiagonalParams()) is None


def test_diagonal_trade_fields_consistent():
    """net_pnl = gross − costs; win = net > 0; record carries both expiries."""
    cyc = _backwardation_cycle(date(2026, 1, 6))
    t = price_and_resolve_diagonal(cyc, params=DiagonalParams())
    assert isinstance(t, DiagonalTrade)
    assert t.net_pnl == pytest.approx(t.gross_pnl - t.costs)
    assert t.win == (t.net_pnl > 0)
    assert t.near_expiry_date < t.far_expiry_date
    # Costs are the statutory + brokerage stack — must be strictly positive (a real
    # 4-execution-leg diagonal always pays ≥ ₹80 brokerage + STT/exchange/GST).
    assert t.costs > 0.0


# ===========================================================================
# 3. Regime routing — contango→RAVEN, backwardation→diagonal, event→aside
# ===========================================================================
def test_route_backwardation_to_diagonal():
    cyc = _backwardation_cycle(date(2026, 1, 6))
    d = regime_route(cyc, cfg=_ADVISORY)
    assert d.route == ROUTE_DIAGONAL
    assert d.backwardation is True


def test_route_contango_sell_to_raven():
    cyc = _contango_cycle(date(2026, 1, 6))
    d = regime_route(cyc, cfg=_ADVISORY)
    assert d.route == ROUTE_RAVEN
    assert d.backwardation is False


def test_route_event_day_to_stand_aside():
    """An event-date veto routes to STAND_ASIDE even in backwardation."""
    entry = date(2026, 1, 6)
    cyc = _backwardation_cycle(entry)
    d = regime_route(cyc, cfg=_ADVISORY, event_dates=[entry])
    assert d.route == ROUTE_STAND_ASIDE
    assert d.event is True


def test_route_expiry_day_to_stand_aside():
    """Entering ON expiry day is an event veto → STAND_ASIDE."""
    entry = date(2026, 1, 6)
    cyc = _backwardation_cycle(entry)
    cyc["expiry_date"] = entry  # entry == expiry
    d = regime_route(cyc, cfg=_ADVISORY)
    assert d.route == ROUTE_STAND_ASIDE


def test_route_missing_front_iv_fail_open():
    """No usable front IV ⇒ STAND_ASIDE (fail-open, never an aggressive trade)."""
    cyc = _contango_cycle(date(2026, 1, 6))
    cyc.pop("straddle_iv")
    cyc.pop("atm_straddle_iv")
    d = regime_route(cyc, cfg=_ADVISORY)
    assert d.route == ROUTE_STAND_ASIDE


def test_route_extreme_iv_to_stand_aside():
    """An illegible / extreme front IV (data error) ⇒ STAND_ASIDE."""
    cyc = _contango_cycle(date(2026, 1, 6))
    cyc["straddle_iv"] = 99.0
    cyc["atm_straddle_iv"] = 99.0
    d = regime_route(cyc, cfg=_ADVISORY)
    assert d.route == ROUTE_STAND_ASIDE


def test_route_contango_no_sell_to_stand_aside():
    """Contango but GATE-V2 does NOT say SELL (realized ≈ implied) → STAND_ASIDE."""
    cyc = _contango_cycle(date(2026, 1, 6), front=0.14, rvol=0.16)  # rv >= iv → not SELL
    d = regime_route(cyc, cfg=_ADVISORY)
    assert d.route == ROUTE_STAND_ASIDE


def test_route_cycles_buckets_and_pit():
    """route_cycles buckets by route and the trailing series is PIT (no look-ahead)."""
    cycles = _mixed_cycles(40, backwardation_every=4)
    buckets, decisions = route_cycles(cycles, cfg=_ADVISORY)
    assert len(decisions) == 40
    assert sum(len(v) for v in buckets.values()) == 40
    # 10 backwardation cycles (every 4th) route to the diagonal.
    assert len(buckets[ROUTE_DIAGONAL]) == 10
    # The rest are contango-SELL → RAVEN (none stood aside in this clean set).
    assert len(buckets[ROUTE_RAVEN]) == 30


# ===========================================================================
# 4. gate_diagonal_cycles — backwardation-only deploy set
# ===========================================================================
def test_gate_diagonal_only_backwardation():
    cycles = _mixed_cycles(40, backwardation_every=4)
    deploy, skipped = gate_diagonal_cycles(cycles, cfg=_ADVISORY)
    assert len(deploy) == 10
    assert len(skipped) == 30
    # Every deployed cycle is genuinely in backwardation (front > next).
    for c in deploy:
        assert c["straddle_iv"] > c["next_iv"]


def test_gate_diagonal_skips_missing_next_iv():
    """A cycle with no next_iv can't be classed backwardation → skipped."""
    cyc = _backwardation_cycle(date(2026, 1, 6))
    cyc.pop("next_iv")
    deploy, skipped = gate_diagonal_cycles([cyc], cfg=_ADVISORY)
    assert deploy == []
    assert len(skipped) == 1


# ===========================================================================
# 5. Diagonal backtest — backtestable vs forward-paper accounting
# ===========================================================================
def test_run_diagonal_backtest_counts():
    """Backtestable diagonal cycles are priced; missing-next_iv ones are FORWARD-PAPER."""
    base = date(2026, 1, 6)
    cycles = []
    for i in range(12):
        ed = base + timedelta(days=7 * i)
        c = _backwardation_cycle(ed)
        if i >= 8:
            c.pop("next_iv")  # last 4 are forward-paper-only
        cycles.append(c)
    res = run_diagonal_backtest(cycles, params=DiagonalParams(), gate_config=_ADVISORY)
    assert res.n_backwardation_cycles == 8  # only those WITH next_iv are backwardation
    assert res.n_backtestable == 8
    assert res.n_forward_paper_only == 0  # the 4 without next_iv never reach the gate
    assert res.metrics["n_trades"] == 8
    assert "verdict" in res.restate


def test_run_diagonal_backtest_empty_set():
    """No backwardation cycles ⇒ empty, stable NO-GO restate (no KeyError)."""
    cycles = [_contango_cycle(date(2026, 1, 6) + timedelta(days=7 * i)) for i in range(10)]
    res = run_diagonal_backtest(cycles, params=DiagonalParams(), gate_config=_ADVISORY)
    assert res.n_backtestable == 0
    assert res.metrics["n_trades"] == 0
    assert res.restate["overall_pass"] is False


def test_run_diagonal_forward_paper_when_no_term():
    """Backwardation but no real term gap ⇒ counted FORWARD-PAPER, not backtested."""
    base = date(2026, 1, 6)
    cycles = []
    for i in range(6):
        ed = base + timedelta(days=7 * i)
        c = _backwardation_cycle(ed, near_dte=7)
        c["next_dte"] = 7 + (MIN_TERM_GAP_DAYS - 1)  # gap too small
        cycles.append(c)
    res = run_diagonal_backtest(cycles, params=DiagonalParams(), gate_config=_ADVISORY)
    assert res.n_backwardation_cycles == 6
    assert res.n_backtestable == 0
    assert res.n_forward_paper_only == 6


# ===========================================================================
# 6. Full router backtest — restate comparison vs baselines
# ===========================================================================
def test_harvester_backtest_runs_and_buckets():
    cycles = _mixed_cycles(40, backwardation_every=4)
    res = run_surface_harvester_backtest(cycles, cfg=_ADVISORY, capital=200_000.0)
    assert res.n_input_cycles == 40
    assert res.route_counts[ROUTE_DIAGONAL] == 10
    assert res.route_counts[ROUTE_RAVEN] == 30
    assert res.diagonal_result.n_backtestable == 10


def test_harvester_restate_keys_present():
    """All three restates carry the stable key set (router + both baselines)."""
    cycles = _mixed_cycles(40, backwardation_every=4)
    res = run_surface_harvester_backtest(cycles, cfg=_ADVISORY)
    for r in (res.restate_router, res.restate_always_raven, res.restate_always_condor):
        assert "verdict" in r
        assert "overall_pass" in r
        assert "gates" in r
        # Verdict is a real string + overall_pass is a real bool (not a stub).
        assert isinstance(r["verdict"], str) and r["verdict"]
        assert isinstance(r["overall_pass"], bool)
        assert ("GO" in r["verdict"]) or ("NO-GO" in r["verdict"])


def test_harvester_router_combines_both_legs():
    """The router restate's n equals RAVEN trades + backtested diagonal trades."""
    cycles = _mixed_cycles(40, backwardation_every=4)
    res = run_surface_harvester_backtest(cycles, cfg=_ADVISORY)
    n_raven = res.raven_result.raven_metrics.get("n_trades", 0)
    n_diag = res.diagonal_result.n_backtestable
    assert res.restate_router["n"] == n_raven + n_diag


def test_harvester_all_contango_no_diagonal():
    """An all-contango universe → zero diagonal cycles, router == RAVEN leg only."""
    cycles = [_contango_cycle(date(2026, 1, 6) + timedelta(days=7 * i)) for i in range(30)]
    res = run_surface_harvester_backtest(cycles, cfg=_ADVISORY)
    assert res.route_counts[ROUTE_DIAGONAL] == 0
    assert res.diagonal_result.n_backtestable == 0


def test_harvester_empty_input():
    """No cycles → stable NO-GO restates, no crash."""
    res = run_surface_harvester_backtest([], cfg=_ADVISORY)
    assert res.n_input_cycles == 0
    assert res.restate_router["overall_pass"] is False


def test_render_report_smoke():
    """render_report produces a string mentioning both legs + all three restates."""
    cycles = _mixed_cycles(40, backwardation_every=4)
    res = run_surface_harvester_backtest(cycles, cfg=_ADVISORY)
    rep = render_report(res)
    assert "SURFACE HARVESTER" in rep
    assert "DIAGONAL" in rep
    assert "RAVEN" in rep
    assert "ROUTER restate" in rep
    assert "ALWAYS-RAVEN" in rep
    assert "ALWAYS-CONDOR" in rep


# ===========================================================================
# 7. Index-agnostic — lot/step are parameters
# ===========================================================================
def test_diagonal_index_agnostic_step():
    """A non-NIFTY step (e.g. 100) flows through to the strike grid."""
    strikes, _src = build_skew_lean_diagonal(
        45000.0, 0.30, 7, params=DiagonalParams(), step=100,
    )
    assert strikes["short_near_k"] % 100 == 0
    assert strikes["short_near_k"] - strikes["long_far_k"] == 2 * 100


def test_diagonal_index_agnostic_lot():
    """A non-NIFTY lot scales the ₹ P&L + margin linearly."""
    cyc = _backwardation_cycle(date(2026, 1, 6))
    t1 = price_and_resolve_diagonal(cyc, params=DiagonalParams(), lot=15, step=50)
    t2 = price_and_resolve_diagonal(cyc, params=DiagonalParams(), lot=30, step=50)
    assert t1 is not None and t2 is not None
    # Gross P&L scales linearly with lot (costs differ slightly, so check gross).
    assert t2.gross_pnl == pytest.approx(2.0 * t1.gross_pnl, rel=1e-9)


# ===========================================================================
# 8. Helper-level unit tests (math + fail-open primitives)
# ===========================================================================
def test_iv_legible_bounds_and_invalid():
    """_iv_legible accepts the modellable band, rejects extremes / non-finite / junk."""
    assert _iv_legible(EXTREME_IV_FLOOR) is True
    assert _iv_legible(EXTREME_IV_CEIL) is True
    assert _iv_legible(0.20) is True
    assert _iv_legible(EXTREME_IV_FLOOR / 2) is False   # below floor
    assert _iv_legible(EXTREME_IV_CEIL + 1) is False     # above ceil
    assert _iv_legible(None) is False
    assert _iv_legible("oops") is False
    assert _iv_legible(float("nan")) is False
    assert _iv_legible(float("inf")) is False


def test_leg_intrinsic_put_and_call():
    """_leg_intrinsic: PE = max(K−S,0), CE = max(S−K,0), boundary 0."""
    assert _leg_intrinsic("PE", 100.0, 80.0) == 20.0   # ITM put
    assert _leg_intrinsic("PE", 100.0, 120.0) == 0.0   # OTM put
    assert _leg_intrinsic("CE", 100.0, 120.0) == 20.0  # ITM call
    assert _leg_intrinsic("CE", 100.0, 80.0) == 0.0    # OTM call
    assert _leg_intrinsic("PE", 100.0, 100.0) == 0.0   # ATM boundary


def test_b76_vega_non_negative_and_zero_at_expiry():
    """Vega is >= 0 and degenerates to 0 at/after expiry or non-positive inputs."""
    assert _b76_vega_per_unit(20000.0, 20000.0, 7, 0.20) > 0.0
    assert _b76_vega_per_unit(20000.0, 20000.0, 0, 0.20) == 0.0   # T=0
    assert _b76_vega_per_unit(20000.0, 20000.0, 7, 0.0) == 0.0    # sigma=0
    assert _b76_vega_per_unit(0.0, 20000.0, 7, 0.20) == 0.0       # spot<=0
    # Longer-dated ATM option has MORE vega than a shorter-dated one (√T scaling).
    assert _b76_vega_per_unit(20000.0, 20000.0, 30, 0.20) > _b76_vega_per_unit(
        20000.0, 20000.0, 7, 0.20
    )


def test_resolve_far_dte_paths():
    """_resolve_far_dte: explicit next_dte → next_expiry_date → assumed gap; reject <= near."""
    ed = date(2026, 1, 6)
    # explicit next_dte
    assert _resolve_far_dte({"next_dte": 21}, 7) == 21
    # next_dte not exceeding near → None
    assert _resolve_far_dte({"next_dte": 7}, 7) is None
    # derived from next_expiry_date
    assert _resolve_far_dte(
        {"entry_date": ed, "next_expiry_date": ed + timedelta(days=14)}, 7
    ) == 14
    # neither → assumed one-front gap, floored to a real term gap
    far = _resolve_far_dte({}, 7)
    assert far is not None and (far - 7) >= MIN_TERM_GAP_DAYS


def test_diagonal_margin_debit_credit_and_floor():
    """_diagonal_margin = max(debit_cap, spread_cap, step*lot)."""
    lot, step = 50, 50
    near_k, far_k = 19650.0, 19550.0  # gap 100
    # Net DEBIT of 5/unit → debit_cap=250, spread_cap=5000 → spread dominates.
    m_debit = _diagonal_margin("PE", near_k, far_k, -5.0, lot, 2, step)
    assert m_debit == pytest.approx(max(5.0 * lot, 100.0 * lot, step * lot))
    # Pure calendar (same strike) net credit → spread_cap=0, debit_cap=0 → floor.
    m_cal = _diagonal_margin("PE", near_k, near_k, 5.0, lot, 0, step)
    assert m_cal == pytest.approx(step * lot)


# ===========================================================================
# 9. PIT trailing-spread accumulator — no look-ahead
# ===========================================================================
def test_pit_trailing_spreads_no_look_ahead():
    """snapshot() returns only spreads from STRICTLY earlier observe() calls."""
    acc = _PitTrailingSpreads(_ADVISORY)
    assert acc.snapshot() == []  # empty before any observation
    c1 = _contango_cycle(date(2026, 1, 6))
    acc.observe(c1)
    assert len(acc.snapshot()) == 1  # the current cycle is observed AFTER its snapshot
    c2 = _contango_cycle(date(2026, 1, 13))
    acc.observe(c2)
    assert len(acc.snapshot()) == 2


def test_pit_trailing_spreads_missing_data_appends_none():
    """A cycle with no IV / realized vol contributes None (the gate filters it)."""
    acc = _PitTrailingSpreads(_ADVISORY)
    bad = {"entry_date": date(2026, 1, 6)}  # no IV, no rvol
    acc.observe(bad)
    assert acc.snapshot() == [None]


# ===========================================================================
# 10. Robustness — fail-open on corrupt rows (no crash)
# ===========================================================================
def test_diagonal_fail_open_on_infinite_dte():
    """A corrupt cycle (dte=inf) fails open (None), never raises OverflowError."""
    cyc = _backwardation_cycle(date(2026, 1, 6))
    cyc["dte"] = float("inf")
    assert price_and_resolve_diagonal(cyc, params=DiagonalParams()) is None


def test_diagonal_fail_open_on_nan_iv():
    """A NaN IV fails open (None), never poisons the P&L with NaN."""
    cyc = _backwardation_cycle(date(2026, 1, 6))
    cyc["next_iv"] = float("nan")
    assert price_and_resolve_diagonal(cyc, params=DiagonalParams()) is None


# ===========================================================================
# 11. Vega-reversed honesty accounting
# ===========================================================================
def test_diagonal_backtest_flags_vega_reversed():
    """When the far IV is far BELOW the front (deep offset), the diagonal can be
    NOT net long vega — those trades are counted in n_vega_reversed (not hidden)."""
    base = date(2026, 1, 6)
    # front >> next + a deep far offset → far-leg vega can fall below near-leg vega.
    cycles = []
    for i in range(6):
        ed = base + timedelta(days=7 * i)
        cycles.append(
            _backwardation_cycle(ed, front=0.60, nxt=0.08, near_dte=14, far_gap=3)
        )
    params = DiagonalParams(option_type="CE", long_far_offset_strikes=3)
    res = run_diagonal_backtest(cycles, params=params, gate_config=_ADVISORY)
    # All are backwardation + backtestable; the long-vega thesis is violated here.
    assert res.n_backtestable == 6
    assert res.n_vega_reversed >= 1
    rep_trade = res.trades[0]
    assert rep_trade.long_vega <= 0  # confirm the diagnostic fired honestly


def test_diagonal_default_is_long_vega():
    """The DEFAULT config (PE, offset 2, near_delta 0.30) IS net long vega on a
    normal backwardation cycle — no reversal flagged."""
    cycles = [
        _backwardation_cycle(date(2026, 1, 6) + timedelta(days=7 * i)) for i in range(8)
    ]
    res = run_diagonal_backtest(cycles, params=DiagonalParams(), gate_config=_ADVISORY)
    assert res.n_backtestable == 8
    assert res.n_vega_reversed == 0


def test_diagonal_delta_iv_source_counts():
    """delta_iv_sources counts real-per-strike vs flat-fallback priced cycles."""
    psi = _skew_per_strike_iv()
    cycles = []
    for i in range(6):
        ed = date(2026, 1, 6) + timedelta(days=7 * i)
        c = _backwardation_cycle(ed)
        if i < 3:
            c["per_strike_iv"] = dict(psi)  # first 3 priced on real skew
        cycles.append(c)
    res = run_diagonal_backtest(cycles, params=DiagonalParams(), gate_config=_ADVISORY)
    assert res.delta_iv_sources[DELTA_IV_SOURCE_REAL] == 3
    assert res.delta_iv_sources[DELTA_IV_SOURCE_FLAT] == 3


def test_lot_constant_imported():
    """Sanity: NIFTY_LOT default is the documented 65 (index-agnostic default)."""
    assert NIFTY_LOT == 65
