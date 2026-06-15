"""QA findings C2, H3 — trade-record lifecycle integrity. BOTH FIXED 2026-06-15.

C2 (Critical, fixed): log_trade_exit now closes exactly ONE (latest matching)
               open row. Previously a null-order (paper) exit closed EVERY open
               row for the security with the same P&L, double-counting into
               daily_pnl and the risk halt.
H3 (High, fixed): Portfolio.apply_fill now journals a flip as exit + a new
               entry for the opposite position (was: exit only → untracked).

C2 uses a real (SQLite) AsyncDBBackend to exercise the actual UPDATE SQL.
H3 uses a fake recorder backend (no DB) to assert the call pattern.

Residual (tracked, not yet fixed): a partial reduce still marks a trade CLOSED
even though qty remains; multi-entry positions are not P&L-apportioned. ORB
opens once and closes once, so neither occurs in the current strategy.
"""
import asyncio

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.journal import AsyncDBBackend
from engine.portfolio import Portfolio
from engine.types import Fill


# ── C2: real SQL against SQLite ─────────────────────────────────────────────────

def _sqlite_backend(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}",
                        connect_args={"check_same_thread": False})
    with eng.begin() as c:
        c.execute(text("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INT, security_id TEXT, side TEXT, qty INT,
                entry_ts TEXT, entry_price REAL, exit_ts TEXT, exit_price REAL,
                pnl REAL, strategy TEXT, dhan_order_id TEXT, status TEXT)"""))
        c.execute(text("""
            CREATE TABLE daily_pnl (
                date TEXT PRIMARY KEY, realized_pnl REAL, trades_count INT,
                wins INT, losses INT)"""))
    be = AsyncDBBackend()
    be._enabled, be._engine, be._Session = True, eng, sessionmaker(bind=eng)
    return be, eng


def _open_count(eng, sid):
    with eng.connect() as c:
        return c.execute(text("SELECT COUNT(*) FROM trades WHERE security_id=:s "
                              "AND status='OPEN'"), {"s": sid}).scalar()


def test_paper_exit_closes_one_open_row_no_double_count(tmp_path):
    """C2 fixed: with two OPEN rows (multi-entry) and a paper exit (order_id=None),
    exactly ONE row closes with the P&L — no duplication into daily_pnl/risk."""
    be, eng = _sqlite_backend(tmp_path)

    async def go():
        await be.log_trade_entry("111", "BUY", 10, 100.0, "ORB")
        await be.log_trade_entry("111", "BUY", 10, 102.0, "ORB")   # add-on
        await be.log_trade_exit("111", exit_price=110.0, pnl=160.0, dhan_order_id=None)
    asyncio.run(go())

    with eng.connect() as c:
        closed = c.execute(text("SELECT COUNT(*) FROM trades WHERE status='CLOSED'")).scalar()
        pnls = [r[0] for r in c.execute(text("SELECT pnl FROM trades WHERE status='CLOSED'"))]
    assert closed == 1                 # exactly one row closed — no blanket close
    assert pnls == [160.0]             # P&L recorded once, not duplicated
    assert _open_count(eng, "111") == 1  # sibling row remains OPEN (not nuked)


def test_live_exit_by_order_id_leaves_sibling_open(tmp_path):
    """Characterization of C2 (live): an order-id-scoped exit closes only its own
    row, so a second entry row for the same security stays OPEN indefinitely."""
    be, eng = _sqlite_backend(tmp_path)

    async def go():
        await be.log_trade_entry("111", "BUY", 10, 100.0, "ORB", dhan_order_id="A")
        await be.log_trade_entry("111", "BUY", 10, 102.0, "ORB", dhan_order_id="B")
        await be.log_trade_exit("111", exit_price=110.0, pnl=80.0, dhan_order_id="A")
    asyncio.run(go())
    assert _open_count(eng, "111") == 1   # row B orphaned OPEN


# ── H3: flip must record the new entry ──────────────────────────────────────────

class _Recorder:
    def __init__(self):
        self.entries, self.exits = [], []
    async def log_trade_entry(self, **kw): self.entries.append(kw)
    async def log_trade_exit(self, **kw): self.exits.append(kw)


def _portfolio_with_recorder(monkeypatch):
    rec = _Recorder()
    pf = Portfolio(mode="PAPER", db_backend=rec)
    async def _no_persist(pos): return None
    monkeypatch.setattr(pf, "_persist", _no_persist)
    return pf, rec


def test_reduce_close_records_one_entry_one_exit(monkeypatch):
    """Regression: a clean open→close records exactly one entry and one exit."""
    pf, rec = _portfolio_with_recorder(monkeypatch)
    asyncio.run(pf.apply_fill(Fill("111", "BUY", 10, 100.0), strategy="ORB"))
    asyncio.run(pf.apply_fill(Fill("111", "SELL", 10, 105.0), strategy="ORB"))
    assert len(rec.entries) == 1 and len(rec.exits) == 1
    assert pf.get("111").is_flat


def test_flip_records_new_entry(monkeypatch):  # H3 fixed
    pf, rec = _portfolio_with_recorder(monkeypatch)
    asyncio.run(pf.apply_fill(Fill("111", "BUY", 10, 100.0), strategy="ORB"))   # long 10
    asyncio.run(pf.apply_fill(Fill("111", "SELL", 15, 105.0), strategy="ORB"))  # flip to short 5
    assert pf.get("111").qty == -5
    # The new short -5 is a fresh position and must be journaled as an entry.
    assert len(rec.entries) == 2
