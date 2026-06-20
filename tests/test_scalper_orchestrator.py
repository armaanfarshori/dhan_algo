"""
Unit tests for engine/scalper_orchestrator.py + engine/daily_governor.py.

All tests are PURE: a fake SignalProvider + OptionResolver, a PaperExecutor stub,
and the REAL Portfolio (db_backend=None) + RiskEngine consumed through their public
interfaces.  No DB, no network, no live data.

TZ-safe: every timestamp the tests build is an aware IST datetime, and the asserts
that depend on the session date are exercised under TZ=UTC in CI (the ci-ist-date-trap
guard).  The orchestrator derives the session date from the *tick's* `now`, never
from `date.today()`, so a UTC CI box and the IST dev Mac agree.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from engine.daily_governor import (
    ACTIVE,
    LOCKED,
    STOOD_DOWN,
    TRAIL_ONLY,
    DailyGovernor,
    GovernorParams,
)
from engine.execution import PaperExecutor
from engine.portfolio import Portfolio
from engine.risk import RiskEngine, RiskParams
from engine.scalper_orchestrator import (
    OptionLeg,
    OrchestratorParams,
    ScalpSignal,
    ScalperOrchestrator,
)

IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSignalProvider:
    """Scripted signal source.  `queue[underlying]` is a list of ScalpSignal|None
    consumed FIFO on each poll; `fills` records notify_fill calls."""

    def __init__(self):
        self.queue: dict[str, list] = {"NIFTY": [], "BANKNIFTY": []}
        self.fills: list = []
        self._open: dict[str, int] = {"NIFTY": 0, "BANKNIFTY": 0}

    def push(self, underlying: str, sig) -> None:
        self.queue[underlying].append(sig)

    def poll(self, underlying, now):
        q = self.queue.get(underlying, [])
        return q.pop(0) if q else None

    def notify_fill(self, underlying, side, lots, premium, now):
        self.fills.append((underlying, side, lots, premium))
        if side == "BUY":
            self._open[underlying] = self._open.get(underlying, 0) + lots
        else:
            self._open[underlying] = max(0, self._open.get(underlying, 0) - lots)

    def open_lots(self, underlying):
        return self._open.get(underlying, 0)


class FakeResolver:
    """Maps an underlying → a fixed OptionLeg.  `ltp` can be retuned per test."""

    LEGS = {
        "NIFTY": OptionLeg(underlying="NIFTY", security_id="NIFTY_CE_22000",
                           exchange_segment="NSE_FNO", option_type="CE",
                           strike=22000, lot=65, ltp=100.0),
        "BANKNIFTY": OptionLeg(underlying="BANKNIFTY", security_id="BANK_CE_48000",
                               exchange_segment="NSE_FNO", option_type="CE",
                               strike=48000, lot=15, ltp=200.0),
    }

    def __init__(self):
        self.legs = dict(self.LEGS)

    def resolve(self, signal):
        return self.legs.get(signal.underlying)


def _ltp_lookup_factory(resolver):
    by_sid = {leg.security_id: leg for leg in resolver.legs.values()}

    def _lookup(sid):
        leg = by_sid.get(sid)
        return leg.ltp if leg else 0.0
    return _lookup


def _build(*, gov_params=None, risk_params=None):
    """Wire a real Portfolio + RiskEngine + PaperExecutor with the fakes."""
    signals = FakeSignalProvider()
    resolver = FakeResolver()
    portfolio = Portfolio(mode="PAPER", db_backend=None)
    # Risk geometry tuned for the scalper sleeve: long-option entries carry no
    # stop_price (the scalper's own % stop lives in the signal engine), so the
    # platform RiskEngine sees full-premium-at-risk.  Give the day budget room so
    # the platform gate doesn't pre-empt the governor in these unit tests — the
    # governor is the strategy-scoped cap under test here.
    risk = RiskEngine(
        params=risk_params or RiskParams(
            equity_base=250_000.0, max_open_positions=10,
            max_daily_loss_pct=1.0, max_notional_pct=1.0,
            max_gross_exposure_pct=10.0),
        portfolio=portfolio, ltp_lookup=_ltp_lookup_factory(resolver),
        db_backend=None)
    executor = PaperExecutor(db_backend=None, slippage_bps=0.0)
    orch = ScalperOrchestrator(
        signals=signals, resolver=resolver, portfolio=portfolio, risk=risk,
        executor=executor,
        params=OrchestratorParams(
            warmup_min=15, cooldown_min=3,
            governor=gov_params or GovernorParams(sleeve_capital=250_000.0)))
    return orch, signals, resolver, portfolio, risk


def _in_session(hour=11, minute=0):
    """An aware IST datetime well inside the trading session (post-warmup)."""
    # 2026-06-22 is a Monday — a trading day for the EQUITY profile.
    return datetime(2026, 6, 22, hour, minute, tzinfo=IST)


def _enter(underlying, lots=1):
    return ScalpSignal(underlying=underlying, action="ENTER", side="BUY",
                       option_type="CE", strike=22000, lots=lots, reason="test enter")


def _exit(underlying, lots=0):
    return ScalpSignal(underlying=underlying, action="EXIT", side="SELL",
                       option_type="CE", strike=22000, lots=lots, reason="test exit")


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# DailyGovernor — pure unit tests
# ===========================================================================


def test_governor_loss_cap_stands_down():
    g = DailyGovernor(GovernorParams(max_daily_loss=5000.0, daily_profit_lock=None))
    now = _in_session()
    g.reset_session(now.date())
    g.record_open("NIFTY", 1)
    g.record_trade_close("NIFTY", -5000.0, now)
    assert g.state.state == STOOD_DOWN
    assert not g.allows_open("NIFTY")
    # entering STOOD_DOWN with a position open requested a flatten exactly once
    assert g.consume_flatten_request() is True
    assert g.consume_flatten_request() is False


def test_governor_profit_lock_arms_and_ratchets():
    g = DailyGovernor(GovernorParams(daily_profit_lock=6000.0,
                                     profit_lock_floor_frac=0.5,
                                     profit_lock_giveback=2500.0))
    now = _in_session()
    g.reset_session(now.date())
    g.record_trade_close("NIFTY", 6500.0, now)   # crosses arm
    assert g.state.lock_armed
    assert g.state.state == LOCKED
    assert g.state.profit_floor == pytest.approx(max(3000.0, 6500.0 - 2500.0))  # 4000
    # ratchet up on a higher high-water
    g.record_trade_close("NIFTY", 3200.0, now)   # realized 9700
    assert g.state.profit_floor == pytest.approx(9700.0 - 2500.0)  # 7200
    # a lower subsequent realized does NOT lower the floor
    g.record_trade_close("NIFTY", -1.0, now)
    assert g.state.profit_floor == pytest.approx(7200.0)


def test_governor_floor_break_trail_only_blocks_opens():
    g = DailyGovernor(GovernorParams(daily_profit_lock=6000.0,
                                     profit_lock_floor_frac=0.5,
                                     profit_lock_giveback=2500.0,
                                     profit_lock_mode="trail_only"))
    now = _in_session()
    g.reset_session(now.date())
    g.record_trade_close("NIFTY", 6500.0, now)   # arm; floor 4000
    g.record_trade_close("NIFTY", -3000.0, now)  # realized 3500 ≤ floor 4000
    assert g.state.state == TRAIL_ONLY
    assert not g.allows_open("NIFTY")
    # trail_only does NOT request a flatten (manage open, just no new risk)
    assert g.consume_flatten_request() is False


def test_governor_floor_break_stop_stands_down():
    g = DailyGovernor(GovernorParams(daily_profit_lock=6000.0,
                                     profit_lock_floor_frac=0.5,
                                     profit_lock_giveback=2500.0,
                                     profit_lock_mode="stop"))
    now = _in_session()
    g.reset_session(now.date())
    g.record_open("NIFTY", 1)
    g.record_trade_close("NIFTY", 6500.0, now)   # arm; floor 4000
    g.record_trade_close("NIFTY", -3000.0, now)  # realized 3500 ≤ floor
    assert g.state.state == STOOD_DOWN


def test_governor_consecutive_loss_stop():
    g = DailyGovernor(GovernorParams(consecutive_loss_stop=3, daily_profit_lock=None,
                                     max_daily_loss=1_000_000.0))
    now = _in_session()
    g.reset_session(now.date())
    g.record_trade_close("NIFTY", -100.0, now)
    g.record_trade_close("NIFTY", -100.0, now)
    assert g.state.state != STOOD_DOWN
    g.record_trade_close("NIFTY", -100.0, now)
    assert g.state.state == STOOD_DOWN


def test_governor_win_resets_streak():
    g = DailyGovernor(GovernorParams(consecutive_loss_stop=3, daily_profit_lock=None,
                                     max_daily_loss=1_000_000.0))
    now = _in_session()
    g.reset_session(now.date())
    g.record_trade_close("NIFTY", -100.0, now)
    g.record_trade_close("NIFTY", -100.0, now)
    g.record_trade_close("NIFTY", +50.0, now)    # win resets
    g.record_trade_close("NIFTY", -100.0, now)
    g.record_trade_close("NIFTY", -100.0, now)
    assert g.state.state != STOOD_DOWN           # only 2 in a row after reset
    assert g.state.consec_losses == 2


def test_governor_scratch_not_a_loss():
    g = DailyGovernor(GovernorParams(consecutive_loss_stop=2, daily_profit_lock=None))
    now = _in_session()
    g.reset_session(now.date())
    g.record_trade_close("NIFTY", -100.0, now)
    g.record_trade_close("NIFTY", 0.0, now)      # scratch — does NOT advance
    assert g.state.consec_losses == 1
    assert g.state.state != STOOD_DOWN


def test_governor_caps_block_opens_per_underlying_and_aggregate():
    g = DailyGovernor(GovernorParams(max_trades_per_day=3,
                                     max_trades_per_underlying=2,
                                     max_concurrent_total=10,
                                     max_concurrent_per_underlying=10,
                                     daily_profit_lock=None))
    now = _in_session()
    g.reset_session(now.date())
    g.record_trade_open("NIFTY")
    g.record_trade_open("NIFTY")
    g.recompute(now)
    assert not g.allows_open("NIFTY")            # per-underlying cap (2)
    assert g.allows_open("BANKNIFTY")            # still room on the sleeve (3)
    g.record_trade_open("BANKNIFTY")
    g.recompute(now)
    assert not g.allows_open("BANKNIFTY")        # sleeve cap (3) now reached


def test_governor_concurrency_caps():
    g = DailyGovernor(GovernorParams(max_concurrent_total=3,
                                     max_concurrent_per_underlying=2,
                                     max_trades_per_day=100,
                                     max_trades_per_underlying=100,
                                     daily_profit_lock=None))
    now = _in_session()
    g.reset_session(now.date())
    g.record_open("NIFTY", 2)
    g.recompute(now)
    assert not g.allows_open("NIFTY")            # per-underlying concurrency (2)
    assert g.allows_open("BANKNIFTY")
    g.record_open("BANKNIFTY", 1)
    g.recompute(now)
    assert not g.allows_open("BANKNIFTY")        # sleeve concurrency (3) reached
    g.record_close("NIFTY", 1)
    g.recompute(now)
    assert g.allows_open("BANKNIFTY")            # room again


def test_governor_session_reset_clears_day():
    g = DailyGovernor(GovernorParams(daily_profit_lock=None, max_daily_loss=1000.0))
    day1 = _in_session()
    g.reset_session(day1.date())
    g.record_open("NIFTY", 1)
    g.record_trade_close("NIFTY", -1000.0, day1)
    assert g.state.state == STOOD_DOWN
    day2 = day1 + timedelta(days=1)
    g.recompute(day2)                            # date change → reset
    assert g.state.state == ACTIVE
    assert g.summary()["aggregate"]["realized_pnl"] == 0.0


def test_governor_loss_cap_is_absolute_floor():
    # Lock armed at a low arm; a deep loss path must defend max(floor, -cap).
    g = DailyGovernor(GovernorParams(daily_profit_lock=2000.0,
                                     profit_lock_floor_frac=0.5,
                                     max_daily_loss=5000.0))
    now = _in_session()
    g.reset_session(now.date())
    g.record_trade_close("NIFTY", 2500.0, now)   # arm; floor 1000
    assert g.defended_floor == pytest.approx(1000.0)   # profit floor above -cap
    # the absolute hard floor is never undercut by lock math
    assert g.defended_floor >= -g.p.max_daily_loss


# ===========================================================================
# ScalperOrchestrator — integration with real Portfolio/RiskEngine
# ===========================================================================


def test_exits_run_before_entries_each_tick():
    """An EXIT signal queued ahead of an ENTER must be processed first: the open
    position is flattened on this tick, and the entry scan does not re-open it
    in the same tick (cooldown engages on the completed exit)."""
    orch, signals, resolver, portfolio, risk = _build()
    now = _in_session()

    # Seed an open NIFTY position directly via a BUY fill so there is something
    # to exit, and register the leg with the orchestrator.
    leg = resolver.legs["NIFTY"]
    _run(portfolio.apply_fill(
        __import__("engine.types", fromlist=["Fill"]).Fill(
            security_id=leg.security_id, side="BUY", qty=leg.lot, price=100.0,
            mode="PAPER"), strategy="options_scalper"))
    orch._legs["NIFTY"][leg.security_id] = leg
    orch.governor.record_open("NIFTY", 1)
    orch.state = "ARMED"
    orch._session_date = now.date()
    orch.governor.reset_session(now.date())
    orch.governor.record_open("NIFTY", 1)

    # Queue EXIT (consumed by _manage_exits) then ENTER (consumed by entry scan).
    signals.push("NIFTY", _exit("NIFTY"))
    signals.push("NIFTY", _enter("NIFTY"))

    _run(orch.tick(now))

    # The position was flattened (exit ran first) ...
    assert portfolio.get(leg.security_id).qty == 0
    # ... and the per-index cooldown engaged, so the queued ENTER did not re-open.
    assert orch._cooldown_until["NIFTY"] is not None
    assert orch._cooldown_until["NIFTY"] > now
    # the SELL fill is recorded; no follow-on BUY this tick
    sides = [f[1] for f in signals.fills]
    assert "SELL" in sides
    assert sides.count("BUY") == 0


def test_eod_squareoff_unconditional_above_governor():
    """Past the EOD square-off, an open position is flattened even when the
    governor is STOOD_DOWN — the square-off is above the governor."""
    orch, signals, resolver, portfolio, risk = _build()
    leg = resolver.legs["NIFTY"]
    _run(portfolio.apply_fill(
        __import__("engine.types", fromlist=["Fill"]).Fill(
            security_id=leg.security_id, side="BUY", qty=leg.lot, price=100.0,
            mode="PAPER"), strategy="options_scalper"))
    # square-off for EQUITY = 15:30 − 15min = 15:15 IST
    now = _in_session(hour=15, minute=20)
    orch._legs["NIFTY"][leg.security_id] = leg
    orch._session_date = now.date()
    orch.governor.reset_session(now.date())
    orch.governor.record_open("NIFTY", 1)
    # Force a STOOD_DOWN governor — the EOD flatten must ignore it.
    orch.governor.state.state = STOOD_DOWN

    _run(orch.tick(now))
    assert portfolio.get(leg.security_id).qty == 0
    assert orch.state in ("DONE",)


def test_eod_squareoff_blocks_new_entries():
    """No ENTER is taken past the square-off even with a fresh ENTER queued."""
    orch, signals, resolver, portfolio, risk = _build()
    now = _in_session(hour=15, minute=20)
    orch._session_date = now.date()
    orch.governor.reset_session(now.date())
    orch.state = "ARMED"
    signals.push("NIFTY", _enter("NIFTY"))
    _run(orch.tick(now))
    assert portfolio.get(resolver.legs["NIFTY"].security_id).qty == 0
    assert all(f[1] != "BUY" for f in signals.fills)


def test_governor_caps_block_entries_not_exits():
    """A STOOD_DOWN governor blocks a new ENTER but a queued EXIT still flattens."""
    orch, signals, resolver, portfolio, risk = _build()
    now = _in_session()
    leg = resolver.legs["NIFTY"]
    _run(portfolio.apply_fill(
        __import__("engine.types", fromlist=["Fill"]).Fill(
            security_id=leg.security_id, side="BUY", qty=leg.lot, price=100.0,
            mode="PAPER"), strategy="options_scalper"))
    orch._legs["NIFTY"][leg.security_id] = leg
    orch._session_date = now.date()
    orch.governor.reset_session(now.date())
    orch.governor.record_open("NIFTY", 1)
    # Stand the governor down (loss-cap style) but keep request_flatten off so we
    # can prove the EXIT path itself works under STOOD_DOWN.
    orch.governor.state.state = STOOD_DOWN
    orch.state = "ARMED"

    # ENTER for BANKNIFTY (blocked) + EXIT for NIFTY (allowed).
    signals.push("NIFTY", _exit("NIFTY"))
    signals.push("BANKNIFTY", _enter("BANKNIFTY"))
    _run(orch.tick(now))

    assert portfolio.get(leg.security_id).qty == 0          # exit honoured
    assert portfolio.get(resolver.legs["BANKNIFTY"].security_id).qty == 0  # entry blocked


def test_entry_lot_aligned_sizing():
    """Sizing is whole-lots: a 2-lot BANKNIFTY entry buys exactly 2×lot units."""
    orch, signals, resolver, portfolio, risk = _build()
    now = _in_session()
    orch._session_date = now.date()
    orch.governor.reset_session(now.date())
    orch.state = "ARMED"
    signals.push("BANKNIFTY", _enter("BANKNIFTY", lots=2))
    _run(orch.tick(now))
    leg = resolver.legs["BANKNIFTY"]
    qty = portfolio.get(leg.security_id).qty
    assert qty == 2 * leg.lot                # 2 × 15 = 30, a whole-lot multiple
    assert qty % leg.lot == 0


def test_cooldown_per_index_independent():
    """A completed NIFTY exit sets a NIFTY cooldown but BANKNIFTY is unaffected."""
    orch, signals, resolver, portfolio, risk = _build()
    now = _in_session()
    nleg = resolver.legs["NIFTY"]
    _run(portfolio.apply_fill(
        __import__("engine.types", fromlist=["Fill"]).Fill(
            security_id=nleg.security_id, side="BUY", qty=nleg.lot, price=100.0,
            mode="PAPER"), strategy="options_scalper"))
    orch._legs["NIFTY"][nleg.security_id] = nleg
    orch._session_date = now.date()
    orch.governor.reset_session(now.date())
    orch.governor.record_open("NIFTY", 1)
    orch.state = "ARMED"

    signals.push("NIFTY", _exit("NIFTY"))
    # BANKNIFTY: no exit; a fresh ENTER should be taken (no cooldown).
    signals.push("BANKNIFTY", _enter("BANKNIFTY"))
    _run(orch.tick(now))

    assert orch._cooldown_until["NIFTY"] is not None and orch._cooldown_until["NIFTY"] > now
    assert orch._cooldown_until["BANKNIFTY"] is None
    # BANKNIFTY entry went on (1 lot) — unaffected by NIFTY's cooldown
    bleg = resolver.legs["BANKNIFTY"]
    assert portfolio.get(bleg.security_id).qty == bleg.lot


def test_governor_request_flatten_routes_through_orchestrator():
    """When the governor trips its loss cap with a position open, the orchestrator
    consumes the flatten request and flattens the residual (governor never owns
    its own exit)."""
    orch, signals, resolver, portfolio, risk = _build(
        gov_params=GovernorParams(max_daily_loss=4000.0, daily_profit_lock=None))
    now = _in_session()
    leg = resolver.legs["NIFTY"]
    _run(portfolio.apply_fill(
        __import__("engine.types", fromlist=["Fill"]).Fill(
            security_id=leg.security_id, side="BUY", qty=leg.lot, price=100.0,
            mode="PAPER"), strategy="options_scalper"))
    orch._legs["NIFTY"][leg.security_id] = leg
    orch._session_date = now.date()
    orch.governor.reset_session(now.date())
    orch.governor.record_open("NIFTY", 1)
    # Drive realized to the loss cap → governor will request a flatten on recompute.
    orch.governor.record_trade_close("NIFTY", -4000.0, now)
    orch.state = "ARMED"

    _run(orch.tick(now))
    assert portfolio.get(leg.security_id).qty == 0


def test_boot_reconciles_open_scalper_contracts():
    """A scalper position restored by the Portfolio is reconciled into MANAGING
    and registered with the governor's concurrency meter on boot."""
    orch, signals, resolver, portfolio, risk = _build()
    leg = resolver.legs["NIFTY"]
    # Simulate a restored position (as reconcile_on_boot would leave it).
    from engine.types import Position
    portfolio.positions[leg.security_id] = Position(
        security_id=leg.security_id, qty=leg.lot, avg_price=100.0,
        strategy="options_scalper")
    _run(orch.boot())
    assert leg.security_id in orch._legs["NIFTY"]
    assert orch.state == "MANAGING"
    assert orch.governor.summary()["aggregate"]["open_contracts"] == 1


def test_restart_safe_governor_seed_no_db():
    """With no DB backend the governor meter starts at zero and boot does not
    raise (documented PAPER restart-resets-the-day gap)."""
    orch, signals, resolver, portfolio, risk = _build()
    _run(orch.boot())
    assert orch.governor.summary()["aggregate"]["trades_opened"] == 0


def test_session_reset_on_date_change_clears_legs_and_cooldown():
    orch, signals, resolver, portfolio, risk = _build()
    d1 = _in_session()
    orch._session_date = d1.date()
    orch._cooldown_until["NIFTY"] = d1 + timedelta(minutes=3)
    orch._legs["NIFTY"]["x"] = resolver.legs["NIFTY"]
    d2 = d1 + timedelta(days=1)
    _run(orch.tick(d2))
    assert orch._cooldown_until["NIFTY"] is None
    assert orch._legs["NIFTY"] == {}


def test_trail_only_blocks_entries_but_allows_exits():
    """Under a TRAIL_ONLY governor: a queued ENTER is blocked, but a queued EXIT
    on an open contract still flattens (exits are never governor-gated)."""
    orch, signals, resolver, portfolio, risk = _build(
        gov_params=GovernorParams(daily_profit_lock=6000.0,
                                  profit_lock_floor_frac=0.5,
                                  profit_lock_giveback=2500.0,
                                  profit_lock_mode="trail_only"))
    now = _in_session()
    leg = resolver.legs["NIFTY"]
    _run(portfolio.apply_fill(
        __import__("engine.types", fromlist=["Fill"]).Fill(
            security_id=leg.security_id, side="BUY", qty=leg.lot, price=100.0,
            mode="PAPER"), strategy="options_scalper"))
    orch._legs["NIFTY"][leg.security_id] = leg
    orch._session_date = now.date()
    orch.governor.reset_session(now.date())
    orch.governor.record_open("NIFTY", 1)
    # Arm the lock, then break the floor → TRAIL_ONLY.
    orch.governor.record_trade_close("NIFTY", 6500.0, now)   # arm; floor 4000
    orch.governor.record_trade_close("NIFTY", -3000.0, now)  # realized 3500 ≤ floor
    assert orch.governor.state.state == TRAIL_ONLY
    orch.state = "ARMED"

    signals.push("NIFTY", _exit("NIFTY"))         # exit on the open contract
    signals.push("BANKNIFTY", _enter("BANKNIFTY"))  # entry — blocked by TRAIL_ONLY
    _run(orch.tick(now))

    assert portfolio.get(leg.security_id).qty == 0                         # exit honoured
    assert portfolio.get(resolver.legs["BANKNIFTY"].security_id).qty == 0  # entry blocked


def test_partial_exit_no_cooldown_until_flat():
    """A partial exit (lots < open) does NOT set the cooldown — only a full
    flatten (qty -> 0) engages it."""
    orch, signals, resolver, portfolio, risk = _build()
    now = _in_session()
    leg = resolver.legs["NIFTY"]
    # Open 2 lots.
    _run(portfolio.apply_fill(
        __import__("engine.types", fromlist=["Fill"]).Fill(
            security_id=leg.security_id, side="BUY", qty=2 * leg.lot, price=100.0,
            mode="PAPER"), strategy="options_scalper"))
    orch._legs["NIFTY"][leg.security_id] = leg
    orch._session_date = now.date()
    orch.governor.reset_session(now.date())
    orch.governor.record_open("NIFTY", 1)
    orch.state = "ARMED"
    # Exit only 1 lot.
    signals.push("NIFTY", _exit("NIFTY", lots=1))
    _run(orch.tick(now))
    assert portfolio.get(leg.security_id).qty == leg.lot          # 1 lot still open
    assert orch._cooldown_until["NIFTY"] is None                  # no cooldown yet


def test_zero_lot_leg_exit_does_not_crash():
    """A leg with lot==0 must not divide-by-zero on exit; the exit is skipped and
    left to the EOD flatten (the position stays on the book, no crash)."""
    orch, signals, resolver, portfolio, risk = _build()
    now = _in_session()
    bad = OptionLeg(underlying="NIFTY", security_id="BAD", exchange_segment="NSE_FNO",
                    option_type="CE", strike=22000, lot=0, ltp=100.0)
    _run(portfolio.apply_fill(
        __import__("engine.types", fromlist=["Fill"]).Fill(
            security_id="BAD", side="BUY", qty=65, price=100.0, mode="PAPER"),
        strategy="options_scalper"))
    orch._legs["NIFTY"]["BAD"] = bad
    orch._session_date = now.date()
    orch.governor.reset_session(now.date())
    orch.state = "ARMED"
    signals.push("NIFTY", _exit("NIFTY"))
    _run(orch.tick(now))   # must not raise ZeroDivisionError
    # the position is left on the book (skipped) — no crash is the assertion
    assert portfolio.get("BAD").qty == 65


def test_boot_seed_does_not_clobber_reconciled_concurrency():
    """The journal seed must NOT overwrite the per-underlying concurrency that
    reconcile set (regression: seed once passed the aggregate total)."""
    g = DailyGovernor(GovernorParams())
    now = _in_session()
    g.reset_session(now.date())
    # reconcile sets concurrency: 2 NIFTY + 1 BANKNIFTY = 3 aggregate
    g.record_open("NIFTY", 2)
    g.record_open("BANKNIFTY", 1)
    # seed the day's trade-count to the first underlying (no concurrency arg)
    g.seed_trades_today("NIFTY", trades_opened=3, realized_pnl=-500.0)
    s = g.summary()
    assert s["by_underlying"]["NIFTY"]["open_contracts"] == 2     # NOT clobbered to 3
    assert s["by_underlying"]["BANKNIFTY"]["open_contracts"] == 1
    assert s["aggregate"]["open_contracts"] == 3
    assert s["aggregate"]["trades_opened"] == 3
    assert s["aggregate"]["realized_pnl"] == -500.0


def test_pre_open_takes_no_entries():
    """Before OPEN + warmup, no entries even with a queued ENTER."""
    orch, signals, resolver, portfolio, risk = _build()
    now = _in_session(hour=9, minute=20)   # 09:20, before 09:15+15=09:30 warmup
    orch._session_date = now.date()
    orch.governor.reset_session(now.date())
    signals.push("NIFTY", _enter("NIFTY"))
    _run(orch.tick(now))
    assert orch.state == "PRE_OPEN"
    assert portfolio.get(resolver.legs["NIFTY"].security_id).qty == 0
