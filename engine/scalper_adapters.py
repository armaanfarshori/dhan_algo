"""
Concrete adapters that wire the pure scalper engine + screener into the
``ScalperOrchestrator`` Protocols (``SignalProvider`` / ``OptionResolver``).

The orchestrator (``engine/scalper_orchestrator.py``) is deliberately decoupled
from the concrete signal engine and the contract screener via two narrow
Protocols.  This module supplies the real implementations:

    ScalperSignalProvider  wraps a per-index ``strategies.options_scalper.OptionsScalper``
                           (driven by ``on_tick`` over a live underlying + held-leg
                           premium feed) → emits ``ScalpSignal`` ENTER/EXIT.

    ScalperOptionResolver  wraps ``strategies.scalper_screener.ScalperScreener.select``
                           over the LIVE option chain (fetched via the Dhan client,
                           throttled to ≥3 s + cached per index, per the execution
                           plan) → maps the chosen contract to an ``OptionLeg``.

Design rules honoured:
  • PAPER only / long-only / mode-blind — these adapters never place an order; they
    only translate signals + resolve contracts.  The executor owns mode.
  • The ``OptionResolver.resolve`` Protocol is SYNCHRONOUS.  All chain IO is async,
    so the resolver reads a per-index CACHE that an async ``refresh`` task keeps
    warm (≥3 s throttle = Dhan's optionchain rate limit).  ``resolve`` itself does
    NO network — it screens the cached chain.  This keeps the orchestrator tick
    free of blocking IO and respects the 1-req/3-s limit.
  • TZ-safe: every wall-clock value the engine sees is the aware-IST ``now`` the
    orchestrator passes through; nothing here calls ``date.today()`` / naive now.
  • Fail-soft: a missing spot, a stale/empty chain, or a screener miss yields
    ``None`` (no signal / no contract) — never an exception into the tick loop.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Callable, Dict, Optional
from zoneinfo import ZoneInfo

from engine.scalper_orchestrator import OptionLeg, ScalpSignal
from strategies.options_scalper import OptionsScalper, ScalperParams
from strategies.scalper_screener import (
    ChosenContract,
    ScalperScreener,
    ScreenerInputs,
    ScreenerParams,
)

logger = logging.getLogger("dhan.engine.scalper_adapters")
IST = ZoneInfo("Asia/Kolkata")

# Dhan rate-limits POST /v2/optionchain to one request / 3 s — never poll faster.
MIN_CHAIN_REFRESH_S = 3.0

# Type aliases for the injected live-data lookups.  Both are SYNC callables the
# trader builds over the live feed; both fail-soft (0 / None means "unknown").
SpotLookup = Callable[[str], float]          # underlying name -> index spot (₹)
PremiumLookup = Callable[[str], Optional[float]]   # security_id -> option LTP (₹)


def _ist(now: Optional[datetime]) -> datetime:
    """Normalise any injected ``now`` to aware IST (TZ-safe, ci-ist-date-trap).

    Callers may pass a UTC-aware ``now`` (the trader's refresh loop does); the
    expiry-cache key + ``dte`` must key on the IST trading date, so convert here
    rather than calling ``.date()`` on a UTC value (which could be off-by-one
    near IST midnight).  Naive datetimes are treated as IST (engine convention)."""
    if now is None:
        return datetime.now(IST)
    if now.tzinfo is None:
        return now.replace(tzinfo=IST)
    return now.astimezone(IST)


# ---------------------------------------------------------------------------
# SignalProvider — per-index OptionsScalper wrapper
# ---------------------------------------------------------------------------


class ScalperSignalProvider:
    """Concrete ``SignalProvider``: one ``OptionsScalper`` per index, driven by
    the live underlying feed + the held leg's premium.

    ``poll(underlying, now)`` ticks that index's scalper with the current spot
    (and, when a leg is open, its premium so the premium-based exits fire) and
    translates the returned ``ScalpDecision`` into the orchestrator's
    ``ScalpSignal``.  When no scalper exists for the underlying, or there is no
    spot yet, it returns ``None`` (no signal).
    """

    def __init__(
        self,
        *,
        underlyings: list[str],
        spot_lookup: SpotLookup,
        premium_lookup: PremiumLookup,
        simple: bool = True,
        param_overrides: Optional[dict] = None,
    ) -> None:
        self._spot = spot_lookup
        self._premium = premium_lookup
        # The security_id the orchestrator currently holds per underlying (set on
        # fills) so we can look up the right leg premium for that index's exits.
        self._held_sid: Dict[str, Optional[str]] = {u: None for u in underlyings}
        # Rate-limit the "spot feed dead while a leg is open" warning per index.
        self._spot_warned_at: Dict[str, float] = {}
        overrides = param_overrides or {}
        self._scalpers: Dict[str, OptionsScalper] = {}
        for u in underlyings:
            params = ScalperParams.for_index(u, simple=simple, **overrides)
            # security_id is a stable per-index label here (the underlying name) —
            # the concrete option contract is resolved separately by the resolver.
            self._scalpers[u] = OptionsScalper(security_id=u, params=params)

    # -- SignalProvider Protocol -------------------------------------------

    def poll(self, underlying: str, now: datetime) -> Optional[ScalpSignal]:
        scalper = self._scalpers.get(underlying)
        if scalper is None:
            return None
        spot = 0.0
        try:
            spot = float(self._spot(underlying) or 0.0)
        except Exception as exc:  # never let a feed hiccup kill the tick
            logger.warning("scalper signal: spot lookup failed for %s (%s)", underlying, exc)
            return None
        if spot <= 0:
            # A dead/zero underlying-spot feed suspends this index's in-session
            # protective exits (the scalper's stop/TP/trail run inside on_tick,
            # which we skip here) — surface it LOUDLY (rate-limited) when a leg is
            # open so a feed outage with live option risk is never silent. The
            # unconditional EOD square-off + kill-switch flatten are independent
            # of this path and still protect the leg.
            if self._held_sid.get(underlying) is not None:
                last = self._spot_warned_at.get(underlying)
                now_m = time.monotonic()
                if last is None or now_m - last >= 60.0:
                    self._spot_warned_at[underlying] = now_m
                    logger.warning(
                        "scalper signal: %s spot feed dead (=0) while a leg is OPEN "
                        "— intraday stop/TP suspended until spot returns; EOD + "
                        "kill-switch flatten still apply", underlying)
            return None

        premium: Optional[float] = None
        held = self._held_sid.get(underlying)
        if held is not None:
            try:
                premium = self._premium(held)
            except Exception as exc:
                logger.warning("scalper signal: premium lookup failed for %s (%s)", held, exc)
                premium = None

        try:
            decision = scalper.on_tick(now, spot, option_premium=premium)
        except Exception as exc:
            logger.error("scalper on_tick error for %s: %s", underlying, exc)
            return None
        if decision is None:
            return None

        return ScalpSignal(
            underlying=underlying,
            action=decision.action,
            side=decision.side,
            option_type=decision.option_type,
            strike=int(decision.strike),
            lots=int(decision.lots),
            reason=decision.reason,
        )

    def notify_fill(self, underlying: str, side: str, lots: int,
                    premium: float, now: datetime) -> None:
        scalper = self._scalpers.get(underlying)
        if scalper is None:
            return
        scalper.notify_fill(side, lots, premium, now)
        # On a full flatten the orchestrator drops the leg; clear our held sid so
        # the next entry's premium lookup doesn't track a closed contract.
        if side == "SELL" and scalper._position_lots() == 0:
            self._held_sid[underlying] = None

    def open_lots(self, underlying: str) -> int:
        scalper = self._scalpers.get(underlying)
        return scalper._position_lots() if scalper is not None else 0

    # -- helper used by the resolver/trader to track the held leg ----------

    def set_held(self, underlying: str, security_id: Optional[str]) -> None:
        """Record the concrete contract the orchestrator now holds for an index so
        the next premium lookup prices the right leg's exits."""
        if underlying in self._held_sid:
            self._held_sid[underlying] = security_id


# ---------------------------------------------------------------------------
# OptionResolver — ScalperScreener over the live (cached) chain
# ---------------------------------------------------------------------------


class _ChainCache:
    """One index's most-recent parsed chain snapshot + spot/expiry context."""

    __slots__ = ("rows", "spot", "expiry_date", "dte", "fetched_at")

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.spot: float = 0.0
        self.expiry_date: Optional[date] = None
        self.dte: int = 0
        # None = NEVER fetched (always eligible). MUST NOT be 0.0: early in a
        # process's life time.monotonic() is < min_refresh_s, so a 0.0 sentinel
        # would make the very first refresh look "throttled" and silently skip it.
        self.fetched_at: Optional[float] = None   # time.monotonic() of last fetch


class ScalperOptionResolver:
    """Concrete ``OptionResolver``: screens a per-index live option chain.

    The Protocol's ``resolve`` is SYNC and must do no network IO from inside the
    orchestrator tick.  An async ``refresh(now)`` (driven by the trader's own poll
    task) fetches + parses each index's chain at most once per ``MIN_CHAIN_REFRESH_S``
    and caches it; ``resolve`` screens that cache.  The screener never fabricates a
    contract — when nothing clears the gates ``resolve`` returns ``None``.
    """

    def __init__(
        self,
        *,
        client,
        underlyings: list[str],
        signal_provider: Optional[ScalperSignalProvider] = None,
        max_spread_pct: float = 0.02,
        screener_overrides: Optional[dict] = None,
        min_refresh_s: float = MIN_CHAIN_REFRESH_S,
    ) -> None:
        self._client = client
        self._underlyings = list(underlyings)
        self._signal_provider = signal_provider
        self._min_refresh_s = max(MIN_CHAIN_REFRESH_S, float(min_refresh_s))
        overrides = screener_overrides or {}
        # Drive the screener's strike step + lot from INDEX_CONFIGS (the single
        # source of truth the live ATM lookup + leg routing use) so the screener's
        # depth gate sizes against the SAME lot the orchestrator routes with —
        # the screener's own DEFAULT_LOT_SIZE table can drift (e.g. BANKNIFTY 35
        # there vs 30 in INDEX_CONFIGS); INDEX_CONFIGS wins.
        from core.instruments import InstrumentMaster

        self._screeners: Dict[str, ScalperScreener] = {}
        for u in underlyings:
            icfg = InstrumentMaster.INDEX_CONFIGS.get(u.upper()) or {}
            step_lot = {}
            if icfg.get("strike_step"):
                step_lot["step"] = int(icfg["strike_step"])
            if icfg.get("lot_size"):
                step_lot["lot"] = int(icfg["lot_size"])
            params = ScreenerParams(index=u, max_spread_pct=max_spread_pct,
                                    **{**step_lot, **overrides})
            self._screeners[u] = ScalperScreener(params)
        self._cache: Dict[str, _ChainCache] = {u: _ChainCache() for u in underlyings}
        # Per-index expiry cache: the front-weekly expiry only changes once a day,
        # so cache (expiry_str, on_date) and skip the expirylist REST call on
        # subsequent refreshes the same day — halves the API spend AND avoids a
        # second back-to-back F&O-endpoint call inside the 3 s window.
        self._expiry_cache: Dict[str, tuple] = {}   # underlying -> (expiry_str, date)

    # -- async refresh (driven by the trader's poll task) ------------------

    async def refresh(self, now: Optional[datetime] = None) -> None:
        """Refresh every index's chain cache, honouring the ≥3 s throttle.

        Spaces the per-index fetches so two indices never burst the 1-req/3-s
        optionchain limit in the same instant.  Each fetch is independent: one
        index failing never blocks another.  Best-effort — never raises."""
        import asyncio

        from core.instruments import InstrumentMaster

        now = _ist(now)   # TZ-safe: key the expiry cache + dte on the IST date
        first = True
        for u in self._underlyings:
            cache = self._cache[u]
            if (cache.fetched_at is not None
                    and time.monotonic() - cache.fetched_at < self._min_refresh_s):
                continue
            if not first:
                await asyncio.sleep(self._min_refresh_s)   # space the calls
            first = False
            cfg = InstrumentMaster.INDEX_CONFIGS.get(u.upper())
            if not cfg:
                logger.warning("scalper resolver: unknown index %s — skipping refresh", u)
                continue
            try:
                await self._refresh_one(u, cfg, now)
            except Exception as exc:
                logger.warning("scalper resolver: chain refresh failed for %s (%s)", u, exc)

    async def _refresh_one(self, underlying: str, cfg: dict,
                           now: Optional[datetime]) -> None:
        import asyncio

        from core.fno_backfill import parse_option_chain

        scrip = int(cfg["underlying_id"])
        seg = cfg["underlying_segment"]
        now = _ist(now)              # TZ-safe: IST trading date for cache + dte
        today = now.date()

        # Reuse today's cached front-weekly expiry (it changes once a day) — skips
        # the expirylist REST call, halving API spend and avoiding a second
        # back-to-back F&O-endpoint call inside the 3 s window.
        cached_exp = self._expiry_cache.get(underlying)
        if cached_exp is not None and cached_exp[1] == today:
            expiry_str = cached_exp[0]
        else:
            expiries = await self._client.get_fno_expiry_list(scrip, seg)
            exp_list = ((expiries or {}).get("data")
                        if isinstance(expiries, dict) else expiries)
            if not exp_list:
                logger.info("scalper resolver: no expiries for %s", underlying)
                return
            # Front weekly = nearest upcoming expiry.
            expiry_str = sorted(str(e) for e in exp_list)[0]
            self._expiry_cache[underlying] = (expiry_str, today)
            # Space the chain call off the expirylist call (≥3 s) in case Dhan
            # shares the 1-req/3-s bucket across F&O-chain endpoints.
            await asyncio.sleep(self._min_refresh_s)
        expiry_date = date.fromisoformat(expiry_str[:10])

        chain = await self._client.get_fno_option_chain(scrip, expiry_str, seg)
        rows = parse_option_chain(chain, scrip, seg, expiry_date, now=now)
        spot = 0.0
        if isinstance(chain, dict):
            data = chain.get("data") or {}
            try:
                spot = float(data.get("last_price") or 0.0)
            except (TypeError, ValueError):
                spot = 0.0

        dte = max(0, (expiry_date - today).days)

        cache = self._cache[underlying]
        cache.rows = rows
        cache.spot = spot
        cache.expiry_date = expiry_date
        cache.dte = dte
        cache.fetched_at = time.monotonic()
        logger.info("scalper resolver: %s chain refreshed — %d rows, spot=%.2f, "
                    "expiry=%s dte=%d", underlying, len(rows), spot, expiry_str, dte)

    # -- premium feed for the held leg (cached chain LTP) -------------------

    def premium_for(self, security_id) -> Optional[float]:
        """Latest cached LTP for an option ``security_id`` across all index
        chain caches, or None if not present / non-positive.

        The held leg's premium comes from the REST option-chain cache (refreshed
        on the ≥3 s throttle) — the WS feed cannot stream a contract subscribed
        mid-session (MarketFeed binds its subscription list once at connect), so
        the cached chain is the truthful, rate-limited premium source for the
        scalper's premium-based exits."""
        sid = str(security_id)
        for cache in self._cache.values():
            for row in cache.rows:
                if str(row.get("security_id")) == sid:
                    ltp = row.get("ltp")
                    try:
                        ltp = float(ltp) if ltp is not None else 0.0
                    except (TypeError, ValueError):
                        ltp = 0.0
                    return ltp if ltp > 0 else None
        return None

    # -- OptionResolver Protocol -------------------------------------------

    def resolve(self, signal: ScalpSignal) -> Optional[OptionLeg]:
        screener = self._screeners.get(signal.underlying)
        cache = self._cache.get(signal.underlying)
        if screener is None or cache is None:
            return None
        if not cache.rows or cache.spot <= 0 or cache.expiry_date is None:
            logger.info("scalper resolver: no cached chain for %s yet — no contract",
                        signal.underlying)
            return None

        # ENTER decides direction (LONG→CE / SHORT→PE).  An EXIT-shaped signal
        # (boot-reconcile probe) carries no direction → default to CE so the
        # screener can still surface a contract for the held side; the orchestrator
        # only uses the security_id match on reconcile.
        direction = "LONG" if signal.option_type in ("", "CE") else "SHORT"

        inputs = ScreenerInputs(
            index=signal.underlying,
            direction=direction,
            spot=cache.spot,
            chain_rows=cache.rows,
            expiry_date=cache.expiry_date,
            dte=cache.dte,
            now=datetime.now(IST),
        )
        try:
            chosen = screener.select(inputs)
        except Exception as exc:
            logger.warning("scalper resolver: screener error for %s (%s)",
                           signal.underlying, exc)
            return None
        if chosen is None:
            return None
        return self._to_leg(signal.underlying, chosen)

    def _to_leg(self, underlying: str, chosen: ChosenContract) -> Optional[OptionLeg]:
        from core.instruments import InstrumentMaster

        cfg = InstrumentMaster.INDEX_CONFIGS.get(underlying.upper()) or {}
        lot = int(cfg.get("lot_size") or 0)
        seg = cfg.get("option_segment") or "NSE_FNO"
        sid = chosen.security_id
        if sid is None or lot <= 0:
            logger.warning("scalper resolver: %s contract missing sid/lot "
                           "(sid=%s lot=%d) — cannot route", underlying, sid, lot)
            return None
        return OptionLeg(
            underlying=underlying,
            security_id=str(sid),
            exchange_segment=seg,
            option_type=chosen.option_type,
            strike=int(chosen.strike),
            lot=lot,
            ltp=float(chosen.ref_ltp or 0.0),
        )
