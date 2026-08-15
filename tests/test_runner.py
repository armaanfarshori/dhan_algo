"""
StrategyRunner — unit tests for the poll-loop wiring.

All fakes are hermetic: no DB, no network, no live feed.
Async tests are driven via asyncio.run() to match the project style.
"""
import asyncio
from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo

from engine.runner import StrategyRunner, _FEED_FRESH_S
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
        # Reflect the fill in the position (real Portfolio does this) so the runner's
        # post-fill `get().qty` check sees the true residual — e.g. a partial exit.
        signed = fill.qty if fill.side == "BUY" else -fill.qty
        self._pos = Position(security_id=self._pos.security_id,
                             qty=self._pos.qty + signed,
                             avg_price=self._pos.avg_price)
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


def test_exit_partial_fill_keeps_slot():
    """A PARTIAL exit fill leaves a residual position → keep the risk slot booked
    and do NOT notify flat (retry the remainder next poll). Releasing here would
    leave a live position with zero booked risk + a strategy that thinks it's flat."""
    decision = Decision(action="EXIT", reason="target hit")
    open_pos = Position(security_id="42", qty=5, avg_price=102.0)
    port = FakePortfolio(position=open_pos)

    runner, strategy, gate, risk, executor, _ = make_runner(
        decision=decision, portfolio=port
    )
    # Executor fills only 3 of the 5 requested — a partial exit
    executor._fill = Fill(security_id="42", side="SELL", qty=3, price=101.0)

    asyncio.run(runner._poll_once(ist(10, 0)))

    assert port.get("42").qty == 2          # 3 of 5 sold → 2 remain
    assert risk.released == []              # slot NOT released
    assert strategy.flats_notified == 0     # strategy NOT told it's flat


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


class _NoneExecutor:
    """Executor that always returns None (failed/no fill)."""

    def __init__(self):
        self.submitted: list = []

    async def submit(self, intent: OrderIntent, *, ref_price: float):
        self.submitted.append((intent, ref_price))
        return None


class _FlippingPortfolio:
    """get() returns an open position first, then flat (broker closed it
    out-of-band between the exit decision and the None-fill check)."""

    def __init__(self, open_pos: Position):
        self._states = [open_pos, Position(security_id=open_pos.security_id, qty=0)]
        self.fills_applied: list = []

    def get(self, sid: str) -> Position:
        # First call returns open; every subsequent call returns flat.
        return self._states[0] if len(self._states) == 1 else self._states.pop(0)

    async def apply_fill(self, fill, *, strategy: str = "ORB") -> float:
        self.fills_applied.append((fill, strategy))
        return 0.0


def test_exit_releases_risk_on_none_fill_when_broker_flat():
    """None fill + broker already flat → release the leaked risk slot + resync,
    do NOT keep the committed-risk slot (which would block new entries)."""
    decision = Decision(action="EXIT", reason="target hit")
    open_pos = Position(security_id="42", qty=5, avg_price=102.0)
    port = _FlippingPortfolio(open_pos)

    strategy = FakeStrategy(decision=decision)
    risk = FakeRisk()
    runner = StrategyRunner(
        strategy, client=None, feed=FakeFeed(ltp=105.0, high=106.0, low=104.0),
        gate=FakeGate(), risk=risk, executor=_NoneExecutor(),
        portfolio=port, max_entries_per_session=4)

    asyncio.run(runner._poll_once(ist(10, 0)))

    assert risk.released == ["42"]          # slot freed
    assert strategy.flats_notified == 1     # strategy resynced
    assert len(port.fills_applied) == 0     # no fill applied


def test_exit_keeps_risk_on_none_fill_when_still_open():
    """None fill + position STILL open → keep the slot, retry next poll.
    Releasing here would falsely free risk on a position that remains live."""
    decision = Decision(action="EXIT", reason="target hit")
    open_pos = Position(security_id="42", qty=5, avg_price=102.0)
    port = FakePortfolio(position=open_pos)   # get() always returns open

    strategy = FakeStrategy(decision=decision)
    risk = FakeRisk()
    runner = StrategyRunner(
        strategy, client=None, feed=FakeFeed(ltp=105.0, high=106.0, low=104.0),
        gate=FakeGate(), risk=risk, executor=_NoneExecutor(),
        portfolio=port, max_entries_per_session=4)

    asyncio.run(runner._poll_once(ist(10, 0)))

    assert risk.released == []              # slot NOT freed
    assert strategy.flats_notified == 0     # not resynced
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
    """15:41 is after the (post-CAS) poll window."""
    assert StrategyRunner._in_window(ist(15, 41)) is False
    assert StrategyRunner._in_window(ist(15, 40)) is True


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


# ─── 12. QR-C3: stale tick cache → REST fallback ─────────────────────────────

class FakeFeedWithAge:
    """LiveFeed stand-in that exposes get_tick_age_s() so the runner's
    freshness guard is exercised.  age_s=None simulates a cold/cleared cache
    (post-reconnect invalidation); age_s > _FEED_FRESH_S simulates a stale
    cached price that survived a reconnect.
    """

    def __init__(self, ltp: float = 105.0, high: float = 106.0,
                 low: float = 104.0, age_s: Optional[float] = None):
        self.ltp   = ltp
        self.high  = high
        self.low   = low
        self._age  = age_s
        self.ohlc_tick_calls: int = 0

    def get_ohlc_tick(self, sid: str) -> dict:
        self.ohlc_tick_calls += 1
        return {
            "last_price": self.ltp,
            "ohlc": {"high": self.high, "low": self.low},
        }

    def get_tick_age_s(self, sid: str) -> Optional[float]:
        return self._age


class FakeClient:
    """Async REST client stub used to verify that the runner falls back to REST."""

    def __init__(self, ltp: float = 200.0):
        self._ltp = ltp
        self.calls: list = []

    async def get_ohlc(self, instruments: dict) -> dict:
        self.calls.append(instruments)
        # Mirror the shape the runner expects: data → segment → sid → tick
        return {
            "data": {
                "NSE_EQ": {
                    "42": {
                        "last_price": self._ltp,
                        "ohlc": {"high": self._ltp + 1, "low": self._ltp - 1},
                    }
                }
            }
        }


def test_runner_falls_back_to_rest_when_tick_cache_cold():
    """QR-C3 — after a reconnect the tick cache is cleared (age=None).

    The runner must skip the WS price entirely and call REST instead.
    This asserts the fix: _get_price() checks get_tick_age_s() before trusting
    the cache.  A None age (cold cache) must be treated as stale → REST path.
    """
    # Feed has a cached ltp=105 but age=None (cleared on reconnect)
    stale_feed = FakeFeedWithAge(ltp=105.0, age_s=None)
    rest_client = FakeClient(ltp=200.0)

    runner, strategy, *_ = make_runner()
    runner._feed    = stale_feed
    runner._client  = rest_client

    asyncio.run(runner._poll_once(ist(10, 0)))

    # REST was called — the runner did NOT trust the cold cache
    assert len(rest_client.calls) == 1, (
        "REST must be called when tick cache is cold (post-reconnect)"
    )
    # The price the strategy received is the REST price, not the cached 105
    assert len(strategy.ticks_received) == 1
    price, _, _ = strategy.ticks_received[0]
    assert price == 200.0, (
        f"strategy must see REST price (200.0), not stale cached price — got {price}"
    )
    # The feed's get_ohlc_tick was never called (skipped entirely)
    assert stale_feed.ohlc_tick_calls == 0, (
        "feed.get_ohlc_tick() must not be called when cache is cold"
    )


def test_runner_falls_back_to_rest_when_tick_is_stale():
    """QR-C3 — tick older than _FEED_FRESH_S must be rejected and REST used.

    Simulates a cached price that survived a WebSocket reconnect and is now
    older than the freshness window.  The runner must not hand this to ORB.
    """
    stale_age = _FEED_FRESH_S + 1   # one second past the window
    stale_feed = FakeFeedWithAge(ltp=105.0, age_s=stale_age)
    rest_client = FakeClient(ltp=200.0)

    runner, strategy, *_ = make_runner()
    runner._feed    = stale_feed
    runner._client  = rest_client

    asyncio.run(runner._poll_once(ist(10, 0)))

    assert len(rest_client.calls) == 1, "REST must be called when tick is stale"
    price, _, _ = strategy.ticks_received[0]
    assert price == 200.0, (
        f"strategy must see REST price (200.0) not stale WS price — got {price}"
    )
    assert stale_feed.ohlc_tick_calls == 0, (
        "feed.get_ohlc_tick() must not be called when tick is stale"
    )


def test_runner_uses_ws_tick_when_fresh():
    """QR-C3 control case — a fresh tick (age < _FEED_FRESH_S) must still take
    the WS path and never hit REST.
    """
    fresh_age = _FEED_FRESH_S - 1   # one second inside the freshness window
    fresh_feed = FakeFeedWithAge(ltp=105.0, age_s=fresh_age)
    rest_client = FakeClient(ltp=200.0)

    runner, strategy, *_ = make_runner()
    runner._feed    = fresh_feed
    runner._client  = rest_client

    asyncio.run(runner._poll_once(ist(10, 0)))

    assert len(rest_client.calls) == 0, "REST must NOT be called when WS tick is fresh"
    assert fresh_feed.ohlc_tick_calls == 1, "feed.get_ohlc_tick() must be called for fresh tick"
    price, _, _ = strategy.ticks_received[0]
    assert price == 105.0, f"strategy must see WS price (105.0) — got {price}"


# ─── 13. Batched OHLC fallback ────────────────────────────────────────────────

class _CountingBatchedOhlc:
    """Stand-in for core.batched_ohlc.BatchedOhlc — records get() calls."""

    def __init__(self, ltp: float = 200.0):
        self._ltp = ltp
        self.calls: list = []

    async def get(self, sid: str):
        self.calls.append(sid)
        return {"last_price": self._ltp,
                "ohlc": {"high": self._ltp + 1, "low": self._ltp - 1}}


def test_runner_uses_batched_ohlc_on_stale_tick():
    """When the WS tick is stale and a batched fetcher is wired, the runner must
    use it instead of the per-sid client.get_ohlc (the 429-burst fix)."""
    stale_feed = FakeFeedWithAge(ltp=105.0, age_s=_FEED_FRESH_S + 5)
    per_sid_client = FakeClient(ltp=999.0)   # must NOT be used
    batched = _CountingBatchedOhlc(ltp=200.0)

    runner, strategy, *_ = make_runner()
    runner._feed = stale_feed
    runner._client = per_sid_client
    runner._batched_ohlc = batched

    asyncio.run(runner._poll_once(ist(10, 0)))

    assert batched.calls == ["42"], "stale lookup must go through the batched fetcher"
    assert len(per_sid_client.calls) == 0, "per-sid get_ohlc must NOT be called"
    price, _, _ = strategy.ticks_received[0]
    assert price == 200.0


# ─── 14. avg_price fallback when never ticked ────────────────────────────────

def test_poll_falls_back_to_avg_price_when_never_ticked():
    """A held position with no fresh price, no prior last_price, but a known
    entry avg_price must still run exit logic priced off avg_price (so EOD /
    stop exits fire). ORB never enters while holding, so a stale price is safe.
    """
    decision = Decision(action="EXIT", reason="EOD square-off")
    open_pos = Position(security_id="42", qty=5, avg_price=102.0)
    port = FakePortfolio(position=open_pos)

    # ltp=0 → _get_price returns 0; runner.last_price stays 0 (never ticked).
    runner, strategy, gate, risk, executor, _ = make_runner(
        decision=decision, portfolio=port, ltp=0.0)
    assert runner.last_price == 0.0

    asyncio.run(runner._poll_once(ist(15, 25)))

    # Exit fired, priced off avg_price.
    assert len(executor.submitted) == 1
    exit_intent, ref_price = executor.submitted[0]
    assert exit_intent.side == "SELL"
    assert ref_price == 102.0


def test_poll_no_price_no_avg_returns_early():
    """No price AND no avg_price (qty!=0 but avg_price 0) → poll returns without
    submitting (avoids ref_price<=0 which the executor rejects)."""
    decision = Decision(action="EXIT", reason="EOD square-off")
    open_pos = Position(security_id="42", qty=5, avg_price=0.0)
    port = FakePortfolio(position=open_pos)

    runner, strategy, gate, risk, executor, _ = make_runner(
        decision=decision, portfolio=port, ltp=0.0)

    asyncio.run(runner._poll_once(ist(15, 25)))

    assert len(executor.submitted) == 0
