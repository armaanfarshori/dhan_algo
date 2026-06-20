"""
Scalper INTEGRATION tests — the glue that wires the merged scalper pieces into the
live trader (PAPER, default OFF).

Covers the facet's safety-critical contracts:
  • The ScalperOrchestrator constructs + runs against the REAL adapters
    (ScalperSignalProvider + ScalperOptionResolver) with a PaperExecutor and a
    fake feed / fake option-chain client — NO live data, NO DB, NO network.
  • scalper_enabled=False (the config default) → the trader builds no orchestrator,
    so flatten_all with no orchestrators is byte-identical to the ORB path.
  • flatten_all covers an OPTION position (the kill-switch reaches an option leg
    held by the orchestrator, flattened on its own NSE_FNO segment).
  • The heartbeat "scalper" key is populated from orch.status().
  • PaperExecutor honours a per-order option-realistic slippage override.

TZ-safe (ci-ist-date-trap): every timestamp is an aware-IST datetime and the
orchestrator derives the session date from the tick's `now`, never date.today().
The whole module is exercised under TZ=UTC in CI.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from config import Config
from engine.execution import PaperExecutor
from engine.portfolio import Portfolio
from engine.risk import RiskEngine, RiskParams
from engine.scalper_adapters import (
    ScalperOptionResolver,
    ScalperSignalProvider,
)
from engine.scalper_orchestrator import (
    OrchestratorParams,
    ScalperOrchestrator,
)
from engine.types import OrderIntent, Position

IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Fakes (no live data)
# ---------------------------------------------------------------------------


class FakeChainClient:
    """Async stand-in for core.client.DhanClient option-chain calls."""

    def __init__(self, spot: float = 22000.0):
        self.spot = spot
        self.expiry_calls = 0
        self.chain_calls = 0

    async def get_fno_expiry_list(self, scrip, seg="IDX_I"):
        self.expiry_calls += 1
        # Two upcoming expiries; the resolver picks the nearest (front weekly).
        return {"data": ["2026-06-25", "2026-07-02"]}

    async def get_fno_option_chain(self, scrip, expiry, seg="IDX_I"):
        self.chain_calls += 1
        oc = {}
        # ATM (spot 22000, step 50) ± a few strikes, all liquid CE+PE.
        for k in (21900, 21950, 22000, 22050, 22100):
            node = {
                "last_price": 120.0,
                "oi": 500_000,
                "volume": 50_000,
                "top_bid_price": 119.5,
                "top_ask_price": 120.5,
                "top_bid_quantity": 65 * 20,
                "top_ask_quantity": 65 * 20,
                "implied_volatility": 14.0,
                "security_id": f"OPT_{k}_CE",
                "greeks": {"delta": 0.5},
            }
            pe = dict(node, security_id=f"OPT_{k}_PE")
            oc[str(k)] = {"ce": node, "pe": pe}
        return {"data": {"last_price": self.spot, "oc": oc}}


class FakeFeed:
    """Minimal spot/premium source keyed by security_id (strings)."""

    def __init__(self, prices: dict):
        self._prices = {str(k): v for k, v in prices.items()}

    def get_ltp(self, sid):
        return self._prices.get(str(sid), 0.0)

    def set(self, sid, price):
        self._prices[str(sid)] = price


def _real_adapters(feed: FakeFeed, client: FakeChainClient,
                   underlyings=("NIFTY",)):
    """Build the REAL signal provider + resolver over the fakes."""
    underlyings = list(underlyings)
    # Mirror production wiring: the resolver is built FIRST so the held-leg
    # premium lookup reads its REST chain cache (not the WS feed, which cannot
    # stream a contract subscribed mid-session). NIFTY underlying_id is "13".
    resolver = ScalperOptionResolver(client=client, underlyings=underlyings)
    # Tests must not actually sleep the 3 s inter-call throttle.
    resolver._min_refresh_s = 0.0
    signals = ScalperSignalProvider(
        underlyings=underlyings,
        spot_lookup=lambda u: feed.get_ltp("13") if u == "NIFTY" else 0.0,
        premium_lookup=resolver.premium_for,
        simple=True)
    resolver._signal_provider = signals
    return signals, resolver


def _wire(feed, client, underlyings=("NIFTY",)):
    signals, resolver = _real_adapters(feed, client, underlyings)
    portfolio = Portfolio(mode="PAPER", db_backend=None)

    def _ltp(sid):
        return feed.get_ltp(sid)

    risk = RiskEngine(
        RiskParams(equity_base=250_000.0, max_open_positions=10,
                   max_daily_loss_pct=1.0, max_notional_pct=1.0,
                   max_gross_exposure_pct=10.0),
        portfolio=portfolio, ltp_lookup=_ltp, db_backend=None)
    executor = PaperExecutor(db_backend=None, slippage_bps=0.0)
    orch = ScalperOrchestrator(
        signals=signals, resolver=resolver, portfolio=portfolio, risk=risk,
        executor=executor,
        params=OrchestratorParams(underlyings=list(underlyings), warmup_min=15))
    return orch, signals, resolver, portfolio, risk, executor


def _now(hour=11, minute=0):
    return datetime(2026, 6, 25, hour, minute, tzinfo=IST)


# ---------------------------------------------------------------------------
# 1. Orchestrator constructs + runs against the REAL adapters
# ---------------------------------------------------------------------------


def _disable_skew_guard(monkeypatch):
    """The pure OptionsScalper rejects ticks stamped >2 min ahead of the wall
    clock. Our deterministic fixed-date `now` is intentionally far from the CI
    wall clock, so widen the guard for the duration of the test."""
    from datetime import timedelta

    import strategies.options_scalper as sc
    monkeypatch.setattr(sc, "MAX_FUTURE_SKEW", timedelta(days=3650))


def test_orchestrator_runs_against_real_adapters_end_to_end(monkeypatch):
    _disable_skew_guard(monkeypatch)
    feed = FakeFeed({"13": 22000.0})       # NIFTY spot
    client = FakeChainClient(spot=22000.0)
    orch, signals, resolver, portfolio, risk, _ = _wire(feed, client)

    async def scenario():
        # Warm the resolver chain cache (the trader's refresh task does this).
        await resolver.refresh(_now())
        now = _now()
        # Drive the per-index OptionsScalper to a LONG ENTER via the test seam.
        # Pre-seed the session so the first on_tick does NOT reset our seams.
        scalper = signals._scalpers["NIFTY"]
        scalper._session_date = now.date()
        scalper._bypass_time_guards = True
        scalper._bars_seen = 25
        scalper._signal_override = "LONG"
        for _ in range(25):
            await orch.tick(now)
        return portfolio.open_positions()

    opened = asyncio.run(scenario())
    # An option leg was entered + applied to the portfolio (long-only BUY).
    assert len(opened) == 1
    pos = opened[0]
    assert pos.qty > 0                       # long option
    assert pos.security_id.startswith("OPT_")
    assert pos.strategy == "options_scalper"
    # The orchestrator now manages exactly one NIFTY leg.
    assert orch._total_open() == 1
    assert client.chain_calls >= 1           # chain came from the (fake) client only


def test_resolver_returns_none_before_chain_warm():
    """resolve() does NO network and returns None until the cache is warm —
    the orchestrator tick never blocks on IO."""
    feed = FakeFeed({"13": 22000.0})
    client = FakeChainClient()
    _, _, resolver, _, _, _ = _wire(feed, client)
    from engine.scalper_orchestrator import ScalpSignal
    sig = ScalpSignal(underlying="NIFTY", action="ENTER", side="BUY",
                      option_type="CE", strike=22000, lots=1)
    assert resolver.resolve(sig) is None     # cache empty → no contract, no fetch
    assert client.chain_calls == 0


def test_resolver_throttles_chain_fetch_within_window():
    """Two refreshes inside the throttle window do not double-fetch (rate-limit).
    Uses a tiny throttle so the test never sleeps the real 3 s."""
    feed = FakeFeed({"13": 22000.0})
    client = FakeChainClient()
    _, _, resolver, _, _, _ = _wire(feed, client)
    resolver._min_refresh_s = 1.0            # tiny window (not the real 3 s)

    async def scenario():
        await resolver.refresh(_now())
        first = client.chain_calls
        await resolver.refresh(_now())       # immediate re-call → throttled
        return first, client.chain_calls

    first, second = asyncio.run(scenario())
    assert first == 1
    assert second == 1                       # no second fetch within the window


def test_resolver_caches_expiry_per_day():
    """The front-weekly expirylist is fetched once per day, not every refresh
    (halves API spend + avoids a 2nd back-to-back F&O-endpoint call)."""
    feed = FakeFeed({"13": 22000.0})
    client = FakeChainClient()
    _, _, resolver, _, _, _ = _wire(feed, client)

    async def scenario():
        await resolver.refresh(_now())
        resolver._cache["NIFTY"].fetched_at = 0.0   # allow a 2nd chain fetch
        await resolver.refresh(_now())              # same day → expiry cached
        return client.expiry_calls, client.chain_calls

    expiry_calls, chain_calls = asyncio.run(scenario())
    assert expiry_calls == 1                  # expirylist hit once for the day
    assert chain_calls == 2                   # chain refreshed both times


# ---------------------------------------------------------------------------
# 2. scalper_enabled defaults OFF — no behaviour change
# ---------------------------------------------------------------------------


def test_scalper_disabled_by_default():
    cfg = Config(_env_file=None)
    assert cfg.scalper_enabled is False
    # The underlyings parse cleanly even though the sleeve ships dark.
    assert cfg.scalper_underlyings_list == ["NIFTY", "BANKNIFTY"]


def test_scalper_underlyings_list_dedupes_and_uppercases():
    cfg = Config(_env_file=None, scalper_underlyings="nifty, BANKNIFTY ,nifty")
    assert cfg.scalper_underlyings_list == ["NIFTY", "BANKNIFTY"]


def test_flatten_all_orb_identical_with_no_orchestrators():
    """With orchestrators=None (scalper OFF), flatten_all behaves exactly as the
    ORB-only path: one equity SELL on NSE_EQ, risk released."""
    from apps.trader import flatten_all
    from strategies.orb import ORB

    class _Port:
        def __init__(self):
            self._p = {"111": Position("111", qty=10, avg_price=100.0)}
            self.applied = []

        def get(self, sid):
            return self._p.get(sid, Position(sid, 0))

        def open_positions(self):
            return [p for p in self._p.values() if p.qty != 0]

        async def apply_fill(self, fill, *, strategy="ORB"):
            self.applied.append((fill, strategy))
            return 0.0

    class _Exec:
        def __init__(self):
            self.sent = []

        async def submit(self, intent, *, ref_price):
            self.sent.append(intent)
            from engine.types import Fill
            return Fill(security_id=intent.security_id, side=intent.side,
                        qty=intent.qty, price=ref_price)

    class _Runner:
        def __init__(self, sid, strat):
            self.sid = sid
            self.strategy = strat
            self.last_price = 95.0

    port = _Port()
    risk = RiskEngine(RiskParams(equity_base=500_000), port, ltp_lookup=lambda s: 0.0)
    risk.register_risk("111", 3000)
    runner = _Runner("111", ORB("111"))
    ex = _Exec()
    asyncio.run(flatten_all([runner], port, ex, risk, "halt", "NSE_EQ"))

    assert len(ex.sent) == 1
    assert ex.sent[0].exchange_segment == "NSE_EQ"
    assert ex.sent[0].strategy == "ORB"
    assert ex.sent[0].side == "SELL"
    assert risk.committed_risk == 0


# ---------------------------------------------------------------------------
# 3. flatten_all covers an OPTION position (kill-switch reaches option legs)
# ---------------------------------------------------------------------------


def test_flatten_all_covers_option_leg_on_its_own_segment(monkeypatch):
    from apps.trader import flatten_all
    _disable_skew_guard(monkeypatch)

    feed = FakeFeed({"13": 22000.0, "OPT_22000_CE": 120.0})
    client = FakeChainClient()
    orch, signals, resolver, portfolio, risk, executor = _wire(feed, client)

    async def scenario():
        await resolver.refresh(_now())
        now = _now()
        scalper = signals._scalpers["NIFTY"]
        # Pre-seed the session so the first on_tick does NOT reset our test seams
        # (reset clears _signal_override / _bypass_time_guards on a date change).
        scalper._session_date = now.date()
        scalper._bypass_time_guards = True
        scalper._bars_seen = 25
        scalper._signal_override = "LONG"
        for _ in range(25):
            await orch.tick(now)
        assert orch._total_open() == 1, "an option leg must be open before the halt"
        leg_sid = next(iter(orch.open_leg_segments()))
        # The kill-switch flatten path must reach the option leg.
        risk.register_risk(leg_sid, 1000.0)
        await flatten_all([], portfolio, executor, risk, "kill",
                          "NSE_EQ", orchestrators=[orch])
        return leg_sid

    leg_sid = asyncio.run(scenario())
    # The option leg was flattened on its OWN NSE_FNO segment (not NSE_EQ).
    # PaperExecutor doesn't record submits, so assert via the book instead.
    assert orch._total_open() == 0           # the leg is flat
    assert portfolio.get(leg_sid).qty == 0
    assert risk.committed_risk == 0          # the option risk slot released


def test_flatten_all_resolves_option_segment_in_generic_sweep():
    """A residual option position the orchestrator did NOT flatten is still swept
    with the orchestrator's recorded NSE_FNO segment (not the equity default)."""
    from apps.trader import flatten_all

    sent = []

    class _Port:
        def __init__(self):
            self._p = {"OPT_X": Position("OPT_X", qty=130, avg_price=120.0,
                                         strategy="options_scalper")}

        def get(self, sid):
            return self._p.get(sid, Position(sid, 0))

        def open_positions(self):
            return [p for p in self._p.values() if p.qty != 0]

        async def apply_fill(self, fill, *, strategy=""):
            self._p[fill.security_id] = Position(fill.security_id, 0)
            return 0.0

    class _Exec:
        async def submit(self, intent, *, ref_price):
            sent.append(intent)
            from engine.types import Fill
            return Fill(security_id=intent.security_id, side="SELL",
                        qty=intent.qty, price=ref_price)

    class _Orch:
        # Reports the leg's segment but flattens NOTHING (forces the generic sweep).
        def open_leg_segments(self):
            return {"OPT_X": "NSE_FNO"}

        async def flatten_open(self, *a, **k):
            return None

    port = _Port()
    risk = RiskEngine(RiskParams(equity_base=500_000), port, ltp_lookup=lambda s: 0.0)
    risk.register_risk("OPT_X", 2000.0)
    asyncio.run(flatten_all([], port, _Exec(), risk, "kill", "NSE_EQ",
                            orchestrators=[_Orch()]))

    assert len(sent) == 1
    assert sent[0].exchange_segment == "NSE_FNO"   # option leg's own segment
    assert sent[0].strategy == "options_scalper"
    assert risk.committed_risk == 0


def test_flatten_all_orphan_scalper_leg_falls_back_to_fno_segment():
    """An option leg the orchestrator did NOT reconcile (no leg_seg, no runner)
    still flattens on NSE_FNO via the strategy-aware fallback — NEVER NSE_EQ —
    so the kill-switch never routes an option exit on the equity segment."""
    from apps.trader import flatten_all

    sent = []

    class _Port:
        def __init__(self):
            self._p = {"ORPHAN": Position("ORPHAN", qty=65, avg_price=120.0,
                                          strategy="options_scalper")}

        def get(self, sid):
            return self._p.get(sid, Position(sid, 0))

        def open_positions(self):
            return [p for p in self._p.values() if p.qty != 0]

        async def apply_fill(self, fill, *, strategy=""):
            self._p[fill.security_id] = Position(fill.security_id, 0)
            return 0.0

    class _Exec:
        async def submit(self, intent, *, ref_price):
            sent.append(intent)
            from engine.types import Fill
            return Fill(security_id=intent.security_id, side="SELL",
                        qty=intent.qty, price=ref_price)

    port = _Port()
    risk = RiskEngine(RiskParams(equity_base=500_000), port, ltp_lookup=lambda s: 0.0)
    risk.register_risk("ORPHAN", 1500.0)
    # No orchestrators at all → the leg is an unreconciled orphan.
    asyncio.run(flatten_all([], port, _Exec(), risk, "kill", "NSE_EQ",
                            orchestrators=[]))

    assert len(sent) == 1
    assert sent[0].exchange_segment == "NSE_FNO"   # NOT the NSE_EQ default
    assert risk.committed_risk == 0


# ---------------------------------------------------------------------------
# 4. Heartbeat "scalper" key is populated from orch.status()
# ---------------------------------------------------------------------------


def test_entry_registers_modest_risk_not_full_premium(monkeypatch):
    """A scalper entry books ONE risk_budget_per_trade slot (symmetric with the
    boot placeholder), NOT the full premium — so one open leg can't consume the
    platform daily-loss budget and silently block the governor's concurrency."""
    _disable_skew_guard(monkeypatch)
    feed = FakeFeed({"13": 22000.0})
    client = FakeChainClient()
    orch, signals, resolver, portfolio, risk, _ = _wire(feed, client)

    async def scenario():
        await resolver.refresh(_now())
        now = _now()
        scalper = signals._scalpers["NIFTY"]
        scalper._session_date = now.date()
        scalper._bypass_time_guards = True
        scalper._bars_seen = 25
        scalper._signal_override = "LONG"
        for _ in range(25):
            await orch.tick(now)
        return risk.committed_risk

    committed = asyncio.run(scenario())
    # Exactly one slot at risk_budget_per_trade, not premium*qty (120*65=7800).
    assert committed == pytest.approx(risk.risk_budget_per_trade)
    assert committed < 7800.0


def test_heartbeat_scalper_state_populated():
    feed = FakeFeed({"13": 22000.0})
    client = FakeChainClient()
    orch, *_ = _wire(feed, client)
    state = orch.status()
    # The shape the dashboard reads (with safe fallbacks).
    assert "state" in state
    assert "open_contracts" in state
    assert "governor" in state
    assert state["open_contracts"]["NIFTY"] == 0


# ---------------------------------------------------------------------------
# 5. PaperExecutor per-order option-realistic slippage
# ---------------------------------------------------------------------------


def test_paper_executor_per_order_slippage_override():
    ex = PaperExecutor(db_backend=None, slippage_bps=2.0)   # equity default
    intent = OrderIntent(security_id="OPT_1", exchange_segment="NSE_FNO",
                         side="BUY", qty=65, strategy="options_scalper",
                         slippage_bps=75.0)                  # option override
    fill = asyncio.run(ex.submit(intent, ref_price=100.0))
    # 75 bps adverse on a BUY → 100 * (1 + 0.0075) = 100.75 (not the 2 bps default).
    assert fill.price == pytest.approx(100.75)


def test_paper_executor_default_slippage_unchanged_for_equity():
    """Equity/ORB intents leave slippage_bps None → the executor default applies
    (byte-identical to the pre-integration path)."""
    ex = PaperExecutor(db_backend=None, slippage_bps=2.0)
    intent = OrderIntent(security_id="111", exchange_segment="NSE_EQ",
                         side="BUY", qty=10, strategy="ORB")   # no override
    fill = asyncio.run(ex.submit(intent, ref_price=100.0))
    assert fill.price == pytest.approx(100.02)   # 2 bps, exactly as before


def test_order_intent_slice_flag_defaults_false():
    intent = OrderIntent(security_id="111", exchange_segment="NSE_EQ",
                         side="BUY", qty=10, strategy="ORB")
    assert intent.slice_order is False
    assert intent.slippage_bps is None


# ---------------------------------------------------------------------------
# 6. Held-leg premium feed (resolver chain cache, not the WS feed)
# ---------------------------------------------------------------------------


def test_resolver_screener_lot_from_index_configs():
    """The screener's depth-gate lot is sourced from INDEX_CONFIGS (the single
    source of truth the leg routing uses), NOT the screener's own DEFAULT_LOT_SIZE
    table which can drift (BANKNIFTY 35 there vs 30 in INDEX_CONFIGS)."""
    from core.instruments import InstrumentMaster

    feed = FakeFeed({"13": 22000.0})
    client = FakeChainClient()
    _, _, resolver, _, _, _ = _wire(feed, client, underlyings=("NIFTY", "BANKNIFTY"))
    for u in ("NIFTY", "BANKNIFTY"):
        want_lot = int(InstrumentMaster.INDEX_CONFIGS[u]["lot_size"])
        want_step = int(InstrumentMaster.INDEX_CONFIGS[u]["strike_step"])
        assert resolver._screeners[u].params.lot == want_lot
        assert resolver._screeners[u].params.step == want_step
    # BANKNIFTY specifically reconciles to INDEX_CONFIGS (30), not the 35 default.
    assert resolver._screeners["BANKNIFTY"].params.lot == 30


def test_resolver_premium_for_reads_cached_chain():
    feed = FakeFeed({"13": 22000.0})
    client = FakeChainClient()
    _, _, resolver, _, _, _ = _wire(feed, client)
    asyncio.run(resolver.refresh(_now()))
    # A known CE security_id from the fake chain → its cached LTP (120.0).
    assert resolver.premium_for("OPT_22000_CE") == pytest.approx(120.0)
    # Unknown sid → None (fail-safe; never fabricates a premium).
    assert resolver.premium_for("NOPE") is None


def test_entry_sets_held_leg_for_premium_lookup(monkeypatch):
    """On a BUY fill the orchestrator records the held leg with the signal
    provider so the next tick prices the leg's premium-based exits."""
    _disable_skew_guard(monkeypatch)
    feed = FakeFeed({"13": 22000.0})
    client = FakeChainClient()
    orch, signals, resolver, portfolio, risk, _ = _wire(feed, client)

    async def scenario():
        await resolver.refresh(_now())
        now = _now()
        scalper = signals._scalpers["NIFTY"]
        scalper._session_date = now.date()
        scalper._bypass_time_guards = True
        scalper._bars_seen = 25
        scalper._signal_override = "LONG"
        for _ in range(25):
            await orch.tick(now)

    asyncio.run(scenario())
    held = signals._held_sid["NIFTY"]
    assert held is not None and held.startswith("OPT_")
    # The premium lookup the provider was wired with resolves that leg's LTP.
    assert resolver.premium_for(held) == pytest.approx(120.0)
