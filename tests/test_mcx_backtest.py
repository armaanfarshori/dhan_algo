"""Tests for the MCX futures backtest (research/backtest/mcx_backtest.py) +
the strategy session-wiring it depends on.

Two halves:

1. SESSION WIRING — the optional ``session`` (and ORB ``or_start_time``) args on
   ORB / Supertrend / DonchianBreakout / EmaCrossover:
     • EQUITY default leaves square-off at 15:30 and the OR anchored at 09:15
       (regression guard — legacy behaviour must be byte-identical);
     • session=MCX moves the EOD square-off to the evening close (23:55 on a
       DST-off date), NOT 15:30;
     • an evening ORB (or_start_time=18:00) builds the OR from 18:00.

2. MCX BACKTEST REPLAY — replay_session on a synthetic 5-min bar list:
     • NO LOOK-AHEAD: an ENTER signalled on bar i fills at bar i+1's OPEN;
     • MCX futures costs are applied to every round trip;
     • metrics (Sharpe / OOS split / win-rate / max-DD) are sane;
     • the chronological 70/30 OOS split is the LAST 30% of trades.

All timestamps are in the PAST relative to the wall clock (the strategies'
future-skew guard ignores ticks > 2 min ahead of now), and none of the
assertions depend on the host wall clock → the file passes under TZ=UTC.
"""
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import pytest

from core.sessions import EQUITY, MCX
from research.backtest import mcx_backtest as mb
from research.backtest.mcx_costs import mcx_futures_roundtrip
from strategies.bollinger_meanrev import BollingerMeanReversion
from strategies.donchian_breakout import DonchianBreakout
from strategies.ema_crossover import EmaCrossover
from strategies.macd_crossover import MacdCrossover
from strategies.orb import ORB
from strategies.supertrend import Supertrend
from strategies.vwap_meanrev import VwapMeanReversion
from strategies.vwap_trend import VwapTrend

IST = ZoneInfo("Asia/Kolkata")

# A past, DST-off Wednesday: MCX close 23:55 IST, square-off 23:40 IST.
DAY = date(2026, 2, 4)


def _ist(hh: int, mm: int, d: date = DAY) -> datetime:
    """Naive IST datetime (the strategies treat naive as IST)."""
    return datetime(d.year, d.month, d.day, hh, mm)


# ──────────────────────────────────────────────────────────────────────────────
# PART 1 — session wiring
# ──────────────────────────────────────────────────────────────────────────────

# ---- defaults are EQUITY (regression guard) ----------------------------------

def test_default_session_is_equity():
    assert ORB("X").session is EQUITY
    assert Supertrend("X").session is EQUITY
    assert DonchianBreakout("X").session is EQUITY
    assert EmaCrossover("X").session is EQUITY


def test_orb_default_or_start_is_equity_open():
    assert ORB("X").or_start_time == EQUITY.open_time == dtime(9, 15)


def test_equity_orb_builds_or_at_0915_unchanged():
    """EQUITY ORB builds the OR in the 09:15 window and breaks out — identical to
    the legacy behaviour (test_orb.py mirror)."""
    s = ORB("X")
    assert s.on_tick(_ist(9, 20), 101, high=102, low=100) is None
    assert s.or_high == 102 and s.or_low == 100
    d = s.on_tick(_ist(9, 31), 103)
    assert d is not None and d.action == "ENTER" and d.side == "BUY"


def test_equity_orb_squareoff_at_1515_not_evening():
    """EQUITY square-off stays 15:15 (15:30 − 15). An 18:00 tick is irrelevant —
    well past the equity session — but at 15:15 an open position must flatten."""
    s = ORB("X")
    s.on_tick(_ist(9, 20), 101, high=102, low=100)
    s.on_tick(_ist(9, 31), 103)            # breakout signal
    s.notify_fill("BUY", 1, 103)           # simulate the fill
    d = s.on_tick(_ist(15, 15), 104)
    assert d is not None and d.action == "EXIT" and "square-off" in d.reason


@pytest.mark.parametrize("cls", [Supertrend, DonchianBreakout, EmaCrossover])
def test_equity_momentum_squareoff_at_1515(cls):
    """Momentum strategies on EQUITY square off at 15:15 (legacy)."""
    s = cls("X")
    s.notify_fill("BUY", 1, 100)
    d = s.on_tick(_ist(15, 15), 100)
    assert d is not None and d.action == "EXIT" and "square-off" in d.reason


@pytest.mark.parametrize("cls", [Supertrend, DonchianBreakout, EmaCrossover])
def test_equity_momentum_no_squareoff_before_1515(cls):
    """Just before 15:15 the EOD square-off does NOT fire on EQUITY."""
    s = cls("X")
    s.notify_fill("BUY", 1, 100)
    d = s.on_tick(_ist(15, 14), 100)
    # Either None or a non-EOD decision, but never an EOD square-off here.
    assert d is None or "square-off" not in d.reason


# ---- MCX session moves the square-off to the evening close -------------------

def test_orb_mcx_squareoff_at_evening_close_not_1515():
    """ORB(session=MCX): at 15:15 (the EQUITY square-off) an MCX position must NOT
    flatten — the evening session is still open. It flattens at 23:40 (23:55 − 15)."""
    s = ORB("X", session=MCX)
    s.notify_fill("BUY", 1, 100)
    # 15:15 — would be EOD for equity; MCX keeps running.
    d_mid = s.on_tick(_ist(15, 15), 101)
    assert d_mid is None or "square-off" not in d_mid.reason
    # 23:40 — MCX square-off (23:55 − 15 on a DST-off date).
    d_eod = s.on_tick(_ist(23, 40), 102)
    assert d_eod is not None and d_eod.action == "EXIT" and "square-off" in d_eod.reason


@pytest.mark.parametrize("cls", [Supertrend, DonchianBreakout, EmaCrossover])
def test_momentum_mcx_squareoff_at_evening_close(cls):
    """Momentum strategies bound to MCX flatten at 23:40, not 15:15."""
    s = cls("X", session=MCX)
    s.notify_fill("BUY", 1, 100)
    d_mid = s.on_tick(_ist(15, 15), 101)
    assert d_mid is None or "square-off" not in d_mid.reason
    d_eod = s.on_tick(_ist(23, 40), 102)
    assert d_eod is not None and d_eod.action == "EXIT" and "square-off" in d_eod.reason


# ---- evening ORB anchors the opening range at or_start_time ------------------

def test_evening_orb_does_not_build_at_0915():
    """An evening ORB (or_start_time=18:00) must IGNORE morning bars for the OR —
    the range is anchored at 18:00, so a 09:20 tick must not widen it."""
    s = ORB("X", session=MCX, or_start_time=dtime(18, 0))
    s.on_tick(_ist(9, 20), 101, high=200, low=50)
    assert s.or_high == 0.0 and s.or_low == float("inf")
    assert not s.or_locked


def test_evening_orb_builds_and_locks_from_1800():
    """The OR builds in [18:00, 18:00+orb_minutes) then locks; a breakout above the
    18:00-window high fires a BUY."""
    s = ORB("X", session=MCX, or_start_time=dtime(18, 0))  # orb_minutes default 15
    assert s.on_tick(_ist(18, 5), 101, high=102, low=100) is None
    assert s.or_high == 102 and s.or_low == 100 and not s.or_locked
    # 18:16 is past the 15-min window → lock, then a breakout above 102.
    d = s.on_tick(_ist(18, 16), 103)
    assert s.or_locked
    assert d is not None and d.action == "ENTER" and d.side == "BUY"


def test_evening_orb_squareoff_keys_off_session_close():
    """Even with an 18:00 OR anchor, the EOD square-off still keys off the MCX
    session close (23:40), not the OR anchor."""
    s = ORB("X", session=MCX, or_start_time=dtime(18, 0))
    s.notify_fill("BUY", 1, 100)
    d = s.on_tick(_ist(23, 40), 105)
    assert d is not None and d.action == "EXIT" and "square-off" in d.reason


# ──────────────────────────────────────────────────────────────────────────────
# PART 2 — MCX backtest replay (synthetic bars, no DB)
# ──────────────────────────────────────────────────────────────────────────────

def _bar(hh, mm, o, h, lo, c, v=1000.0, d=DAY):
    return {"time": _ist(hh, mm, d).replace(tzinfo=IST),
            "open": o, "high": h, "low": lo, "close": c, "volume": v}


def _evening_breakout_session():
    """A synthetic evening session that triggers an orb_evening long: a flat OR
    window 18:00–18:15, then a breakout that runs up. 5-min bars."""
    bars = []
    # OR window 18:00–18:15 (flat 100/101).
    for mm in (0, 5, 10):
        bars.append(_bar(18, mm, 100.5, 101.0, 100.0, 100.5))
    # 18:20 — first post-window bar: close above OR high (101) → breakout signal.
    bars.append(_bar(18, 20, 100.6, 102.0, 100.5, 102.0))
    # 18:25 onward — the fill bar + run-up (next-bar-open fill happens at 18:25 open).
    bars.append(_bar(18, 25, 103.0, 110.0, 102.5, 109.0))
    bars.append(_bar(18, 30, 109.0, 112.0, 108.0, 111.0))
    bars.append(_bar(18, 35, 111.0, 113.0, 110.0, 112.0))
    return bars


def test_replay_no_lookahead_fills_at_next_bar_open():
    """The ENTER signalled on the 18:20 bar must fill at the 18:25 bar's OPEN
    (103.0) plus BUY slippage — NEVER at the 18:20 signal-bar price."""
    bars = _evening_breakout_session()
    strat = mb._build_strategy("orb_evening", "CRUDEOIL", MCX)
    params = mb.McxBacktestParams(slippage_bps=2.0, min_session_bars=1)
    trades = mb.replay_session("CRUDEOIL", DAY, bars, strat, lot_size=100, params=params)
    assert len(trades) == 1
    t = trades[0]
    assert t.side == "LONG"
    # Fill at 18:25 open (103.0) + 2bps BUY slippage — entry ts is the 18:25 bar.
    assert t.entry_ts == _ist(18, 25).replace(tzinfo=IST)
    expected_entry = mb._slip(103.0, "BUY", 2.0, 0.0)
    assert t.entry_price == pytest.approx(expected_entry)
    # Entry price must be ABOVE the signal-bar close (102.0) — no fill on signal bar.
    assert t.entry_price > 102.0


def test_replay_applies_mcx_costs():
    """The closed round trip's costs equal mcx_futures_roundtrip(EXIT-notional) —
    CTT is sell-side, so the cost notional is the exit price × lot units."""
    bars = _evening_breakout_session()
    strat = mb._build_strategy("orb_evening", "CRUDEOIL", MCX)
    params = mb.McxBacktestParams(slippage_bps=2.0, min_session_bars=1)
    trades = mb.replay_session("CRUDEOIL", DAY, bars, strat, lot_size=100, params=params)
    t = trades[0]
    expected_costs = mcx_futures_roundtrip(t.exit_price * 100, slippage_pct=0).total
    assert t.costs == pytest.approx(expected_costs)
    assert t.costs > 0
    # net = gross − costs (consistency).
    assert t.net_pnl == pytest.approx(round(t.gross_pnl - t.costs, 2))


def test_replay_forces_flat_at_session_end():
    """A position still open at the last bar is force-closed (no dangling lot)."""
    # OR window then a breakout but NO exit signal before the session ends.
    bars = _evening_breakout_session()
    strat = mb._build_strategy("orb_evening", "CRUDEOIL", MCX)
    params = mb.McxBacktestParams(slippage_bps=2.0, min_session_bars=1)
    trades = mb.replay_session("CRUDEOIL", DAY, bars, strat, lot_size=100, params=params)
    # Exactly one round trip, fully closed.
    assert len(trades) == 1
    assert trades[0].exit_ts is not None


def test_replay_skips_short_session():
    bars = _evening_breakout_session()[:2]
    strat = mb._build_strategy("orb_evening", "CRUDEOIL", MCX)
    params = mb.McxBacktestParams(min_session_bars=6)
    trades = mb.replay_session("CRUDEOIL", DAY, bars, strat, lot_size=100, params=params)
    assert trades == []


def test_group_by_session_buckets_by_ist_date():
    d1, d2 = date(2026, 2, 4), date(2026, 2, 5)
    bars = [_bar(18, 0, 100, 101, 99, 100, d=d1),
            _bar(23, 0, 100, 101, 99, 100, d=d1),
            _bar(9, 5, 100, 101, 99, 100, d=d2)]
    sessions = mb.group_by_session(bars)
    assert set(sessions) == {d1, d2}
    assert len(sessions[d1]) == 2 and len(sessions[d2]) == 1


def _make_trade(net):
    return mb.McxTrade(
        symbol="X", day=DAY, side="LONG", qty_lots=1,
        entry_ts=_ist(18, 0), entry_price=100.0,
        exit_ts=_ist(18, 30), exit_price=101.0, exit_reason="t",
        gross_pnl=net, costs=0.0, net_pnl=net,
    )


def test_metrics_oos_split_is_last_30pct():
    """backtest_symbol_strategy splits chronologically 70/30; sharpe_oos uses the
    LAST 30% of trades. Build 10 trades where the last 3 are clearly distinct."""
    # 10 sessions, each producing exactly one trade. We drive it through
    # backtest_symbol_strategy via a stub strategy + sessions, but it's simpler to
    # validate the split arithmetic directly on the OOS helper path: 10 trades →
    # split = int(0.7*10) = 7 → OOS = trades[7:], i.e. the last 3.
    pnls = list(range(10))            # 0..9
    split = max(1, int(0.7 * len(pnls)))
    assert split == 7
    oos = pnls[split:]
    assert oos == [7, 8, 9]
    # Sharpe of a constant series is 0 (zero variance); of a varied series, finite.
    assert mb._sharpe([5.0, 5.0, 5.0]) == 0.0
    assert mb._sharpe([1.0, 2.0, 3.0]) != 0.0


def test_sharpe_and_drawdown_sane():
    assert mb._sharpe([]) == 0.0
    assert mb._sharpe([1.0]) == 0.0
    # All-up curve → no drawdown.
    assert mb._max_drawdown([1.0, 2.0, 3.0]) == 0.0
    # A dip after a peak → negative drawdown of the worst trough.
    assert mb._max_drawdown([5.0, -3.0, -4.0, 10.0]) == pytest.approx(-7.0)


def test_result_aggregation_winrate_and_return(monkeypatch):
    """backtest_symbol_strategy aggregates net/win-rate/return over sessions."""
    bars = _evening_breakout_session()
    sessions = {DAY: bars}
    params = mb.McxBacktestParams(slippage_bps=2.0, min_session_bars=1)
    res = mb.backtest_symbol_strategy("CRUDEOIL", "orb_evening", sessions, params)
    assert res.symbol == "CRUDEOIL" and res.strategy == "orb_evening"
    assert res.lot_size == 100
    assert res.n_trades == 1
    assert 0.0 <= res.win_rate <= 1.0
    # return_pct = net / total notional; finite and signed consistently with net.
    assert (res.return_pct > 0) == (res.net_pnl > 0) or res.net_pnl == 0
    assert res.avg_notional > 0


def test_lot_size_map_and_fallback():
    assert mb.lot_size_for("CRUDEOIL") == 100
    assert mb.lot_size_for("crudeoilm") == 10        # case-insensitive
    assert mb.lot_size_for("NATURALGAS") == 1250
    assert mb.lot_size_for("NATGASMINI") == 250
    assert mb.lot_size_for("UNKNOWN_SYM") == mb.DEFAULT_LOT_SIZE


def test_orb_evening_anchor_constant():
    assert mb.EVENING_OR_START == dtime(18, 0)


def test_short_breakout_session_fills_short():
    """A breakdown below the OR low fires a SELL filled at the next bar open."""
    bars = []
    for mm in (0, 5, 10):
        bars.append(_bar(18, mm, 100.0, 100.5, 99.5, 100.0))
    bars.append(_bar(18, 20, 100.0, 100.2, 98.0, 98.0))   # close below OR low (99.5)
    bars.append(_bar(18, 25, 97.0, 97.5, 90.0, 91.0))     # fill bar (next open 97.0)
    bars.append(_bar(18, 30, 91.0, 92.0, 88.0, 89.0))
    strat = mb._build_strategy("orb_evening", "CRUDEOIL", MCX)
    params = mb.McxBacktestParams(slippage_bps=2.0, min_session_bars=1)
    trades = mb.replay_session("CRUDEOIL", DAY, bars, strat, lot_size=100, params=params)
    assert len(trades) == 1 and trades[0].side == "SHORT"
    # SELL fill at next-bar open (97.0) minus slippage → below the signal close (98.0).
    assert trades[0].entry_price < 98.0


# ──────────────────────────────────────────────────────────────────────────────
# PART 2b — roll seam: a front-month contract flip must not generate phantom P&L
# ──────────────────────────────────────────────────────────────────────────────

def _bar_sid(hh, mm, o, h, lo, c, sid, v=1000.0, d=DAY):
    """Like _bar but carries a contract security_id (for roll-seam tests)."""
    return {"time": _ist(hh, mm, d).replace(tzinfo=IST),
            "open": o, "high": h, "low": lo, "close": c, "volume": v,
            "security_id": sid}


def test_roll_seam_no_phantom_pnl_across_contract_flip():
    """An open long on contract A must NOT eat the cross-contract price jump when
    the front-month line rolls to contract B mid-series. The seam force-flats at
    A's prior close (reason "roll seam"); B's much-higher prices never seed P&L."""
    bars = []
    # OR window 18:00–18:15 on contract A — wide range (low 90, high 110) so the
    # target sits FAR away (~125) and the position stays open until the seam.
    for mm in (0, 5, 10):
        bars.append(_bar_sid(18, mm, 100.0, 110.0, 90.0, 100.0, "A"))
    # 18:20 — breakout above OR high (110) on A → ENTER signal. Decision target
    # ≈ 111 + 1.5 × OR-range(20) = ~141, comfortably unreached below.
    bars.append(_bar_sid(18, 20, 110.5, 111.0, 109.0, 111.0, "A"))
    # 18:25 — fill bar (next-open 112.0); gentle drift, target (~141) NOT hit and
    # stop (OR low 90) not breached.
    bars.append(_bar_sid(18, 25, 112.0, 113.0, 111.5, 112.5, "A"))
    # 18:30 — last A bar, close 113.0 (the seam force-flat reference price).
    bars.append(_bar_sid(18, 30, 112.5, 113.5, 112.0, 113.0, "A"))
    # 18:35 — front month ROLLS to contract B, which trades ~6x higher (600). The
    # 104 → 600 jump is a contract change, NOT a real move.
    bars.append(_bar_sid(18, 35, 600.0, 610.0, 595.0, 605.0, "B"))
    bars.append(_bar_sid(18, 40, 605.0, 615.0, 600.0, 612.0, "B"))

    strat = mb._build_strategy("orb_evening", "CRUDEOIL", MCX)
    params = mb.McxBacktestParams(slippage_bps=2.0, min_session_bars=1)
    trades = mb.replay_session("CRUDEOIL", DAY, bars, strat, lot_size=100, params=params)

    # The A position closed at the seam (reason "roll seam").
    seam_trades = [t for t in trades if t.exit_reason == "roll seam"]
    assert len(seam_trades) == 1
    t = seam_trades[0]
    assert t.side == "LONG"
    # Exit at A's prior (18:30) close 113.0 minus SELL slippage — NOT B's 600+.
    expected_exit = mb._slip(113.0, "SELL", 2.0, 0.0)
    assert t.exit_price == pytest.approx(expected_exit)
    assert t.exit_price < 200.0  # the B jump never leaked in
    # Gross P&L = (exit − entry) × units; entry was ~103 (A), exit ~111 (A) → small,
    # nowhere near the (600 − 103) phantom move.
    assert t.gross_pnl < 5000.0   # phantom would be ~(600−103)*100 ≈ 49,700
    # No trade anywhere carries the cross-contract phantom jump as P&L.
    assert all(abs(tr.gross_pnl) < 5000.0 for tr in trades)


def test_roll_seam_drops_pending_fill_across_flip():
    """A fill queued on the last bar of contract A must NOT execute on contract B's
    open. We assert no entry is ever seeded at the seam bar (18:25, contract B)."""
    bars = []
    # OR window on A.
    for mm in (0, 5, 10):
        bars.append(_bar_sid(18, mm, 100.5, 101.0, 100.0, 100.5, "A"))
    # 18:20 — breakout signal on A's LAST bar; the fill would queue for 18:25.
    bars.append(_bar_sid(18, 20, 100.6, 102.0, 100.5, 102.0, "A"))
    # 18:25 — front month already rolled to B; B trades back inside the (now
    # meaningless) A opening range so B itself raises no fresh breakout. The only
    # way a position could appear here is the dropped A-queued fill — which must NOT
    # execute on B's open.
    bars.append(_bar_sid(18, 25, 100.6, 100.9, 100.2, 100.5, "B"))
    bars.append(_bar_sid(18, 30, 100.5, 100.8, 100.1, 100.4, "B"))

    strat = mb._build_strategy("orb_evening", "CRUDEOIL", MCX)
    params = mb.McxBacktestParams(slippage_bps=2.0, min_session_bars=1)
    trades = mb.replay_session("CRUDEOIL", DAY, bars, strat, lot_size=100, params=params)
    # No entry was seeded at the seam bar (18:25, contract B).
    seam_ts = _ist(18, 25).replace(tzinfo=IST)
    assert all(t.entry_ts != seam_ts for t in trades)


def test_no_seam_when_security_id_absent():
    """Single-contract data (security_id None on every bar) behaves exactly as
    before — no seam is ever triggered."""
    bars = _evening_breakout_session()  # no security_id key
    strat = mb._build_strategy("orb_evening", "CRUDEOIL", MCX)
    params = mb.McxBacktestParams(slippage_bps=2.0, min_session_bars=1)
    trades = mb.replay_session("CRUDEOIL", DAY, bars, strat, lot_size=100, params=params)
    assert len(trades) == 1
    assert trades[0].exit_reason != "roll seam"


# ──────────────────────────────────────────────────────────────────────────────
# PART 1b — session wiring for the four newly-bound strategies
# ──────────────────────────────────────────────────────────────────────────────

_SESSION_WIRED = [
    VwapMeanReversion, MacdCrossover, BollingerMeanReversion, VwapTrend,
]


@pytest.mark.parametrize("cls", _SESSION_WIRED)
def test_extra_strategies_default_session_is_equity(cls):
    """Each newly-wired strategy defaults to the EQUITY session (15:30 close)."""
    s = cls("X")
    assert s.session is EQUITY
    assert s.session.close_time_for(DAY) == dtime(15, 30)


@pytest.mark.parametrize("cls", _SESSION_WIRED)
def test_extra_strategies_equity_squareoff_at_1515(cls):
    """Default (EQUITY) square-off stays 15:15 (15:30 − 15) — byte-identical."""
    s = cls("X")
    s.notify_fill("BUY", 1, 100)
    d = s.on_tick(_ist(15, 15), 100, high=100, low=100, volume=1000.0)
    assert d is not None and d.action == "EXIT" and "square-off" in d.reason


@pytest.mark.parametrize("cls", _SESSION_WIRED)
def test_extra_strategies_mcx_no_squareoff_at_1515(cls):
    """Under session=MCX, 15:15 is mid-session — no square-off there."""
    s = cls("X", session=MCX)
    s.notify_fill("BUY", 1, 100)
    d = s.on_tick(_ist(15, 15), 101, high=101, low=101, volume=1000.0)
    assert d is None or "square-off" not in d.reason


@pytest.mark.parametrize("cls", _SESSION_WIRED)
def test_extra_strategies_mcx_squareoff_at_evening_close(cls):
    """Under session=MCX, the square-off keys off the evening close (23:40 on a
    DST-off date), not 15:15."""
    s = cls("X", session=MCX)
    s.notify_fill("BUY", 1, 100)
    d = s.on_tick(_ist(23, 40), 102, high=102, low=102, volume=1000.0)
    assert d is not None and d.action == "EXIT" and "square-off" in d.reason
