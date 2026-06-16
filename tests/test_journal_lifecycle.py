"""QA findings C2, H3 — trade-record lifecycle integrity. BOTH FIXED 2026-06-15.
DATA-03 (2026-06-16): exact open_trade_id threading — exit closes the specific
               row whose id was returned by log_trade_entry, not an ORDER BY guess.

C2 (Critical, fixed): log_trade_exit now closes exactly ONE (latest matching)
               open row. Previously a null-order (paper) exit closed EVERY open
               row for the security with the same P&L, double-counting into
               daily_pnl and the risk halt.
H3 (High, fixed): Portfolio.apply_fill now journals a flip as exit + a new
               entry for the opposite position (was: exit only → untracked).
DATA-03: log_trade_entry returns the new row id; Portfolio threads it to
               log_trade_exit so the exit is always exact even with two
               concurrent open rows for the same security.

C2 uses a real (SQLite) AsyncDBBackend to exercise the actual UPDATE SQL.
H3 / DATA-03 use a fake recorder backend (no DB) to assert the call pattern.

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


# ── DATA-03: exact trade_id — two concurrent opens close the correct row ─────────

def test_exact_trade_id_closes_correct_row(tmp_path):
    """DATA-03: when two OPEN rows exist for the same security, log_trade_exit
    with trade_id=<first_id> closes ONLY that row; the second stays OPEN."""
    be, eng = _sqlite_backend(tmp_path)

    async def go():
        id_a = await be.log_trade_entry("222", "BUY", 10, 100.0, "ORB")
        id_b = await be.log_trade_entry("222", "BUY", 5,  105.0, "ORB")   # second open row
        # Close the FIRST row by exact id — id_b must remain OPEN.
        await be.log_trade_exit("222", exit_price=110.0, pnl=100.0, trade_id=id_a)
        return id_a, id_b
    id_a, id_b = asyncio.run(go())

    with eng.connect() as c:
        closed_ids = [r[0] for r in c.execute(
            text("SELECT id FROM trades WHERE status='CLOSED'"))]
        open_ids = [r[0] for r in c.execute(
            text("SELECT id FROM trades WHERE status='OPEN'"))]

    assert closed_ids == [id_a], f"Expected only row {id_a} closed, got {closed_ids}"
    assert open_ids == [id_b],   f"Expected only row {id_b} open, got {open_ids}"


def test_portfolio_threads_trade_id_to_exit(tmp_path, monkeypatch):
    """DATA-03 integration: Portfolio stores the id returned by log_trade_entry
    and passes it to log_trade_exit, so two securities' rows are never confused."""
    be, eng = _sqlite_backend(tmp_path)

    pf = Portfolio(mode="PAPER", db_backend=be)
    async def _no_persist(pos): return None
    monkeypatch.setattr(pf, "_persist", _no_persist)

    async def go():
        # Open two different securities so each gets its own trade row.
        await pf.apply_fill(Fill("AAA", "BUY", 10, 100.0), strategy="ORB")
        await pf.apply_fill(Fill("BBB", "BUY", 5,  200.0), strategy="ORB")
        # Close AAA — must close AAA's row, leave BBB open.
        await pf.apply_fill(Fill("AAA", "SELL", 10, 110.0), strategy="ORB")
    asyncio.run(go())

    with eng.connect() as c:
        closed = [r[0] for r in c.execute(
            text("SELECT security_id FROM trades WHERE status='CLOSED'"))]
        opened = [r[0] for r in c.execute(
            text("SELECT security_id FROM trades WHERE status='OPEN'"))]

    assert closed == ["AAA"], f"Only AAA should be closed, got {closed}"
    assert opened == ["BBB"], f"Only BBB should be open, got {opened}"


# ── H3: flip must record the new entry ──────────────────────────────────────────

class _Recorder:
    """Fake AsyncDBBackend that records calls and returns synthetic trade ids."""
    def __init__(self):
        self.entries, self.exits = [], []
        self._next_id = 1

    async def log_trade_entry(self, **kw):
        self.entries.append(kw)
        tid = self._next_id
        self._next_id += 1
        return tid

    async def log_trade_exit(self, **kw):
        self.exits.append(kw)


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


def test_exit_passes_trade_id_from_entry(monkeypatch):
    """DATA-03: the trade_id returned by log_trade_entry must be forwarded to
    log_trade_exit so the exit closes the exact DB row, not the heuristic match."""
    pf, rec = _portfolio_with_recorder(monkeypatch)
    asyncio.run(pf.apply_fill(Fill("111", "BUY", 10, 100.0), strategy="ORB"))
    asyncio.run(pf.apply_fill(Fill("111", "SELL", 10, 110.0), strategy="ORB"))
    # The entry returned id=1 (first call); exit must carry that id.
    assert rec.exits[0]["trade_id"] == 1


def test_flip_exit_carries_entry_id_new_entry_stored(monkeypatch):
    """DATA-03 + H3: on a flip, the exit uses the OLD entry's trade_id, and the
    NEW entry's returned id is stored for the subsequent exit."""
    pf, rec = _portfolio_with_recorder(monkeypatch)
    asyncio.run(pf.apply_fill(Fill("111", "BUY", 10, 100.0), strategy="ORB"))  # entry id=1
    asyncio.run(pf.apply_fill(Fill("111", "SELL", 15, 110.0), strategy="ORB")) # exit id=1, new entry id=2
    # Exit must reference the ORIGINAL long entry (id=1).
    assert rec.exits[0]["trade_id"] == 1
    # The flip's new short entry (id=2) must be stored for the next exit.
    assert pf._open_trade_id["111"] == 2
