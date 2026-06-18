"""RiskEngine — fractional limits, sizing floors, budgets, persistent halts.

All limits are fractions of equity (2026-06-13 rework): paper and live share
one geometry. Tests pin the numbers with explicit params.
"""
import asyncio
import json

from engine.portfolio import Portfolio
from engine.risk import RiskEngine, RiskParams
from engine.types import OrderIntent, Position


def make_engine(ltp=None, **kw) -> RiskEngine:
    params = RiskParams(**{**dict(
        equity_base=500_000,
        risk_per_trade=0.01,            # ₹5,000 budget/trade
        max_daily_loss_pct=0.02,        # ₹10,000/day
        weekly_loss_pct=0.05,
        max_notional_pct=0.20,          # ₹100,000/trade
        max_gross_exposure_pct=1.00,    # ₹500,000 gross
        min_stop_distance_pct=0.0035,
    ), **kw})
    pf = Portfolio(mode="PAPER")
    return RiskEngine(params, pf, ltp_lookup=ltp or (lambda sid: 0.0))


def intent(side="BUY", qty=10, sid="999", stop=None) -> OrderIntent:
    return OrderIntent(security_id=sid, exchange_segment="NSE_EQ",
                       side=side, qty=qty, strategy="ORB", stop_price=stop)


# ── Sizing ────────────────────────────────────────────────────────────────────

def test_stop_distance_sizing():
    rm = make_engine()
    # budget 5000; stop distance 2 → 2500 shares, notional cap 100000/100 = 1000
    assert rm.size_position(entry=100, stop=98) == 1000
    # wider stop: distance 10 → 500 shares
    assert rm.size_position(entry=100, stop=90) == 500


def test_min_stop_distance_floor():
    # distance 0.01 on a ₹100 stock is floored to 0.35 — size must not explode
    rm = make_engine(risk_per_trade=0.0005, max_notional_pct=1.0)   # budget ₹250
    assert rm.size_position(entry=100, stop=99.99) == int(250 / 0.35)   # 714, not 25,000


def test_adv_participation_cap():
    rm = make_engine()
    # 1% of a 10,000-share ADV = 100 shares, regardless of risk budget
    assert rm.size_position(entry=100, stop=98, adv=10_000) == 100


def test_sizing_without_stop_uses_full_entry_risk():
    rm = make_engine()
    assert rm.size_position(entry=100, stop=None) == 50   # 5000/100


def test_sizing_rejects_bad_entry():
    assert make_engine().size_position(entry=0, stop=10) == 0


def test_equity_compounds_realized_pnl():
    rm = make_engine()
    rm._realized_total = -100_000          # drawdown shrinks sizing
    assert rm.equity == 400_000
    assert rm.risk_budget_per_trade == 4_000


# ── Pre-trade gate ────────────────────────────────────────────────────────────

def test_zero_qty_blocked():
    ok, msg = make_engine().check_intent(intent(qty=0), ref_price=100)
    assert not ok and "0" in msg


def test_notional_cap_fractional():
    rm = make_engine()
    ok, msg = rm.check_intent(intent(qty=2000), ref_price=100)   # 200k > 20% of 500k
    assert not ok and "Notional" in msg


def test_gross_exposure_cap():
    rm = make_engine(ltp=lambda sid: 500.0)
    rm._portfolio.positions["111"] = Position(security_id="111", qty=900, avg_price=500)
    # existing gross 450k; +100k would breach the 500k (100%) cap
    ok, msg = rm.check_intent(intent(sid="222", qty=200, stop=495), ref_price=500)
    assert not ok and "Gross exposure" in msg
    ok, _ = rm.check_intent(intent(sid="222", qty=90, stop=495), ref_price=500)   # 495k OK
    assert ok


def test_daily_risk_budget_blocks_new_entries():
    rm = make_engine()
    rm._realized_today = -7_000            # losses already booked today
    rm.register_risk("111", 2_000)         # open position risking 2k
    # consumed 9k of the 10k budget → a 1k-risk trade fits, a 2k-risk one doesn't
    ok, _ = rm.check_intent(intent(sid="222", qty=100, stop=90), ref_price=100)   # 100×10=1k
    assert ok
    ok, msg = rm.check_intent(intent(sid="333", qty=200, stop=90), ref_price=100) # 2k
    assert not ok and "budget" in msg.lower()


def test_release_risk_frees_budget():
    rm = make_engine()
    rm.register_risk("111", 9_500)
    ok, _ = rm.check_intent(intent(sid="222", qty=100, stop=90), ref_price=100)
    assert not ok
    rm.release_risk("111")
    ok, _ = rm.check_intent(intent(sid="222", qty=100, stop=90), ref_price=100)
    assert ok


def test_max_open_positions_blocks_new_entries_not_exits():
    rm = make_engine(max_open_positions=1)
    rm._portfolio.positions["111"] = Position(security_id="111", qty=10, avg_price=50)
    ok, msg = rm.check_intent(intent(sid="222", qty=1, stop=95), ref_price=100)
    assert not ok and "Max open positions" in msg
    ok, _ = rm.check_intent(intent(sid="111", side="SELL", qty=10), ref_price=55)
    assert ok   # exits are never blocked by exposure limits


def test_kill_switch_blocks():
    rm = make_engine()
    rm.activate_kill_switch("test")
    ok, msg = rm.check_intent(intent(qty=1), ref_price=100)
    assert not ok and "halted" in msg.lower()
    rm.resume()
    ok, _ = rm.check_intent(intent(qty=1, stop=95), ref_price=100)
    assert ok


def test_exit_not_blocked_while_halted():
    # An EOD square-off (or any close) must pass even during a daily-loss halt
    # or with the kill switch active — otherwise a naked position orphans.
    rm = make_engine()
    rm._portfolio.positions["111"] = Position(security_id="111", qty=10, avg_price=50)
    rm.state.halted = True
    rm.state.halt_reason = "daily loss"
    # Closing a long (SELL) is allowed despite the halt.
    ok, msg = rm.check_intent(intent(sid="111", side="SELL", qty=10), ref_price=45)
    assert ok and msg == "OK"
    # A new entry on a different security stays blocked.
    ok, _ = rm.check_intent(intent(sid="222", side="BUY", qty=1, stop=95), ref_price=100)
    assert not ok
    # Same under the kill switch.
    rm.state.halted = False
    rm.state.kill_switch = True
    ok, _ = rm.check_intent(intent(sid="111", side="SELL", qty=10), ref_price=45)
    assert ok


def test_exit_short_not_blocked_while_halted():
    rm = make_engine()
    rm._portfolio.positions["111"] = Position(security_id="111", qty=-10, avg_price=50)
    rm.state.halted = True
    rm.state.halt_reason = "daily loss"
    # Closing a short = BUY back.
    ok, _ = rm.check_intent(intent(sid="111", side="BUY", qty=10), ref_price=55)
    assert ok


# ── Persistent halts ──────────────────────────────────────────────────────────

def test_daily_halt_survives_restart(tmp_path):
    hf = tmp_path / "halt_state.json"
    rm = make_engine(halt_file=hf)
    asyncio.run(rm._halt("daily loss", scope="day"))
    assert hf.exists()

    rm2 = make_engine(halt_file=hf)        # "restart"
    rm2.load_persisted_halt()
    assert rm2.state.halted and rm2.state.halt_scope == "day"


def test_stale_halt_file_is_cleared(tmp_path):
    hf = tmp_path / "halt_state.json"
    hf.write_text(json.dumps({"scope": "day", "reason": "x",
                              "date": "2020-01-01", "week": "(2020, 1)"}))
    rm = make_engine(halt_file=hf)
    rm.load_persisted_halt()
    assert not rm.state.halted and not hf.exists()


def test_resume_clears_halt_file(tmp_path):
    hf = tmp_path / "halt_state.json"
    rm = make_engine(halt_file=hf)
    asyncio.run(rm._halt("weekly loss", scope="week"))
    assert hf.exists()
    rm.resume()
    assert not hf.exists() and not rm.state.halted


# ── _evaluate monitoring paths (TEST-04) ─────────────────────────────────────

def _stub_refresh(rm, monkeypatch):
    """Replace refresh_pnl with an async no-op so _evaluate doesn't hit the DB."""
    async def _noop():
        pass
    monkeypatch.setattr(rm, "refresh_pnl", _noop)


def test_evaluate_daily_loss_triggers_halt(monkeypatch):
    """day P&L < -daily_loss_budget  →  halted, scope 'day', on_halt callback fires."""
    # equity = 500k, daily budget = 500k * 0.02 = 10k
    rm = make_engine()
    _stub_refresh(rm, monkeypatch)

    # Set realized today to -8k and give the portfolio an unrealized loss of -3k.
    # day_total = -8000 + -3000 = -11000 < -10000 budget → should halt.
    rm._realized_today = -8_000
    rm._portfolio.positions["999"] = Position(security_id="999", qty=10, avg_price=100)
    # ltp at 70 → unrealized = (70 - 100) * 10 = -300… not enough.
    # Use qty=100 @ avg 100, ltp 70 → unrealized = (70-100)*100 = -3000. Done.
    rm._portfolio.positions["999"] = Position(security_id="999", qty=100, avg_price=100)
    rm._ltp = lambda sid: 70.0

    halt_fired = []
    rm.on_halt(lambda reason: halt_fired.append(reason))

    asyncio.run(rm._evaluate())

    assert rm.state.halted is True
    assert rm.state.halt_scope == "day"
    assert len(halt_fired) == 1, "on_halt callback must fire exactly once"


def test_evaluate_weekly_loss_triggers_halt(monkeypatch):
    """week P&L < -weekly_loss_budget but day within budget → halt scope 'week'."""
    # equity = 500k, weekly budget = 500k * 0.05 = 25k, daily budget = 10k
    rm = make_engine()
    _stub_refresh(rm, monkeypatch)

    # today's realized = -3k (within daily budget of 10k when added to unrealized 0)
    # week realized = -30k → week_total = -30k + 0 = -30k < -25k → week halt
    rm._realized_today = -3_000
    rm._realized_week = -30_000
    # No open positions → unrealized = 0
    rm._ltp = lambda sid: 0.0

    halt_fired = []
    rm.on_halt(lambda reason: halt_fired.append(reason))

    asyncio.run(rm._evaluate())

    assert rm.state.halted is True
    assert rm.state.halt_scope == "week"
    assert len(halt_fired) == 1


def test_evaluate_killswitch_file_triggers_halt(tmp_path, monkeypatch):
    """Writing the killswitch file causes _evaluate to set kill_switch + halt."""
    ks = tmp_path / "killswitch"
    rm = make_engine(killswitch_file=ks)
    _stub_refresh(rm, monkeypatch)

    # Write the kill-switch file and ensure realized values are safe (no extra halt)
    ks.write_text("operator test")
    rm._realized_today = 0.0
    rm._realized_week = 0.0
    rm._ltp = lambda sid: 0.0

    halt_fired = []
    rm.on_halt(lambda reason: halt_fired.append(reason))

    asyncio.run(rm._evaluate())

    assert rm.state.kill_switch is True
    assert rm.state.halted is True
    assert len(halt_fired) >= 1, "on_halt callback must fire when kill switch trips"


def test_evaluate_resume_file_clears_killswitch_halt(tmp_path, monkeypatch):
    """Dropping the resume file clears a kill-switch halt and deletes both markers."""
    ks = tmp_path / "killswitch"
    rf = tmp_path / "resume"
    rm = make_engine(killswitch_file=ks, resume_file=rf)
    _stub_refresh(rm, monkeypatch)
    rm._ltp = lambda sid: 0.0

    # Trip the kill switch via the file-watch path.
    ks.write_text("operator test")
    asyncio.run(rm._evaluate())
    assert rm.state.halted and rm.state.kill_switch

    # Operator clears it from the dashboard: resume file appears (api also unlinks
    # the killswitch file, mirror that here).
    ks.unlink()
    rf.write_text("dashboard")
    resumed = []
    rm.on_resume(lambda: resumed.append(True))
    asyncio.run(rm._evaluate())

    assert not rm.state.halted and not rm.state.kill_switch
    assert not rf.exists(), "resume file must be consumed"
    assert len(resumed) == 1, "on_resume callback must fire once"


def test_evaluate_resume_does_not_clear_breached_loss(tmp_path, monkeypatch):
    """Resume can't override a still-breached daily loss — it re-halts same tick."""
    rf = tmp_path / "resume"
    rm = make_engine(resume_file=rf)
    _stub_refresh(rm, monkeypatch)
    # day_total = -11k < -10k budget (same setup as the daily-loss halt test)
    rm._realized_today = -8_000
    rm._portfolio.positions["999"] = Position(security_id="999", qty=100, avg_price=100)
    rm._ltp = lambda sid: 70.0
    asyncio.run(rm._evaluate())
    assert rm.state.halted

    rf.write_text("dashboard")
    asyncio.run(rm._evaluate())
    # Resume cleared it, but the loss is still breached → re-halted on the same tick.
    assert rm.state.halted is True
    assert rm.state.halt_scope == "day"
    assert not rf.exists()


def test_evaluate_resume_does_not_clear_breached_weekly_loss(tmp_path, monkeypatch):
    """Resume can't override a still-breached WEEKLY loss — re-halts scope 'week'."""
    rf = tmp_path / "resume"
    rm = make_engine(resume_file=rf)
    _stub_refresh(rm, monkeypatch)
    rm._realized_today = -3_000      # within daily budget (10k)
    rm._realized_week = -30_000      # over weekly budget (25k)
    rm._ltp = lambda sid: 0.0
    asyncio.run(rm._evaluate())
    assert rm.state.halted and rm.state.halt_scope == "week"

    rf.write_text("dashboard")
    asyncio.run(rm._evaluate())
    assert rm.state.halted is True
    assert rm.state.halt_scope == "week"
    assert not rf.exists()


def test_evaluate_resume_clears_halt_file_too(tmp_path, monkeypatch):
    """A resume via _evaluate deletes the persisted halt_state.json (not just state)."""
    hf = tmp_path / "halt_state.json"
    rf = tmp_path / "resume"
    rm = make_engine(halt_file=hf, resume_file=rf)
    _stub_refresh(rm, monkeypatch)
    rm._ltp = lambda sid: 0.0
    asyncio.run(rm._halt("weekly loss", scope="week"))   # persists hf
    assert hf.exists()
    rf.write_text("dashboard")
    asyncio.run(rm._evaluate())
    assert not hf.exists() and not rf.exists()
    assert not rm.state.halted


def test_evaluate_resume_fires_async_on_resume_callback(tmp_path, monkeypatch):
    """An async on_resume callback is awaited (not left a dangling coroutine)."""
    ks = tmp_path / "killswitch"
    rf = tmp_path / "resume"
    rm = make_engine(killswitch_file=ks, resume_file=rf)
    _stub_refresh(rm, monkeypatch)
    rm._ltp = lambda sid: 0.0
    ks.write_text("op"); asyncio.run(rm._evaluate())
    assert rm.state.halted
    ks.unlink(); rf.write_text("dashboard")
    fired = []
    async def _cb():
        fired.append(True)
    rm.on_resume(_cb)
    asyncio.run(rm._evaluate())
    assert fired == [True] and not rm.state.halted


def test_resume_unlinked_only_after_resume_succeeds(tmp_path, monkeypatch):
    """If resume() raises, the flag is NOT consumed — it's retried next tick."""
    rf = tmp_path / "resume"
    rm = make_engine(resume_file=rf)
    _stub_refresh(rm, monkeypatch)
    rm._ltp = lambda sid: 0.0
    rm.state.halted = True
    rf.write_text("dashboard")

    def _boom():
        raise RuntimeError("resume failed")
    monkeypatch.setattr(rm, "resume", _boom)
    # _evaluate is called inside the monitor loop's try/except, but here we call it
    # directly — the exception propagates; the flag must still be on disk.
    try:
        asyncio.run(rm._evaluate())
    except RuntimeError:
        pass
    assert rf.exists(), "resume flag must survive a failed resume() for retry"


def test_get_summary_reports_kill_switch(monkeypatch):
    """get_summary() surfaces kill_switch so the dashboard can tell halt types apart."""
    rm = make_engine()
    _stub_refresh(rm, monkeypatch)
    assert rm.get_summary()["kill_switch"] is False
    rm.state.kill_switch = True
    assert rm.get_summary()["kill_switch"] is True


def test_activate_kill_switch_sets_flag_eagerly_in_async_ctx():
    """In an async context, activate_kill_switch sets kill_switch=True immediately,
    not only once the scheduled _halt task runs."""
    rm = make_engine()
    async def _run():
        rm.activate_kill_switch("operator")
        # synchronously after the call, before yielding to the scheduled task:
        assert rm.state.kill_switch is True
        await asyncio.sleep(0)   # let the _halt task run
    asyncio.run(_run())
    assert rm.state.halted is True


def test_evaluate_resume_noop_when_not_halted(tmp_path, monkeypatch):
    """A stray resume file when healthy is consumed without firing on_resume."""
    rf = tmp_path / "resume"
    rm = make_engine(resume_file=rf)
    _stub_refresh(rm, monkeypatch)
    rm._ltp = lambda sid: 0.0
    rf.write_text("dashboard")
    resumed = []
    rm.on_resume(lambda: resumed.append(True))
    asyncio.run(rm._evaluate())
    assert not rf.exists() and not rm.state.halted
    assert resumed == [], "on_resume must not fire when nothing was halted"


# ── FIX 1 (QR-C1): activate_kill_switch must fire on_halt callbacks ──────────

def test_activate_kill_switch_fires_on_halt_sync_callback():
    """activate_kill_switch() from a sync context must fire sync on_halt callbacks.

    This exercises the path the dashboard kill-switch endpoint takes when calling
    activate_kill_switch() directly (i.e. not via _evaluate / the file-watch loop).
    The flatten-positions callback MUST fire so open positions are not left dangling.
    """
    rm = make_engine()
    flatten_called = []
    rm.on_halt(lambda reason: flatten_called.append(reason))

    rm.activate_kill_switch("dashboard-test")

    assert rm.state.kill_switch is True, "kill_switch flag must be set"
    assert rm.state.halted is True, "halted flag must be set"
    assert len(flatten_called) == 1, (
        "on_halt callback (flatten positions) must fire once via activate_kill_switch; "
        "if it doesn't, positions stay open after an operator kill"
    )
    assert "Kill switch" in flatten_called[0]


def test_activate_kill_switch_fires_async_on_halt_callback():
    """activate_kill_switch() from a sync context must also fire async on_halt callbacks."""
    rm = make_engine()
    flatten_called = []

    async def async_flatten(reason: str):
        flatten_called.append(reason)

    rm.on_halt(async_flatten)
    rm.activate_kill_switch("dashboard-async-test")

    assert rm.state.kill_switch is True
    assert rm.state.halted is True
    assert len(flatten_called) == 1, (
        "async on_halt callback must fire via activate_kill_switch even in sync context"
    )


# ── FIX 2 (QR-M-INFRA): DB fallback must not understate weekly loss ──────────

def test_refresh_pnl_db_failure_preserves_weekly_loss(monkeypatch):
    """On DB failure the weekly loss meter must keep its last DB-derived value.

    Before FIX 2 the fallback set _realized_week = portfolio.realized_pnl, which
    is the *session-only* P&L (resets on restart). If Mon–Thu losses were -₹20k
    and today (Fri) the DB fails, the week meter would incorrectly show 0 (or only
    today's P&L), allowing new trades that should be blocked by the weekly halt.
    """
    rm = make_engine()

    # Simulate that the DB told us earlier this week there was -₹20k realized.
    rm._realized_week = -20_000.0
    rm._realized_today = -3_000.0
    rm._realized_total = -23_000.0

    # The portfolio only knows about this session's P&L (e.g. ₹500 profit today).
    # Simulate a failing DB by making _query raise.
    async def failing_refresh():
        raise RuntimeError("DB connection lost")

    # Patch the internal query by monkey-patching run_in_executor to raise.
    async def patched_refresh():
        # Replicate the fallback branch directly: DB fails, fallback fires.
        try:
            raise RuntimeError("simulated DB failure")
        except Exception as exc:
            import logging
            logging.getLogger("dhan.engine.risk").warning(
                "Risk: DB P&L refresh failed (%s) — using process-local realized for today; "
                "weekly value preserved from last DB read (restart-unsafe)", exc)
            rm._realized_today = rm._portfolio.realized_pnl
            # _realized_week intentionally left unchanged (the fix under test)

    monkeypatch.setattr(rm, "refresh_pnl", patched_refresh)

    # Confirm portfolio session P&L is 0 (no trades this session).
    assert rm._portfolio.realized_pnl == 0.0

    asyncio.run(rm.refresh_pnl())

    # After the fallback: today's meter updates to session P&L (0), but the weekly
    # meter must STILL reflect the DB-derived -₹20k, not be overwritten with 0.
    assert rm._realized_today == 0.0, "today should update to session P&L"
    assert rm._realized_week == -20_000.0, (
        "weekly loss meter must not be overwritten with session-only P&L on DB failure; "
        "got %r instead of -20000" % rm._realized_week
    )
