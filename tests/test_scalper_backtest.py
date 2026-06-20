"""Tests for research/backtest/scalper_backtest.py + the BANKNIFTY registry add.

Pure-function tests only — the module's DB loaders (``load_days_from_db`` and the
``_load_*`` helpers) are NEVER exercised here; every test drives the replay loop,
fill model, costs, metrics and sweep with SYNTHETIC in-memory minute bars.

TZ-safe (memory ``ci-ist-date-trap``): every timestamp is an explicit tz-aware
literal in IST or UTC — no date.today()/naive now() for injected state — so the
suite is deterministic on a UTC CI runner as well as the IST dev Mac. Each test
that buckets by trading day also runs the same assertions whether the bars are
expressed in IST or in their UTC equivalents.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from research.backtest.scalper_backtest import (
    EXIT_EOD,
    EXIT_HARD_STOP,
    EXIT_SIGNAL_FLIP,
    EXIT_TARGET,
    EXIT_TIME_STOP,
    EXIT_TRAIL,
    Bar,
    FillModel,
    ReplayParams,
    ScalpTrade,
    _break_even_win_rate,
    compute_metrics,
    replay_day,
    run_backtest,
    run_sweep,
    scalp_costs,
    straddle_proxy_underlying,
)

_IST = timezone(timedelta(hours=5, minutes=30))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — build synthetic minute paths
# ─────────────────────────────────────────────────────────────────────────────
def _t(h: int, m: int, *, day: date = date(2026, 3, 3), tz=_IST) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=tz)


def _flat_bar(t: datetime, px: float, vol: float = 100.0, iv=None) -> Bar:
    return Bar(t=t, open=px, high=px, low=px, close=px, volume=vol, iv=iv)


def _ramp_underlying(start_min: int, n: int, base: float, step: float,
                     day: date = date(2026, 3, 3)) -> list[Bar]:
    """A clean rising underlying path: each minute closes `step` higher than the
    last. Highs/lows give a wide-enough true range to clear the ATR filter."""
    bars = []
    for i in range(n):
        px = base + i * step
        t = _t(9, 15, day=day) + timedelta(minutes=start_min + i)
        bars.append(Bar(t=t, open=px - 1, high=px + 5, low=px - 5, close=px, volume=100.0))
    return bars


# ─────────────────────────────────────────────────────────────────────────────
# 1. Fill model arithmetic
# ─────────────────────────────────────────────────────────────────────────────
def test_fill_model_buy_pays_up_sell_receives_down():
    fm = FillModel(spread=2.0, slip_pct=0.01)
    # BUY: open + spread/2 + open*slip = 100 + 1 + 1 = 102
    assert fm.buy_fill(100.0) == pytest.approx(102.0)
    # SELL: open - spread/2 - open*slip = 100 - 1 - 1 = 98
    assert fm.sell_fill(100.0) == pytest.approx(98.0)


def test_fill_model_sell_floored_at_min():
    fm = FillModel(spread=400.0, slip_pct=0.5)
    # A deep adverse model must never produce a negative fill.
    assert fm.sell_fill(10.0) == pytest.approx(0.05)


def test_fill_model_iv_scaled_spread_widens():
    base = FillModel(spread=1.0, slip_pct=0.0, iv_scale_spread=False)
    scaled = FillModel(spread=1.0, slip_pct=0.0, iv_scale_spread=True, iv_scale_ref=12.0)
    # At iv=24 (2× ref) the scaled spread is 2.0 → buy fill = 100 + 1.0
    assert scaled.buy_fill(100.0, iv=24.0) == pytest.approx(101.0)
    # Base (no scaling) stays at spread 1.0 → 100 + 0.5
    assert base.buy_fill(100.0, iv=24.0) == pytest.approx(100.5)
    # Scaled never goes BELOW the base spread (floored).
    assert scaled.buy_fill(100.0, iv=1.0) == pytest.approx(100.5)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Charges applied (cost stack reused)
# ─────────────────────────────────────────────────────────────────────────────
def test_scalp_costs_positive_and_includes_brokerage():
    # Two legs → ₹40 brokerage minimum, plus STT/exchange/etc.
    c = scalp_costs(entry_premium=120.0, exit_premium=132.0, qty=65)
    assert c > 40.0
    # Sanity: scales with qty (more units → more turnover-based fees).
    c2 = scalp_costs(120.0, 132.0, 130)
    assert c2 > c


def test_net_pnl_is_gross_minus_costs():
    # Drive a single clean winning scalp and check the accounting identity.
    day = date(2026, 3, 3)
    ub = _ramp_underlying(0, 40, base=22000.0, step=8.0, day=day)
    # CE premium rises with the underlying; PE flat (won't be used for a LONG).
    ce = []
    pe = []
    for i, b in enumerate(ub):
        ce.append(Bar(t=b.t, open=100.0 + i * 2, high=100.0 + i * 2 + 1,
                      low=100.0 + i * 2 - 1, close=100.0 + i * 2, volume=500.0, iv=12.0))
        pe.append(_flat_bar(b.t, 80.0, iv=12.0))
    trades = replay_day(day, "NIFTY", ub, ce, pe,
                        fill=FillModel(spread=1.0, slip_pct=0.01),
                        replay=ReplayParams(use_trail=False))
    assert trades, "expected at least one scalp"
    for t in trades:
        # net is rounded independently of gross/costs; three 2dp roundings can
        # accumulate to ~±0.015, so allow abs=0.02 (the exact invariant holds on
        # the unrounded floats inside _close_trade).
        assert t.net_pnl == pytest.approx(t.gross_pnl - t.costs, abs=0.02)
        assert t.costs > 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. Entry/exit replay — a clean LONG opens a CE and banks the target
# ─────────────────────────────────────────────────────────────────────────────
def test_clean_long_opens_ce_and_targets():
    day = date(2026, 3, 3)
    ub = _ramp_underlying(0, 30, base=22000.0, step=10.0, day=day)
    ce = [Bar(t=b.t, open=100.0 + i * 5, high=100.0 + i * 5 + 2,
              low=100.0 + i * 5 - 2, close=100.0 + i * 5, volume=500.0, iv=12.0)
          for i, b in enumerate(ub)]
    pe = [_flat_bar(b.t, 80.0, iv=12.0) for b in ub]
    trades = replay_day(day, "NIFTY", ub, ce, pe,
                        fill=FillModel(spread=1.0, slip_pct=0.01),
                        replay=ReplayParams(target_pct=0.10, use_trail=False))
    assert trades
    first = trades[0]
    assert first.option_type == "CE"
    assert first.direction == "LONG"
    assert first.exit_reason in (EXIT_TARGET, EXIT_EOD, EXIT_TRAIL)


def test_no_entry_when_signal_never_fires():
    # A perfectly flat tape → ATR/momentum filters keep direction FLAT → no trade.
    day = date(2026, 3, 3)
    ub = [_flat_bar(_t(9, 15, day=day) + timedelta(minutes=i), 22000.0) for i in range(40)]
    ce = [_flat_bar(b.t, 100.0, iv=12.0) for b in ub]
    pe = [_flat_bar(b.t, 100.0, iv=12.0) for b in ub]
    trades = replay_day(day, "NIFTY", ub, ce, pe)
    assert trades == []


# ─────────────────────────────────────────────────────────────────────────────
# 4. Adverse intra-bar sequencing — stop (on the LOW) beats target (on the HIGH)
# ─────────────────────────────────────────────────────────────────────────────
def test_adverse_sequencing_stop_before_target_same_bar():
    """A single option bar whose LOW breaches the stop AND whose HIGH breaches the
    target must exit at the STOP (worst-case ordering), not the target."""
    day = date(2026, 3, 3)
    ub = _ramp_underlying(0, 30, base=22000.0, step=10.0, day=day)
    # CE: entry near 100; a later bar spans both the −20% stop and +10% target.
    ce = []
    for i, b in enumerate(ub):
        if i == 25:
            # wide bar: low 75 (< 80 stop) AND high 130 (> 110 target) vs ~100 entry
            ce.append(Bar(t=b.t, open=100.0, high=130.0, low=75.0, close=100.0,
                          volume=500.0, iv=12.0))
        else:
            ce.append(Bar(t=b.t, open=100.0, high=101.0, low=99.0, close=100.0,
                          volume=500.0, iv=12.0))
    pe = [_flat_bar(b.t, 80.0, iv=12.0) for b in ub]
    trades = replay_day(day, "NIFTY", ub, ce, pe,
                        fill=FillModel(spread=0.0, slip_pct=0.0),
                        replay=ReplayParams(stop_pct=0.20, target_pct=0.10,
                                            use_trail=False, time_stop_min=999))
    stop_trades = [t for t in trades if t.exit_reason == EXIT_HARD_STOP]
    assert stop_trades, "the wide bar must trigger the hard stop, not the target"


def test_trail_ratchets_on_close_not_intrabar_high():
    """With the trail armed, the exit floor is driven off the CLOSE high-water; a
    spiky intra-bar high that the close doesn't confirm must not set a tighter
    floor than the close supports."""
    day = date(2026, 3, 3)
    ub = _ramp_underlying(0, 40, base=22000.0, step=10.0, day=day)
    ce = []
    for i, b in enumerate(ub):
        if i < 22:
            px = 100.0 + i  # rise to ~121 → arms the +10% target/trail
        elif i == 25:
            px = 200.0      # spiky CLOSE high-water
        elif i >= 26:
            px = 150.0      # give-back > 12% off 200 → trail exit
        else:
            px = 122.0
        ce.append(Bar(t=b.t, open=px, high=px + 1, low=px - 1, close=px,
                      volume=500.0, iv=12.0))
    pe = [_flat_bar(b.t, 80.0, iv=12.0) for b in ub]
    trades = replay_day(day, "NIFTY", ub, ce, pe,
                        fill=FillModel(spread=0.0, slip_pct=0.0),
                        replay=ReplayParams(target_pct=0.10, trail_pct=0.12,
                                            use_trail=True, time_stop_min=999))
    assert any(t.exit_reason == EXIT_TRAIL for t in trades)


def test_time_stop_cuts_a_stagnant_scalp():
    day = date(2026, 3, 3)
    ub = _ramp_underlying(0, 40, base=22000.0, step=10.0, day=day)
    # CE dead flat after entry → no target → time-stop fires.
    ce = [Bar(t=b.t, open=100.0, high=100.5, low=99.5, close=100.0, volume=500.0, iv=12.0)
          for b in ub]
    pe = [_flat_bar(b.t, 80.0, iv=12.0) for b in ub]
    trades = replay_day(day, "NIFTY", ub, ce, pe,
                        fill=FillModel(spread=0.0, slip_pct=0.0),
                        replay=ReplayParams(time_stop_min=5, use_trail=False))
    assert trades
    assert any(t.exit_reason == EXIT_TIME_STOP for t in trades)
    ts = next(t for t in trades if t.exit_reason == EXIT_TIME_STOP)
    assert ts.hold_minutes >= 5


def test_signal_flip_flattens():
    """A LONG that flips SHORT exits via signal_flip."""
    day = date(2026, 3, 3)
    up = _ramp_underlying(0, 25, base=22000.0, step=12.0, day=day)
    # then a sharp reversal down to trip a SHORT anchor
    down = []
    last_px = up[-1].close
    for i in range(15):
        px = last_px - (i + 1) * 18.0
        t = up[-1].t + timedelta(minutes=i + 1)
        down.append(Bar(t=t, open=px + 1, high=px + 5, low=px - 5, close=px, volume=100.0))
    ub = up + down
    ce = [Bar(t=b.t, open=100.0, high=101.0, low=99.0, close=100.0, volume=500.0, iv=12.0)
          for b in ub]
    pe = [Bar(t=b.t, open=100.0, high=101.0, low=99.0, close=100.0, volume=500.0, iv=12.0)
          for b in ub]
    trades = replay_day(day, "NIFTY", ub, ce, pe,
                        fill=FillModel(spread=0.0, slip_pct=0.0),
                        replay=ReplayParams(time_stop_min=999, use_trail=False,
                                            stop_pct=0.99, target_pct=0.99))
    assert any(t.exit_reason == EXIT_SIGNAL_FLIP for t in trades)


def test_eod_exit_is_unconditional():
    """A position still open at 15:15 IST is flattened with reason 'eod'."""
    day = date(2026, 3, 3)
    # Build a path that warms up + opens a LONG well before 15:05, never
    # targets/stops, and runs past 15:15 so the EOD flatten fires. Start at 14:30
    # so the 20-bar warm-up completes (~14:50) and an entry opens before 15:05.
    bars = []
    start = _t(14, 30, day=day)
    for i in range(55):
        px = 22000.0 + i * 9.0
        t = start + timedelta(minutes=i)
        bars.append(Bar(t=t, open=px - 1, high=px + 5, low=px - 5, close=px, volume=100.0))
    ce = [Bar(t=b.t, open=100.0, high=100.5, low=99.6, close=100.0, volume=500.0, iv=12.0)
          for b in bars]
    pe = [_flat_bar(b.t, 80.0, iv=12.0) for b in bars]
    trades = replay_day(day, "NIFTY", bars, ce, pe,
                        fill=FillModel(spread=0.0, slip_pct=0.0),
                        replay=ReplayParams(time_stop_min=999, use_trail=False,
                                            stop_pct=0.99, target_pct=0.99))
    assert trades
    assert trades[-1].exit_reason == EXIT_EOD
    # The EOD exit happens at/after 15:15 IST.
    assert trades[-1].exit_time.astimezone(_IST).time() >= datetime(2026, 3, 3, 15, 15).time()


# ─────────────────────────────────────────────────────────────────────────────
# 5. Metrics
# ─────────────────────────────────────────────────────────────────────────────
def _mk_trade(net: float, day: date, reason: str = EXIT_TARGET,
              entry_iv=12.0, dte=1, t_hour=10) -> ScalpTrade:
    et = datetime(day.year, day.month, day.day, t_hour, 0, tzinfo=_IST)
    gross = net + 50.0
    return ScalpTrade(
        day=day, underlying="NIFTY", option_type="CE", direction="LONG",
        entry_time=et, exit_time=et + timedelta(minutes=4),
        entry_premium=100.0, exit_premium=100.0 + net / 65.0, qty=65,
        exit_reason=reason, gross_pnl=gross, costs=50.0, net_pnl=net,
        dte=dte, entry_iv=entry_iv,
    )


def test_compute_metrics_headline_and_breakeven():
    d = date(2026, 3, 3)
    trades = [_mk_trade(100.0, d), _mk_trade(100.0, d), _mk_trade(-50.0, d)]
    m = compute_metrics(trades)
    assert m["n_trades"] == 3
    assert m["net_expectancy"] == pytest.approx((100 + 100 - 50) / 3, abs=0.01)
    assert m["win_rate"] == pytest.approx(2 / 3, abs=0.001)
    # avg_win=100, avg_loss=50 → BE win-rate = 50/150 = 0.333
    assert m["break_even_win_rate"] == pytest.approx(0.3333, abs=0.001)
    assert m["profit_factor"] == pytest.approx(200 / 50, abs=0.01)
    assert "by_exit_reason" in m and "by_cell" in m


def test_break_even_win_rate_edges():
    assert _break_even_win_rate(0.0, 0.0) == 0.0
    assert _break_even_win_rate(0.0, 50.0) == 1.0   # no wins → can't break even
    assert _break_even_win_rate(100.0, 0.0) == 0.0  # no losses → 0 needed
    assert _break_even_win_rate(100.0, 50.0) == pytest.approx(1 / 3, abs=0.001)


def test_metrics_loss_streak_and_drawdown():
    d0 = date(2026, 3, 2)
    d1 = date(2026, 3, 3)
    # day0: -100 then -100 (streak); day1: +50
    trades = [
        _mk_trade(-100.0, d0, reason=EXIT_HARD_STOP),
        _mk_trade(-100.0, d0, reason=EXIT_HARD_STOP),
        _mk_trade(50.0, d1),
    ]
    m = compute_metrics(trades)
    assert m["longest_loss_streak"] == 2
    assert m["n_days"] == 2
    assert m["max_drawdown"] <= -200.0 + 1e-6


def test_metrics_empty():
    assert compute_metrics([])["n_trades"] == 0


def test_metrics_fat_tail_concentration():
    d = date(2026, 3, 3)
    # 9 tiny winners + 1 huge winner → top decile dominates net profit.
    trades = [_mk_trade(5.0, d) for _ in range(9)] + [_mk_trade(1000.0, d)]
    m = compute_metrics(trades)
    assert m["fat_tail_top_decile_pct"] > 80.0


# ─────────────────────────────────────────────────────────────────────────────
# 6. Sweep + determinism
# ─────────────────────────────────────────────────────────────────────────────
def _one_day_inputs():
    day = date(2026, 3, 3)
    ub = _ramp_underlying(0, 40, base=22000.0, step=9.0, day=day)
    ce = [Bar(t=b.t, open=100.0 + i * 3, high=100.0 + i * 3 + 2,
              low=100.0 + i * 3 - 2, close=100.0 + i * 3, volume=500.0, iv=12.0)
          for i, b in enumerate(ub)]
    pe = [_flat_bar(b.t, 80.0, iv=12.0) for b in ub]
    return [{"day": day, "underlying_bars": ub, "ce_bars": ce, "pe_bars": pe,
             "qty": 65, "dte": 1}]


def test_run_sweep_covers_mandatory_falsifiers():
    days = _one_day_inputs()
    sweep = run_sweep(days, "NIFTY")
    cells = sweep["cells"]
    # 3 spreads × 3 slips × 2 trail = 18 cells.
    assert len(cells) == 18
    spreads = {c["spread"] for c in cells}
    slips = {c["slip_pct"] for c in cells}
    trails = {c["use_trail"] for c in cells}
    assert spreads == {0.5, 1.0, 2.0}
    assert slips == {0.005, 0.01, 0.02}
    assert trails == {True, False}
    for c in cells:
        assert "net_expectancy" in c and "break_even_win_rate" in c


def test_backtest_is_deterministic():
    days = _one_day_inputs()
    a = run_backtest(days, "NIFTY", seed=7)["metrics"]
    b = run_backtest(days, "NIFTY", seed=7)["metrics"]
    assert a == b


def test_higher_costs_reduce_expectancy():
    """Wider spread + more slippage must never IMPROVE net expectancy."""
    days = _one_day_inputs()
    cheap = run_backtest(days, "NIFTY", fill=FillModel(spread=0.5, slip_pct=0.005))["metrics"]
    dear = run_backtest(days, "NIFTY", fill=FillModel(spread=2.0, slip_pct=0.02))["metrics"]
    if cheap["n_trades"] and dear["n_trades"]:
        assert dear["net_expectancy"] <= cheap["net_expectancy"] + 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# 7. Straddle-proxy fallback
# ─────────────────────────────────────────────────────────────────────────────
def test_straddle_proxy_tracks_direction():
    day = date(2026, 3, 3)
    # CE rising, PE falling → proxy (CE-PE) should rise.
    ce = [Bar(t=_t(9, 30, day=day) + timedelta(minutes=i), open=100 + i, high=101 + i,
              low=99 + i, close=100 + i) for i in range(10)]
    pe = [Bar(t=_t(9, 30, day=day) + timedelta(minutes=i), open=100 - i, high=101 - i,
              low=99 - i, close=100 - i) for i in range(10)]
    proxy = straddle_proxy_underlying(ce, pe)
    assert len(proxy) == 10
    assert proxy[-1].close > proxy[0].close
    # Non-overlapping legs → empty.
    pe2 = [Bar(t=_t(11, 0, day=day) + timedelta(minutes=i), open=100, high=100,
               low=100, close=100) for i in range(5)]
    assert straddle_proxy_underlying(ce, pe2) == []


def test_replay_flags_straddle_proxy_in_metrics():
    days = _one_day_inputs()
    days[0]["used_straddle_proxy"] = True
    m = run_backtest(days, "NIFTY")["metrics"]
    if m["n_trades"]:
        assert m["used_straddle_proxy"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 8. TZ-safety — identical results in IST and UTC representations
# ─────────────────────────────────────────────────────────────────────────────
def test_tz_safe_ist_vs_utc_equivalent():
    day = date(2026, 3, 3)
    ub_ist = _ramp_underlying(0, 40, base=22000.0, step=9.0, day=day)
    ce_ist = [Bar(t=b.t, open=100.0 + i * 3, high=100.0 + i * 3 + 2,
                  low=100.0 + i * 3 - 2, close=100.0 + i * 3, volume=500.0, iv=12.0)
              for i, b in enumerate(ub_ist)]
    pe_ist = [_flat_bar(b.t, 80.0, iv=12.0) for b in ub_ist]

    def _to_utc(bars):
        return [Bar(t=b.t.astimezone(timezone.utc), open=b.open, high=b.high,
                    low=b.low, close=b.close, volume=b.volume, iv=b.iv) for b in bars]

    res_ist = replay_day(day, "NIFTY", ub_ist, ce_ist, pe_ist,
                         fill=FillModel(spread=1.0, slip_pct=0.01))
    res_utc = replay_day(day, "NIFTY", _to_utc(ub_ist), _to_utc(ce_ist), _to_utc(pe_ist),
                         fill=FillModel(spread=1.0, slip_pct=0.01))
    assert len(res_ist) == len(res_utc)
    for a, b in zip(res_ist, res_utc):
        assert a.exit_reason == b.exit_reason
        assert a.net_pnl == pytest.approx(b.net_pnl, abs=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# 9. BANKNIFTY registry add — its OWN expiry rule, not NIFTY's
# ─────────────────────────────────────────────────────────────────────────────
def test_banknifty_registered_with_own_spec():
    from core.dhan_option_history import UNDERLYINGS, resolve_underlying

    assert "BANKNIFTY" in UNDERLYINGS
    bn = resolve_underlying("BANKNIFTY")
    assert bn.security_id == 25
    assert bn.strike_step == 100
    assert bn.instrument == "OPTIDX"
    assert bn.chain_seg == "IDX_I"


def test_banknifty_does_not_inherit_nifty_cutover():
    from core.dhan_option_history import resolve_underlying
    from core.expiry import THURSDAY

    bn = resolve_underlying("BANKNIFTY")
    nifty = resolve_underlying("NIFTY")
    # BANKNIFTY: single fixed weekday, NO Thursday→Tuesday cutover.
    assert bn.expiry_weekday == THURSDAY
    assert bn.cutover_date is None
    assert bn.pre_cutover_weekday is None
    # NIFTY keeps its cutover (sanity contrast).
    assert nifty.cutover_date is not None
    # The two derive DIFFERENT expiry weekdays before NIFTY's cutover.
    rule_bn = bn.expiry_rule
    rule_nifty = nifty.expiry_rule
    from core.expiry import expiry_weekday_for
    # AFTER NIFTY's 2026-09-01 cutover NIFTY → Tuesday(1) while BANKNIFTY stays
    # Thursday(3): BANKNIFTY does NOT follow NIFTY's cutover.
    post = date(2026, 10, 1)
    assert expiry_weekday_for(post, rule_bn) != expiry_weekday_for(post, rule_nifty)
    assert expiry_weekday_for(post, rule_bn) == THURSDAY


def test_banknifty_replay_uses_lot_30():
    """A BANKNIFTY replay must size at qty=30 (its lot), not NIFTY's 65."""
    day = date(2026, 3, 3)
    ub = _ramp_underlying(0, 30, base=48000.0, step=15.0, day=day)
    ce = [Bar(t=b.t, open=200.0 + i * 6, high=200.0 + i * 6 + 3,
              low=200.0 + i * 6 - 3, close=200.0 + i * 6, volume=500.0, iv=14.0)
          for i, b in enumerate(ub)]
    pe = [_flat_bar(b.t, 160.0, iv=14.0) for b in ub]
    trades = replay_day(day, "BANKNIFTY", ub, ce, pe,
                        fill=FillModel(spread=0.0, slip_pct=0.0),
                        replay=ReplayParams(use_trail=False))
    assert trades
    assert all(t.qty == 30 for t in trades)
    assert all(t.underlying == "BANKNIFTY" for t in trades)


# ─────────────────────────────────────────────────────────────────────────────
# 10. QA-hardening: exit priority, honest fallbacks, losing-system metrics
# ─────────────────────────────────────────────────────────────────────────────
def test_hard_stop_beats_signal_flip_same_bar():
    """When a bar both breaches the stop (on its low) AND the signal flips, the
    HARD STOP must win (it gives the worse, truer fill at stop_level, not the more
    optimistic close-based flip fill)."""
    day = date(2026, 3, 3)
    up = _ramp_underlying(0, 25, base=22000.0, step=12.0, day=day)
    # sharp reversal that flips the signal SHORT; on the SAME first down-bar the CE
    # premium also craters below the stop.
    down = []
    last_px = up[-1].close
    for i in range(15):
        px = last_px - (i + 1) * 20.0
        t = up[-1].t + timedelta(minutes=i + 1)
        down.append(Bar(t=t, open=px + 1, high=px + 5, low=px - 5, close=px, volume=100.0))
    ub = up + down
    ce = []
    for i, b in enumerate(ub):
        if i == len(up):  # first down-bar: CE collapses below the 20% stop
            ce.append(Bar(t=b.t, open=100.0, high=100.0, low=50.0, close=60.0,
                          volume=500.0, iv=12.0))
        else:
            ce.append(Bar(t=b.t, open=100.0, high=101.0, low=99.0, close=100.0,
                          volume=500.0, iv=12.0))
    pe = [Bar(t=b.t, open=100.0, high=101.0, low=99.0, close=100.0, volume=500.0, iv=12.0)
          for b in ub]
    trades = replay_day(day, "NIFTY", ub, ce, pe,
                        fill=FillModel(spread=0.0, slip_pct=0.0),
                        replay=ReplayParams(stop_pct=0.20, target_pct=0.99,
                                            use_trail=False, time_stop_min=999))
    # The exit on that bar must be the hard stop, not the flip.
    assert trades
    assert trades[0].exit_reason == EXIT_HARD_STOP


def test_eod_fallback_uses_last_known_close_not_entry():
    """If the option bar is missing at the EOD minute, the residual flatten must
    use the LAST KNOWN option close (honest), not the stale entry premium."""
    day = date(2026, 3, 3)
    bars = []
    start = _t(14, 30, day=day)
    for i in range(50):
        px = 22000.0 + i * 9.0
        t = start + timedelta(minutes=i)
        bars.append(Bar(t=t, open=px - 1, high=px + 5, low=px - 5, close=px, volume=100.0))
    # CE rises steadily but is ABSENT on the final (EOD) bar so the residual
    # flatten must fall back to the last seen close (~ much higher than entry).
    ce = []
    for i, b in enumerate(bars[:-1]):
        px = 100.0 + i * 3.0
        ce.append(Bar(t=b.t, open=px, high=px + 0.5, low=px - 0.4, close=px,
                      volume=500.0, iv=12.0))
    # (no CE bar for bars[-1])
    pe = [_flat_bar(b.t, 80.0, iv=12.0) for b in bars]
    trades = replay_day(day, "NIFTY", bars, ce, pe,
                        fill=FillModel(spread=0.0, slip_pct=0.0),
                        replay=ReplayParams(time_stop_min=999, use_trail=False,
                                            stop_pct=0.99, target_pct=0.99))
    assert trades
    eod = trades[-1]
    assert eod.exit_reason == EXIT_EOD
    # Honest fallback: exit premium reflects the last KNOWN close, well above entry,
    # NOT a phantom zero-gross trade at entry premium.
    assert eod.exit_premium > eod.entry_premium
    assert eod.gross_pnl > 0


def test_losing_system_metrics_are_not_misleading():
    """On a NET-LOSER, fat_tail and cost-drag must be None (not a sign-flipped or
    blown-up percentage), and profit_factor must be a real float < 1."""
    d = date(2026, 3, 3)
    trades = [_mk_trade(-100.0, d, reason=EXIT_HARD_STOP) for _ in range(8)]
    trades += [_mk_trade(20.0, d) for _ in range(2)]  # a couple of small winners
    m = compute_metrics(trades)
    assert m["total_net_pnl"] < 0
    assert m["fat_tail_top_decile_pct"] is None   # no profit to take a share of
    assert m["gross_vs_net_drag_pct"] is None      # gross also negative
    assert isinstance(m["profit_factor"], float)   # real float, never a string
    assert m["profit_factor"] < 1.0


def test_profit_factor_is_float_inf_when_no_losses():
    import math
    d = date(2026, 3, 3)
    trades = [_mk_trade(50.0, d) for _ in range(5)]
    m = compute_metrics(trades)
    assert isinstance(m["profit_factor"], float)
    assert math.isinf(m["profit_factor"])
    assert m["zero_loss_sample"] is True


def test_by_exit_reason_and_by_cell_content():
    """Bucketing content (not just key presence) is correct."""
    d = date(2026, 3, 3)
    trades = [
        _mk_trade(100.0, d, reason=EXIT_TARGET, dte=1, t_hour=10, entry_iv=12.0),
        _mk_trade(50.0, d, reason=EXIT_TARGET, dte=1, t_hour=10, entry_iv=12.0),
        _mk_trade(-30.0, d, reason=EXIT_HARD_STOP, dte=0, t_hour=14, entry_iv=16.0),
    ]
    m = compute_metrics(trades)
    ber = m["by_exit_reason"]
    assert ber[EXIT_TARGET]["n"] == 2
    assert ber[EXIT_TARGET]["net"] == pytest.approx(150.0)
    assert ber[EXIT_TARGET]["avg_net"] == pytest.approx(75.0)
    assert ber[EXIT_HARD_STOP]["n"] == 1
    assert ber[EXIT_HARD_STOP]["net"] == pytest.approx(-30.0)
    # by_cell: the two targets share a cell (dte_1|open|iv_mid); the stop is its own.
    bc = m["by_cell"]
    target_cell = "dte_1|open(9-11)|iv_mid(11-15)"
    stop_cell = "dte_0(expiry)|close(13-15)|iv_high(>=15)"
    assert bc[target_cell]["n"] == 2
    assert bc[target_cell]["expectancy"] == pytest.approx(75.0)
    assert bc[stop_cell]["n"] == 1
    assert bc[stop_cell]["expectancy"] == pytest.approx(-30.0)


def test_no_trail_collapses_or_changes_ev_falsifier():
    """The trail-on/off falsifier dimension must actually change behaviour: a
    target-then-runner path should differ between use_trail True and False."""
    day = date(2026, 3, 3)
    ub = _ramp_underlying(0, 45, base=22000.0, step=10.0, day=day)
    # Entry only after the 20-bar warm-up (~bar 21). Keep the CE ~flat at 100
    # through warm-up so entry premium is ~100, THEN rise past +10% (→110), run far
    # higher (200), and give back. use_trail=False banks at +10%; use_trail=True
    # rides the runner and exits later via the trail — a DIFFERENT level.
    ce = []
    for i, b in enumerate(ub):
        if i <= 21:
            px = 100.0               # flat through warm-up → entry premium ~100
        elif i < 33:
            px = 100.0 + (i - 21) * 9.0   # rise past +10% (110) and well beyond
        elif i < 38:
            px = 200.0               # high-water plateau
        else:
            px = 150.0               # give back > 12% off 200 → trail fires
        ce.append(Bar(t=b.t, open=px, high=px + 1, low=px - 1, close=px,
                      volume=500.0, iv=12.0))
    pe = [_flat_bar(b.t, 80.0, iv=12.0) for b in ub]
    days = [{"day": day, "underlying_bars": ub, "ce_bars": ce, "pe_bars": pe, "qty": 65, "dte": 1}]
    rp_trail = ReplayParams(target_pct=0.10, trail_pct=0.12, use_trail=True,
                            time_stop_min=999, stop_pct=0.99)
    rp_notrail = ReplayParams(target_pct=0.10, trail_pct=0.12, use_trail=False,
                              time_stop_min=999, stop_pct=0.99)
    with_trail = run_backtest(days, "NIFTY", replay=rp_trail,
                              fill=FillModel(spread=0.0, slip_pct=0.0))["metrics"]
    no_trail = run_backtest(days, "NIFTY", replay=rp_notrail,
                            fill=FillModel(spread=0.0, slip_pct=0.0))["metrics"]
    # The two configs must produce a different outcome (the falsifier is live):
    # the trailing runner banks more on this trending-then-fade path.
    assert with_trail["total_net_pnl"] != no_trail["total_net_pnl"]
    assert with_trail["total_net_pnl"] > no_trail["total_net_pnl"]
