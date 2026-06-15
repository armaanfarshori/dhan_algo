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
from collections import defaultdict, deque
from datetime import datetime, timezone
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

    def on_tick(self, price: float, volume: int = 0):
        now    = datetime.now(IST)
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

    def __init__(self, client_id: str, access_token: str, on_tick=None):
        self._client_id    = client_id
        self._access_token = access_token
        self._subscriptions: list = []      # list of (exchange_code, security_id)

        self._ticks:   Dict[str, dict]          = {}   # sid → {ltp, volume, ...}
        self._candles: Dict[str, CandleBuilder] = defaultdict(CandleBuilder)
        self._running  = False
        self._feed: Optional[MarketFeed] = None
        self._connected = asyncio.Event()
        # Optional sync callback(sid, ltp, cum_volume) — BarBuilder hooks in
        # here to persist 1-minute bars to the DB. Must never raise/block.
        self._on_tick_cb = on_tick

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
        """Returns current intrabar OHLC suitable for strategy on_tick()."""
        sid  = str(security_id)
        tick = self._ticks.get(sid, {})
        ltp  = float(tick.get("LTP") or tick.get("ltp") or 0)
        cb   = self._candles.get(sid)
        cur  = cb.current if cb else {}
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

    async def _close_feed(self):
        """Best-effort close of the current MarketFeed socket. Releases the
        connection on Dhan's side instead of leaving it for a server-side TTL."""
        feed = self._feed
        self._feed = None
        if feed is None:
            return
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

        self._ticks[sid] = data
        self._candles[sid].on_tick(ltp, volume)

        if self._on_tick_cb is not None:
            try:
                self._on_tick_cb(sid, ltp, volume)
            except Exception as exc:
                logger.debug("LiveFeed on_tick callback error: %s", exc)

    def stop(self):
        self._running = False
        self._connected.clear()
        logger.info("LiveFeed: stopped")
