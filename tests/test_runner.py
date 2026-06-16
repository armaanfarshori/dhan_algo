"""
StrategyRunner — unit tests for the poll-loop wiring.

All fakes are hermetic: no DB, no network, no live feed.
Async tests are driven via asyncio.run() to match the project style.
"""
import asyncio
from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo


from engine.runner import StrategyRunner
from engine.types import Fill, OrderIntent, Position
from strategies.orb import Decision

IST = ZoneInfo("Asia/Kolkata")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def ist(hh: int, mm: int, weekday_date: str = "2026-06-16") -> datetime:
    """Return an IST datetime on a Monday (2026-06-16 is a Monday)."""
    y, mo, d = (int(p) for p in weekday_date.split("-"))
    return datetime(y, mo, d, hh, mm, tzinfo=IST)


def weekend_ist(hh: int, mm: int) -> datetime:
    """2026-06-14 is a Saturday."""
    return datetime(2026, 6, 14, hh, mm, tzinfo=IST)


def _make_fill(side: str = "BUY", qty: int = 5, price: float = 105.0) -> Fill:
    return Fill(security_id="42", side=side, qty=qty, price=price)


# ─── Minimal fakes ────────────────────────────────────────────────────────────

class FakeFeed:
    """Returns a fixed tick so the runner takes the WS branch."""

    def __init__(self, ltp: float = 105.0, high: float = 106.0, low: float = 104.0):
        self.ltp = ltp
        self.high = high
        self.low = low

    def get_ohlc_tick(self, sid: str) -> dict:
        return {
            "last_price": self.ltp,
            "ohlc": {"high": self.high, "low": self.low},
        }


class FakeGate:
    """Async gate — allows by default; set .allow = False to block."""

    def __init__(self, allow: bool = True):
        self.allow = allow
        self.calls: list = []

    async def allows(self, sid, side, segment, *, strategy="ORB") -> bool:
        self.calls.append((sid, side, segment, strategy))
        return self.allow


class FakeRisk:
    """Sync sizing methods + async get_adv; set .check_ok = False to block intent."""

    def __init__(self, qty: int = 5, check_ok: bool = True):
        self._qty = qty
        self.check_ok = check_ok
        self.registered: list = []
        self.released: list = []
        self.intents_checked: list = []

    async def get_adv(self, sid: str) -> float:
        return 1_000_000.0

    def size_position(self, *, entry: float, stop: float, adv: float) -> int:
        return self._qty

    def check_intent(self, intent: OrderIntent, price: float):
        self.intents_checked.append(intent)
        if self.check_ok:
            return True, ""
        return False, "risk-block"

    def position_risk(self, entry: float, stop: float, qty: int) -> float:
        return abs(entry - stop) * qty

    def register_risk(self, sid: str, risk: float):
        self.registered.append((sid, risk))

    def release_risk(self, sid: str):
        self.released.append(sid)


class FakeExecutor:
    """Async executor — returns a Fill by default; set .fill = None to simulate failure."""

    def __init__(self, fill: Optional[Fill] = None):
        self._fill = fill
        self.submitted: list = []

    async def submit(self, intent: OrderIntent, *, ref_price: float) -> Optional[Fill]:
        self.submitted.append((intent, ref_price))
        # Build a sensible fill if none was pre-configured
        if self._fill is not None:
            return self._fill
        return Fill(
            security_id=intent.security_id,
            side=intent.side,
            qty=intent.qty,
            price=ref_price,
        )


class FakePortfolio:
    """Tracks apply_fill calls; get() returns a Position-like object."""

    def __init__(self, position: Optional[Position] = None):
        self._pos = position or Position(security_id="42", qty=0)
        self.fills_applied: list = []
        self.realized_pnl: float = 100.0   # dummy realized value

    def get(self, sid: str) -> Position:
        return self._pos

    async def apply_fill(self, fill: Fill, *, strategy: str = "ORB") -> float:
        self.fills_applied.append((fill, strategy))
        return self.realized_pnl


class FakeStrategy:
    """
    Lightweight stand-in for ORB that returns a predetermined Decision and
    records notify_fill / notify_flat calls.  The runner only cares that it
    has: security_id, on_tick(), notify_fill(), notify_flat(), status().
    """

    def __init__(self, security_id: str = "42", decision: Optional[Decision] = None):
        self.security_id = security_id
        self._decision = decision
        self.fills_notified: list = []
        self.flats_notified: int = 0
        # Records (price, high, low) for each on_tick call so tests can verify
        # the runner threads the correct intrabar H/L from the feed.
        self.ticks_received: list = []

    def on_tick(self, now, price, high=None, low=None) -> Optional[Decision]:
        self.ticks_received.append((price, high, low))
        return self._decision

    def notify_fill(self, side: str, qty: int, price: float):
        self.fills_notified.append((side, qty, price))

    def notify_flat(self):
        self.flats_notified += 1

    def status(self) -> dict:
        return {"security_id": self.security_id}


# ─── Factory ──────────────────────────────────────────────────────────────────

def make_runner(
    decision: Optional[Decision] = None,
    gate_allow: bool = True,
    risk_qty: int = 5,
    risk_check_ok: bool = True,
    executor_fill: Optional[Fill] = None,
    portfolio: Optional[FakePortfolio] = None,
    max_entries: int = 4,
    ltp: float = 105.0,
    high: float = 106.0,
    low: float = 104.0,
):
    strategy = FakeStrategy(decision=decision)
    gate = FakeGate(allow=gate_allow)
    risk = FakeRisk(qty=risk_qty, check_ok=risk_check_ok)
    executor = FakeExecutor(fill=executor_fill)
    port = portfolio or FakePortfolio()
    feed = FakeFeed(ltp=ltp, high=high, low=low)
    runner = StrategyRunner(
        strategy,
        client=None,
        feed=feed,
        gate=gate,
        risk=risk,
        executor=executor,
        portfolio=port,
        max_entries_per_session=max_entries,
    )
    return runner, strategy, gate, risk, executor, port


# ─── 1. Entry happy path ───────────────────────────────────────────────────────

def test_entry_happy_path():
    """ENTER decision → gate allows → risk sizes → fill → all downstream callbacks fired."""
    decision = Decision(action="ENTER", side="BUY", stop=100.0, target=110.0, reason="test")
    runner, strategy, gate, risk, executor, port = make_runner(decision=decision)

    asyncio.run(runner._poll_once(ist(10, 0)))

    # gate was consulted
    assert len(gate.calls) == 1
    # executor submitted one intent
    assert len(executor.submitted) == 1
    submitted_intent, ref_price = executor.submitted[0]
    assert submitted_intent.side == "BUY"
    assert submitted_intent.qty == 5
    assert ref_price == 105.0
    # portfolio received the fill
    assert len(port.fills_applied) == 1
    # strategy was notified of the fill
    assert len(strategy.fills_notified) == 1
    assert strategy.fills_notified[0][0] == "BUY"
    # risk.register_risk was called
    assert len(risk.registered) == 1
    assert risk.registered[0][0] == "42"
    # entries counter incremented
    assert runner._entries_today == 1


# ─── 2. Gate blocks entry ─────────────────────────────────────────────────────

def test_gate_blocks_entry():
    """Gate veto → executor never called, no fill, entries counter stays 0."""
    decision = Decision(action="ENTER", side="BUY", stop=100.0, target=110.0)
    runner, strategy, gate, risk, executor, port = make_runner(
        decision=decision, gate_allow=False
    )

    asyncio.run(runner._poll_once(ist(10, 0)))

    assert len(gate.calls) == 1          # gate was asked
    assert len(executor.submitted) == 0  # but nothing was submitted
    assert len(port.fills_applied) == 0
    assert runner._entries_today == 0


# ─── 3. Risk blocks entry ─────────────────────────────────────────────────────

def test_risk_blocks_entry():
    """check_intent returns False → executor never called."""
    decision = Decision(action="ENTER", side="BUY", stop=100.0, target=110.0)
    runner, strategy, gate, risk, executor, port = make_runner(
        decision=decision, risk_check_ok=False
    )

    asyncio.run(runner._poll_once(ist(10, 0)))

    assert len(gate.calls) == 1              # gate was asked and passed
    assert len(risk.intents_checked) == 1    # risk checked the intent
    assert len(executor.submitted) == 0      # but order was not placed
    assert len(port.fills_applied) == 0
    assert runner._entries_today == 0


# ─── 4. Max entries cap ───────────────────────────────────────────────────────

def test_max_entries_blocks_additional_entry():
    """When entries_today == max_entries_per_session the runner skips the entry."""
    decision = Decision(action="ENTER", side="BUY", stop=100.0, target=110.0)
    runner, strategy, gate, risk, executor, port = make_runner(
        decision=decision, max_entries=2
    )
    runner._entries_today = 2    # already at cap
    runner._session_date = ist(10, 0).date()   # same session, no reset

    asyncio.run(runner._poll_once(ist(10, 0)))

    assert len(gate.calls) == 0          # bailed out before gate
    assert len(executor.submitted) == 0
    assert runner._entries_today == 2    # unchanged


# ─── 5. Exit happy path ───────────────────────────────────────────────────────

def test_exit_happy_path():
    """Open long position + EXIT decision → SELL order submitted, notify_flat called."""
    decision = Decision(action="EXIT", reason="target hit")
    open_pos = Position(security_id="42", qty=5, avg_price=102.0)
    port = FakePortfolio(position=open_pos)

    runner, strategy, gate, risk, executor, _ = make_runner(
        decision=decision, portfolio=port
    )

    asyncio.run(runner._poll_once(ist(10, 0)))

    # executor should have been called with a SELL of qty=5
    assert len(executor.submitted) == 1
    exit_intent, ref_price = executor.submitted[0]
    assert exit_intent.side == "SELL"
    assert exit_intent.qty == 5
    # portfolio received the fill
    assert len(port.fills_applied) == 1
    # strategy notified flat
    assert strategy.flats_notified == 1
    # risk released the slot
    assert risk.released == ["42"]
    # gate must NOT have been queried on exits
    assert len(gate.calls) == 0


def test_exit_happy_path_short():
    """Open short position → exit must issue a BUY to close."""
    decision = Decision(action="EXIT", reason="stop hit")
    open_pos = Position(security_id="42", qty=-3, avg_price=108.0)
    port = FakePortfolio(position=open_pos)

    runner, strategy, gate, risk, executor, _ = make_runner(
        decision=decision, portfolio=port
    )

    asyncio.run(runner._poll_once(ist(10, 0)))

    assert len(executor.submitted) == 1
    exit_intent, _ = executor.submitted[0]
    assert exit_intent.side == "BUY"
    assert exit_intent.qty == 3


# ─── 6. Exit when already flat (resync) ──────────────────────────────────────

def test_exit_when_already_flat():
    """Portfolio reports qty=0 → notify_flat called to resync strategy, no order placed."""
    decision = Decision(action="EXIT", reason="stale resync")
    flat_pos = Position(security_id="42", qty=0)
    port = FakePortfolio(position=flat_pos)

    runner, strategy, gate, risk, executor, _ = make_runner(
        decision=decision, portfolio=port
    )

    asyncio.run(runner._poll_once(ist(10, 0)))

    assert len(executor.submitted) == 0      # no order
    assert strategy.flats_notified == 1      # strategy resynced
    assert len(port.fills_applied) == 0


# ─── 7. _in_window tests ──────────────────────────────────────────────────────

def test_in_window_inside():
    """10:30 on a Monday is inside the trading window."""
    assert StrategyRunner._in_window(ist(10, 30)) is True


def test_in_window_exactly_open():
    """09:00 is the first moment of the window."""
    assert StrategyRunner._in_window(ist(9, 0)) is True


def test_in_window_exactly_close():
    """15:35 is the last moment of the window."""
    assert StrategyRunner._in_window(ist(15, 35)) is True


def test_in_window_before_open():
    """08:59 is before the window."""
    assert StrategyRunner._in_window(ist(8, 59)) is False


def test_in_window_after_close():
    """15:36 is after the window."""
    assert StrategyRunner._in_window(ist(15, 36)) is False


def test_in_window_weekend():
    """Saturday is never inside the window."""
    assert StrategyRunner._in_window(weekend_ist(10, 30)) is False


# ─── 8. Zero price → poll_once returns early ─────────────────────────────────

def test_zero_price_skips_strategy():
    """Feed returns ltp=0 → strategy.on_tick is never reached, no decision processed."""
    decision = Decision(action="ENTER", side="BUY", stop=90.0, target=120.0)
    runner, strategy, gate, risk, executor, port = make_runner(
        decision=decision, ltp=0.0
    )

    asyncio.run(runner._poll_once(ist(10, 0)))

    assert len(executor.submitted) == 0
    assert runner._entries_today == 0


# ─── 9. Session date resets entries counter ───────────────────────────────────

def test_new_session_resets_entries():
    """On a new calendar date the entries_today counter resets to 0."""
    decision = Decision(action="ENTER", side="BUY", stop=100.0, target=110.0)
    runner, *_ = make_runner(decision=decision)

    # Simulate a previous session with maxed-out entries
    runner._entries_today = 4
    runner._session_date = date(2026, 6, 15)   # yesterday

    # Today's poll — session date mismatch should reset counter before entry check
    asyncio.run(runner._poll_once(ist(10, 0)))   # 2026-06-16

    # Counter was reset to 0 then incremented to 1 on successful entry
    assert runner._entries_today == 1
    assert runner._session_date == date(2026, 6, 16)


# ─── 10. status() reflects runner state ──────────────────────────────────────

def test_status_includes_entries_today():
    runner, *_ = make_runner()
    runner._entries_today = 3
    runner.last_price = 107.5
    s = runner.status()
    assert s["entries_today"] == 3
    assert s["last_price"] == 107.5
    assert "security_id" in s    # merged from strategy.status()


# ─── 11. DATA-04: runner threads feed H/L into strategy ──────────────────────

def test_runner_passes_intrabar_high_low_to_strategy():
    """The runner must forward the intrabar high and low from the feed to
    strategy.on_tick().  This is the DATA-04 behavioral contract: whatever
    aggregator the feed exposes, the runner threads it to the strategy intact.
    With a BarBuilder-backed feed the high/low are now single-source-of-truth.
    """
    runner, strategy, *_ = make_runner(ltp=105.0, high=108.5, low=103.2)

    asyncio.run(runner._poll_once(ist(10, 0)))

    assert len(strategy.ticks_received) == 1
    price, high, low = strategy.ticks_received[0]
    assert price == 105.0
    assert high  == 108.5
    assert low   == 103.2


def test_runner_uses_ltp_as_fallback_when_ohlc_missing():
    """FakeFeed returns ltp=90 but ohlc keys absent → runner must fall back to
    ltp for both high and low (the guard in _get_price: ``or ltp``)."""

    class FeedNoOHLC:
        def get_ohlc_tick(self, sid: str) -> dict:
            return {"last_price": 90.0, "ohlc": {}}   # empty ohlc

    runner, strategy, *_ = make_runner()
    runner._feed = FeedNoOHLC()

    asyncio.run(runner._poll_once(ist(10, 0)))

    assert len(strategy.ticks_received) == 1
    price, high, low = strategy.ticks_received[0]
    assert price == 90.0
    assert high  == 90.0   # fell back to ltp
    assert low   == 90.0   # fell back to ltp
