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
