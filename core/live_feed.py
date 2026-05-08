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

    def __init__(self, client_id: str, access_token: str):
        self._client_id    = client_id
        self._access_token = access_token
        self._subscriptions: list = []      # list of (exchange_code, security_id)

        self._ticks:   Dict[str, dict]          = {}   # sid → {ltp, volume, ...}
        self._candles: Dict[str, CandleBuilder] = defaultdict(CandleBuilder)
        self._running  = False
        self._feed: Optional[MarketFeed] = None
        self._connected = asyncio.Event()

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
                self._subscriptions.append((ex, int(sid), MarketFeed.Quote))
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
        """Main async loop — reconnects on disconnect."""
        self._running = True
        logger.info("LiveFeed: starting WebSocket connection…")

        while self._running:
            try:
                await self._connect_and_listen()
            except Exception as e:
                logger.warning(f"LiveFeed: connection error ({e}), reconnecting in 5s…")
                self._connected.clear()
                await asyncio.sleep(5)

    async def _connect_and_listen(self):
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

    def stop(self):
        self._running = False
        self._connected.clear()
        logger.info("LiveFeed: stopped")
