"""
StrategyRunner — drives one pure strategy instance against live market data.

Data path per poll:
    LiveFeed tick (WebSocket, preferred)
        └─ REST get_ohlc fallback when the feed is cold for this security
    → ORB.on_tick() (pure decision)
    → ENTER: KronosGate (shadow logs / enforcing blocks)
              → RiskEngine sizing + pre-trade check
              → executor.submit() → Portfolio.apply_fill() → ORB.notify_fill()
    → EXIT:  RiskEngine check (exits always pass position-count caps)
              → executor.submit() → Portfolio.apply_fill() → ORB.notify_flat()

The runner never mutates position state directly — Portfolio owns it, the
strategy only mirrors what it is told via notify_*.
"""
import asyncio
import logging
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

from engine.types import OrderIntent
from strategies.orb import ORB, Decision

logger = logging.getLogger("dhan.engine.runner")
IST = ZoneInfo("Asia/Kolkata")

# Poll only inside the trading window (with pre/post margin) — zero API
# calls outside 9:00–15:35 IST weekdays.
_WINDOW_OPEN = dtime(9, 0)
_WINDOW_CLOSE = dtime(15, 35)
_FEED_FRESH_S = 30   # use WS tick if younger than this, else REST fallback


class StrategyRunner:
    def __init__(self, strategy: ORB, *, client, feed, gate, risk, executor,
                 portfolio, exchange_segment: str = "NSE_EQ",
                 poll_interval: float = 20.0, poll_offset: float = 0.0,
                 max_entries_per_session: int = 4, batched_ohlc=None):
        self.strategy = strategy
        self.sid = strategy.security_id
        self._client = client
        self._feed = feed
        # Shared, coalescing, TTL-cached REST OHLC fallback. When None the
        # runner falls back to a per-sid get_ohlc (legacy / test path).
        self._batched_ohlc = batched_ohlc
        self._gate = gate
        self._risk = risk
        self._executor = executor
        self._portfolio = portfolio
        self._segment = exchange_segment
        self._poll_interval = poll_interval
        self._poll_offset = poll_offset
        self._max_entries = max_entries_per_session
        self._entries_today = 0
        self._session_date = None
        self._running = False
        self.last_price: float = 0.0
        self._last_ws_tick_ts: Optional[datetime] = None

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        logger.info("▶ Runner %s started (poll %.0fs, offset %.1fs)",
                    self.sid, self._poll_interval, self._poll_offset)
        if self._poll_offset > 0:
            await asyncio.sleep(self._poll_offset)
        while self._running:
            now = datetime.now(IST)
            if not self._in_window(now):
                await asyncio.sleep(60)
                continue
            try:
                await self._poll_once(now)
            except Exception as exc:
                logger.error("Runner %s tick error: %s", self.sid, exc)
            await asyncio.sleep(self._poll_interval)

    def stop(self):
        self._running = False

    @staticmethod
    def _in_window(now: datetime) -> bool:
        return now.weekday() < 5 and _WINDOW_OPEN <= now.time() <= _WINDOW_CLOSE

    # ── One poll ──────────────────────────────────────────────────────────────

    async def _poll_once(self, now: datetime):
        if self._session_date != now.date():
            self._session_date = now.date()
            self._entries_today = 0

        price, high, low = await self._get_price()
        if price <= 0:
            # Feed AND REST both failed. If we hold a position we MUST still run the
            # strategy (stop/target/EOD square-off) — skipping the poll would leave
            # the position naked/unstoppable until the feed recovers. Fall back to
            # the last known good price so exit logic can still fire. (We only hit
            # this branch with a position open, and ORB never ENTERs while in a
            # position, so a stale price can't trigger a bad entry.)
            pos = self._portfolio.get(self.sid)
            if pos.qty != 0 and self.last_price > 0:
                logger.warning(
                    "Runner %s: no fresh price — running exit logic on last known ₹%.2f",
                    self.sid, self.last_price)
                price = high = low = self.last_price
            elif pos.qty != 0 and pos.avg_price > 0:
                # Never-ticked position (feed+REST both gave nothing, no prior
                # last_price): the executor rejects ref_price<=0, so a stop/EOD
                # exit could never fire and the position would be stuck open.
                # Fall back to the entry avg_price as the ref so exit logic can
                # still run. This is a last resort — surface it loudly.
                logger.critical(
                    "Runner %s: no price at all — running exit logic on avg_price ₹%.2f "
                    "(never-ticked position)", self.sid, pos.avg_price)
                price = high = low = pos.avg_price
            else:
                return
        self.last_price = price

        decision = self.strategy.on_tick(now, price, high, low)
        if decision is None:
            return
        if decision.action == "ENTER":
            await self._handle_entry(decision, price)
        elif decision.action == "EXIT":
            await self._handle_exit(decision, price)

    async def _get_price(self) -> tuple[float, Optional[float], Optional[float]]:
        # WebSocket first — only use cached tick if it is fresh enough.
        # _FEED_FRESH_S guards against pre-disconnect prices surviving a
        # reconnect: after _invalidate_tick_cache() the age comes back as None
        # (no data) and must be treated as stale → fall through to REST.
        # Feeds that do not expose get_tick_age_s() (legacy / test stubs) are
        # treated as always-fresh to preserve backward compatibility.
        if self._feed is not None:
            tick_is_fresh = True   # default: trust the feed (legacy path)
            if hasattr(self._feed, "get_tick_age_s"):
                age = self._feed.get_tick_age_s(self.sid)
                if age is None:
                    # Cache cleared on disconnect — do not use stale price
                    tick_is_fresh = False
                    logger.debug("Runner %s: feed cache cold, falling back to REST", self.sid)
                elif age >= _FEED_FRESH_S:
                    # Price is too old — fall back to REST
                    tick_is_fresh = False
                    logger.debug(
                        "Runner %s: feed tick stale (%.0fs >= %ds), falling back to REST",
                        self.sid, age, _FEED_FRESH_S,
                    )
            if tick_is_fresh:
                tick = self._feed.get_ohlc_tick(self.sid)
                ltp = float(tick.get("last_price") or 0)
                if ltp > 0:
                    ohlc = tick.get("ohlc", {})
                    return ltp, float(ohlc.get("high") or ltp), float(ohlc.get("low") or ltp)
        # REST fallback.
        # Prefer the SHARED batched fetcher: it coalesces all concurrent stale
        # lookups into ONE get_ohlc call for the whole watchlist, so N quiet
        # names no longer fire N simultaneous single-sid calls → no 429 burst.
        # Falls back to a per-sid get_ohlc when no batcher is wired (tests).
        try:
            if self._batched_ohlc is not None:
                t = await self._batched_ohlc.get(self.sid)
                if not t:
                    return 0.0, None, None
            else:
                data = await self._client.get_ohlc({self._segment: [int(self.sid)]})
                t = data.get("data", {}).get(self._segment, {}).get(self.sid, {})
            ltp = float(t.get("last_price") or 0)
            ohlc = t.get("ohlc", {})
            return ltp, float(ohlc.get("high") or ltp), float(ohlc.get("low") or ltp)
        except Exception as exc:
            logger.warning("Runner %s: price fetch failed (%s)", self.sid, exc)
            return 0.0, None, None

    # ── Entries / exits ───────────────────────────────────────────────────────

    async def _handle_entry(self, d: Decision, price: float):
        if self._entries_today >= self._max_entries:
            logger.info("Runner %s: max entries (%d) reached for session",
                        self.sid, self._max_entries)
            return

        # Kronos gate — shadow logs, enforcing may veto
        if not await self._gate.allows(self.sid, d.side, self._segment, strategy="ORB"):
            logger.info("Runner %s: Kronos gate blocked %s entry", self.sid, d.side)
            return

        adv = await self._risk.get_adv(self.sid)
        qty = self._risk.size_position(entry=price, stop=d.stop, adv=adv)
        intent = OrderIntent(
            security_id=self.sid, exchange_segment=self._segment,
            side=d.side, qty=qty, strategy="ORB", reason=d.reason,
            stop_price=d.stop, target_price=d.target)
        ok, msg = self._risk.check_intent(intent, price)
        if not ok:
            logger.warning("Runner %s: risk blocked entry — %s", self.sid, msg)
            return

        fill = await self._executor.submit(intent, ref_price=price)
        if fill is None:
            return
        await self._portfolio.apply_fill(fill, strategy="ORB")
        self.strategy.notify_fill(fill.side, fill.qty, fill.price)
        # Book this position's stop-distance risk against the daily budget
        self._risk.register_risk(self.sid,
                                 self._risk.position_risk(fill.price, d.stop, fill.qty))
        self._entries_today += 1

    async def _handle_exit(self, d: Decision, price: float):
        pos = self._portfolio.get(self.sid)
        if pos.qty == 0:
            self.strategy.notify_flat()   # strategy thought it had a position — resync
            return
        side = "SELL" if pos.qty > 0 else "BUY"
        intent = OrderIntent(
            security_id=self.sid, exchange_segment=self._segment,
            side=side, qty=abs(pos.qty), strategy="ORB", reason=d.reason)
        ok, msg = self._risk.check_intent(intent, price)
        if not ok:
            # Exits must not be silently swallowed — this is how positions orphan
            logger.critical("Runner %s: EXIT BLOCKED by risk (%s) — retrying next poll",
                            self.sid, msg)
            return
        fill = await self._executor.submit(intent, ref_price=price)
        if fill is None:
            # The executor returned no fill. If the broker is already flat for
            # this security (position closed out-of-band), the committed risk
            # slot would otherwise leak forever and block new entries — release
            # it and resync the strategy. Only if the position is STILL open do
            # we keep the slot and retry the exit next poll.
            if self._portfolio.get(self.sid).qty == 0:
                self._risk.release_risk(self.sid)
                self.strategy.notify_flat()
                logger.warning("Runner %s: exit fill None but broker flat — "
                               "released risk + resynced", self.sid)
                return
            logger.critical("Runner %s: EXIT EXECUTION FAILED — retrying next poll", self.sid)
            return
        realized = await self._portfolio.apply_fill(fill, strategy="ORB")
        # Partial fill (live): the residual position is still open. Releasing the
        # risk slot + notifying flat here would leave a live position carrying ZERO
        # booked risk and a strategy that thinks it's flat. Keep the slot booked
        # (conservative) and retry the remainder next poll; only finalize when flat.
        remaining = self._portfolio.get(self.sid).qty
        if remaining != 0:
            logger.warning("Runner %s: PARTIAL exit (%d filled, %d remain) — keeping "
                           "risk slot, retrying remainder next poll", self.sid, fill.qty, remaining)
            return
        self.strategy.notify_flat()
        self._risk.release_risk(self.sid)   # free its slice of the daily budget
        logger.info("Runner %s: exited (%s) — realized ₹%+.2f", self.sid, d.reason, realized)

    # ── Introspection (heartbeat) ────────────────────────────────────────────

    def status(self) -> dict:
        return {
            **self.strategy.status(),
            "last_price": self.last_price,
            "entries_today": self._entries_today,
            "running": self._running,
        }
