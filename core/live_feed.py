"""
Live Market Feed — WebSocket
============================
Subscribes to Dhan's WebSocket feed for real-time tick data.
Aggregates ticks into 1-minute OHLC candles per security.
Completely replaces REST get_ohlc() polling — no rate limits.

Usage:
    feed = LiveFeed(client_id, access_token)
    await feed.subscribe({"NSE_EQ": [2885, 3045], "IDX_I": [13, 25]})
    asyncio.create_task(feed.run())

    # In scanner tick:
    tick = feed.get_tick("2885")      # latest LTP + OHLC
    candle = feed.get_candle("2885")  # latest closed 1-min candle
"""

import asyncio
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from dhanhq import DhanContext
from dhanhq.marketfeed import MarketFeed

logger = logging.getLogger("dhan.live_feed")
IST = ZoneInfo("Asia/Kolkata")

# Dhan segment code → WebSocket exchange constant
SEG_TO_EX = {
    "IDX_I":    MarketFeed.IDX,
    "NSE_EQ":   MarketFeed.NSE,
    "NSE_FNO":  MarketFeed.NSE_FNO,
    "BSE_FNO":  MarketFeed.BSE_FNO,
    "MCX_COMM": MarketFeed.MCX,
}


class CandleBuilder:
    """Aggregates ticks into 1-minute OHLC candles."""

    def __init__(self, maxlen: int = 60):
        self.candles: deque = deque(maxlen=maxlen)
        self._open = self._high = self._low = self._close = 0.0
        self._volume = 0
        self._minute = -1

    def on_tick(self, price: float, volume: int = 0, ts: Optional[datetime] = None):
        now    = ts or datetime.now(IST)
        minute = now.hour * 60 + now.minute

        if minute != self._minute:
            # Close previous candle
            if self._minute >= 0 and self._open > 0:
                self.candles.append({
                    "open":   self._open,
                    "high":   self._high,
                    "low":    self._low,
                    "close":  self._close,
                    "volume": self._volume,
                    "minute": self._minute,
                })
            # Start new candle
            self._open = self._high = self._low = self._close = price
            self._volume = volume
            self._minute = minute
        else:
            self._high   = max(self._high, price)
            self._low    = min(self._low,  price)
            self._close  = price
            self._volume += volume

    @property
    def last_closed(self) -> Optional[dict]:
        return self.candles[-1] if self.candles else None

    @property
    def current(self) -> dict:
        """Intrabar OHLC of the candle in progress."""
        return {
            "open":  self._open,
            "high":  self._high,
            "low":   self._low,
            "close": self._close,
            "volume":self._volume,
        }


class LiveFeed:
    """
    Manages a Dhan WebSocket connection and provides per-security tick + candle data.
    Thread-safe within a single asyncio event loop.
    """

    _RECONNECT_MIN     = 5      # normal reconnect backoff floor (s)
    _RECONNECT_MAX     = 60     # backoff ceiling (s)
    _RECONNECT_429_MIN = 30     # on HTTP 429, never retry faster than this

    def __init__(self, client_id: str, access_token: str, on_tick=None,
                 bar_builder=None):
        self._client_id    = client_id
        self._access_token = access_token
        self._subscriptions: list = []      # list of (exchange_code, security_id)

        self._ticks:    Dict[str, dict]          = {}   # sid → {ltp, volume, ...}
        self._tick_ts:  Dict[str, float]         = {}   # sid → monotonic receive time (s)
        self._candles:  Dict[str, CandleBuilder] = defaultdict(CandleBuilder)
        self._running  = False
        self._feed: Optional[MarketFeed] = None
        self._connected = asyncio.Event()
        # Optional sync callback(sid, ltp, cum_volume) — BarBuilder hooks in
        # here to persist 1-minute bars to the DB. Must never raise/block.
        self._on_tick_cb = on_tick
        # When a BarBuilder is wired in, get_ohlc_tick() reads intrabar H/L
        # from it instead of the internal CandleBuilder — single source of
        # truth so the strategy and Kronos/DB see identical intrabar candles.
        # If bar_builder is not passed explicitly, auto-detect it from the
        # on_tick bound-method's owner (duck-typed: any object with get_current).
        # This lets existing call-sites (trader.py) work without modification.
        if bar_builder is not None:
            self._bar_builder = bar_builder
        elif on_tick is not None and hasattr(getattr(on_tick, "__self__", None), "get_current"):
            self._bar_builder = on_tick.__self__
        else:
            self._bar_builder = None

    # ── Public API ────────────────────────────────────────────────────────────

    def subscribe(self, instruments: Dict[str, list]):
        """
        instruments = {"NSE_EQ": [2885, 3045], "IDX_I": [13, 25, 51], "NSE_FNO": [41784]}
        Call before run().
        """
        for seg, sids in instruments.items():
            ex = SEG_TO_EX.get(seg)
            if ex is None:
                logger.warning(f"LiveFeed: unknown segment {seg}, skipping")
                continue
            for sid in sids:
                # Dhan v2 feed requires SecurityId as a STRING in the subscribe
                # JSON — an int is accepted silently and never streams a packet.
                self._subscriptions.append((ex, str(int(sid)), MarketFeed.Quote))
        logger.info(f"LiveFeed: {len(self._subscriptions)} instruments subscribed")

    def get_tick(self, security_id: str) -> Optional[dict]:
        """Returns latest tick: {ltp, open, high, low, close, volume}"""
        return self._ticks.get(str(security_id))

    def get_ltp(self, security_id: str) -> float:
        t = self._ticks.get(str(security_id), {})
        return float(t.get("LTP") or t.get("ltp") or 0)

    def get_ohlc_tick(self, security_id: str) -> dict:
        """Returns current intrabar OHLC suitable for strategy on_tick().

        When a BarBuilder is wired in (the normal live path), intrabar H/L/O/C
        come from BarBuilder.get_current() — the same aggregator that writes
        bars to the DB and feeds Kronos.  This is the single-source-of-truth
        fix for DATA-04: the strategy and the DB now see identical intrabar
        candles.

        When no BarBuilder is wired (unit tests / lightweight callers), falls
        back to the internal CandleBuilder so behaviour is unchanged.
        """
        sid  = str(security_id)
        tick = self._ticks.get(sid, {})
        ltp  = float(tick.get("LTP") or tick.get("ltp") or 0)

        # Prefer BarBuilder's in-progress bar (authoritative, feeds Kronos/DB)
        if self._bar_builder is not None:
            cur = self._bar_builder.get_current(sid) or {}
        else:
            cb  = self._candles.get(sid)
            cur = cb.current if cb else {}

        return {
            "last_price": ltp,
            "ohlc": {
                "open":  cur.get("open",  ltp),
                "high":  cur.get("high",  ltp),
                "low":   cur.get("low",   ltp),
                "close": cur.get("close", ltp),
            },
            "volume": cur.get("volume", 0),
        }

    def get_tick_age_s(self, security_id: str) -> Optional[float]:
        """Seconds since the last tick was received for this security.

        Returns None if no tick has ever been received (i.e. the cache is cold
        or was cleared on a disconnect).  Callers should treat None as infinite
        age (stale / no data).
        """
        ts = self._tick_ts.get(str(security_id))
        if ts is None:
            return None
        return time.monotonic() - ts

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def all_subscribed_sids(self) -> list:
        return [str(sid) for _, sid, _ in self._subscriptions]

    # ── Internal ──────────────────────────────────────────────────────────────

    async def run(self):
        """Main async loop — reconnects on disconnect with exponential backoff.

        Flat fast reconnects on an HTTP 429 are self-defeating: the server is
        explicitly saying "too many requests", so retrying every 5s only keeps
        the limit tripped. We back off exponentially (longer on a 429), and
        always close the previous socket before retrying so we don't leak
        connections into Dhan's per-client cap.
        """
        self._running = True
        logger.info("LiveFeed: starting WebSocket connection…")

        delay = self._RECONNECT_MIN
        try:
            while self._running:
                try:
                    await self._connect_and_listen()
                    delay = self._RECONNECT_MIN          # clean exit → reset
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    await self._close_feed()
                    self._connected.clear()
                    is_429 = "429" in str(e)
                    if is_429:
                        delay = max(delay, self._RECONNECT_429_MIN)
                    logger.warning("LiveFeed: connection error (%s), reconnecting in %ds…",
                                   e, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self._RECONNECT_MAX)
        finally:
            await self._close_feed()

    def _invalidate_tick_cache(self):
        """Clear cached ticks and their timestamps on disconnect.

        After a WebSocket disconnect the cached prices are pre-disconnect data.
        Clearing them prevents the runner from handing the ORB strategy a stale
        price as if it were fresh — it will fall back to REST until new ticks
        arrive on reconnect.
        """
        self._ticks.clear()
        self._tick_ts.clear()
        logger.debug("LiveFeed: tick cache invalidated on disconnect")

    async def _close_feed(self):
        """Best-effort close of the current MarketFeed socket. Releases the
        connection on Dhan's side instead of leaving it for a server-side TTL."""
        feed = self._feed
        self._feed = None
        if feed is None:
            return
        self._invalidate_tick_cache()
        try:
            ws = getattr(feed, "ws", None)
            if ws is not None:
                await ws.close()
        except Exception as exc:
            logger.debug("LiveFeed: error closing socket (%s)", exc)

    async def _connect_and_listen(self):
        # Drop any prior socket before opening a new one.
        await self._close_feed()
        ctx  = DhanContext(self._client_id, self._access_token)
        feed = MarketFeed(
            dhan_context = ctx,
            instruments  = self._subscriptions,
            version      = "v2",
        )
        self._feed = feed

        # Connect
        await feed.connect()
        self._connected.set()
        logger.info(f"LiveFeed: connected ✓  ({len(self._subscriptions)} instruments)")

        while self._running:
            try:
                data = await feed.get_instrument_data()
                if data:
                    self._on_data(data)
            except Exception as e:
                if self._running:
                    raise
                break

    @staticmethod
    def _parse_ltt(ltt_str: str) -> Optional[datetime]:
        """Convert exchange LTT ("HH:MM:SS" UTC string from dhanhq Quote/Ticker
        packet) to an IST-aware datetime anchored to today's UTC date.

        The dhanhq marketfeed library decodes the LTT epoch via
        ``datetime.utcfromtimestamp(epoch).strftime('%H:%M:%S')``, yielding a
        plain ``"HH:MM:SS"`` string in UTC (the raw epoch integer is no longer
        accessible from the parsed dict).  We reconstruct it by combining
        today's UTC date with the time component and then converting to IST.

        Edge case: if the UTC time crosses midnight (e.g. LTT "18:31" on a day
        where IST is "00:01" next day) we use today's UTC date which is correct
        because market hours are 09:15–15:30 IST = 03:45–10:00 UTC (always same
        UTC date).

        Returns None when the field is absent, empty, or unparsable so the
        caller falls back to server-receive time.
        """
        if not ltt_str or not isinstance(ltt_str, str):
            return None
        try:
            t = datetime.strptime(ltt_str, "%H:%M:%S")
            today_utc = datetime.now(timezone.utc).date()
            ltt_utc = datetime(
                today_utc.year, today_utc.month, today_utc.day,
                t.hour, t.minute, t.second,
                tzinfo=timezone.utc,
            )
            return ltt_utc.astimezone(IST)
        except ValueError:
            return None

    def _on_data(self, data):
        """Process incoming WebSocket message."""
        if not isinstance(data, dict):
            return

        sid = str(data.get("security_id") or data.get("LTT_security_id") or "")
        if not sid:
            return

        ltp = float(data.get("LTP") or data.get("ltp") or 0)
        if not ltp:
            return

        volume = int(data.get("volume") or data.get("vol") or 0)

        # Extract exchange last-traded-time from the Quote/Ticker packet.
        # dhanhq parses the binary LTT epoch into data["LTT"] as a UTC
        # "HH:MM:SS" string.  Convert it to an IST-aware datetime so bars are
        # bucketed by exchange time rather than server-receive time (the
        # deviation is normally sub-second for liquid names, but under GC
        # pauses or reconnects it can cross a minute boundary and mis-bucket).
        # Fallback to server-receive time when the field is missing/zero.
        tick_ts: Optional[datetime] = self._parse_ltt(data.get("LTT"))

        self._ticks[sid]   = data
        self._tick_ts[sid] = time.monotonic()
        self._candles[sid].on_tick(ltp, volume, ts=tick_ts)

        if self._on_tick_cb is not None:
            try:
                self._on_tick_cb(sid, ltp, volume, tick_ts)
            except Exception as exc:
                logger.debug("LiveFeed on_tick callback error: %s", exc)

    def stop(self):
        self._running = False
        self._connected.clear()
        logger.info("LiveFeed: stopped")
