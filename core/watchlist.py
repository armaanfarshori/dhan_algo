"""
NSE Top Movers Watchlist
========================
Fetches the top 15 most-active / gainers from NSE daily and maps
each symbol to a Dhan security_id using the cached instrument master CSV.

NSE requires a session cookie obtained from a prior GET to nseindia.com.
We handle that transparently with a short-lived aiohttp session.

Refresh strategy:
  - On startup if no cache or cache is stale (>6h)
  - Manually via POST /api/watchlist/refresh
  - Auto at 09:05 IST on trading days via background task

Usage:
    wl = await WatchlistManager.build()
    stocks = wl.get()          # list[WatchlistStock]
    await wl.refresh()         # force refresh from NSE
"""

import asyncio
import csv
import io
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import aiohttp

logger = logging.getLogger("dhan.watchlist")

IST         = ZoneInfo("Asia/Kolkata")
CACHE_DIR   = Path(__file__).parent.parent / ".cache"
CACHE_FILE  = CACHE_DIR / "watchlist.json"
MASTER_CSV  = CACHE_DIR / "scrip_master.csv"
CACHE_TTL_H = 6

NSE_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}

# NSE unofficial JSON endpoints (stable, widely used)
NSE_ENDPOINTS = {
    "volume_gainers": "https://www.nseindia.com/api/live-analysis-volume-gainers",
    "gainers":        "https://www.nseindia.com/api/live-analysis-variations?index=gainers",
    "most_active":    "https://www.nseindia.com/api/live-analysis-volume-spurts",
}


@dataclass
class WatchlistStock:
    symbol:      str
    security_id: str
    name:        str       = ""
    ltp:         float     = 0.0
    change_pct:  float     = 0.0
    volume:      int       = 0
    source:      str       = ""   # "volume_gainers" | "gainers" | "most_active"
    lot_size:    int       = 1
    signal:      str       = ""   # filled by scanner: "BUY" | "SELL" | ""
    signal_reason: str     = ""


class WatchlistManager:
    def __init__(self):
        self._stocks: List[WatchlistStock] = []
        self._last_refresh: Optional[float] = None
        self._sym_to_sid: dict = {}   # symbol → security_id from master CSV
        self._sym_to_meta: dict = {}  # symbol → {name, lot_size}

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    async def build(cls) -> "WatchlistManager":
        wl = cls()
        wl._load_symbol_index()

        # Try cache first
        if CACHE_FILE.exists():
            age_h = (time.time() - CACHE_FILE.stat().st_mtime) / 3600
            if age_h < CACHE_TTL_H:
                try:
                    data = json.loads(CACHE_FILE.read_text())
                    wl._stocks = [WatchlistStock(**s) for s in data]
                    wl._last_refresh = CACHE_FILE.stat().st_mtime
                    logger.info(f"Watchlist loaded from cache ({len(wl._stocks)} stocks, age {age_h:.1f}h)")
                    return wl
                except Exception as e:
                    logger.warning(f"Cache read failed: {e}")

        await wl.refresh()
        return wl

    # ── Symbol → Dhan security_id index ─────────────────────────────────────

    def _load_symbol_index(self):
        if not MASTER_CSV.exists():
            logger.warning("Scrip master CSV not cached yet — run the platform first")
            return

        with open(MASTER_CSV, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("SEM_EXM_EXCH_ID") != "NSE":
                    continue
                if row.get("SEM_INSTRUMENT_NAME") != "EQUITY":
                    continue
                if row.get("SEM_SERIES") != "EQ":
                    continue
                sym = row.get("SEM_TRADING_SYMBOL", "").strip()
                sid = row.get("SEM_SMST_SECURITY_ID", "").strip()
                if sym and sid:
                    self._sym_to_sid[sym] = sid
                    self._sym_to_meta[sym] = {
                        "name":     row.get("SM_SYMBOL_NAME", sym).strip(),
                        "lot_size": max(1, int(float(row.get("SEM_LOT_UNITS", "1") or "1"))),
                    }

        logger.info(f"Symbol index built: {len(self._sym_to_sid)} NSE EQ instruments")

    # ── NSE fetch ─────────────────────────────────────────────────────────────

    async def refresh(self, top_n: int = 15):
        logger.info("Refreshing watchlist from NSE…")
        CACHE_DIR.mkdir(exist_ok=True)

        stocks_raw: List[dict] = []

        try:
            async with aiohttp.ClientSession(headers=NSE_HEADERS,
                                              timeout=aiohttp.ClientTimeout(total=15)) as session:
                # Warm up the session with a cookie
                try:
                    await session.get("https://www.nseindia.com", ssl=False)
                    await asyncio.sleep(0.5)
                except Exception:
                    pass

                for source, url in NSE_ENDPOINTS.items():
                    try:
                        async with session.get(url, ssl=False) as resp:
                            if resp.status != 200:
                                logger.warning(f"NSE {source} returned {resp.status}")
                                continue
                            data = await resp.json(content_type=None)
                            items = data.get("data", data) if isinstance(data, dict) else data
                            if isinstance(items, list):
                                for item in items[:top_n]:
                                    stocks_raw.append({**item, "source": source})
                    except Exception as e:
                        logger.warning(f"NSE {source} fetch error: {e}")
                    await asyncio.sleep(0.3)

        except Exception as e:
            logger.error(f"NSE session error: {e}")

        if not stocks_raw:
            logger.warning("NSE returned no data — using fallback NIFTY 50 heavy-weights")
            stocks_raw = self._fallback_stocks()

        # Deduplicate, map to security_id, rank by volume/change
        seen: set = set()
        result: List[WatchlistStock] = []

        for item in stocks_raw:
            # NSE field names vary by endpoint
            sym = (item.get("symbol") or item.get("Symbol") or "").strip().upper()
            if not sym or sym in seen:
                continue
            sid = self._sym_to_sid.get(sym)
            if not sid:
                continue
            seen.add(sym)
            meta = self._sym_to_meta.get(sym, {})
            result.append(WatchlistStock(
                symbol      = sym,
                security_id = sid,
                name        = meta.get("name", sym),
                ltp         = float(item.get("ltp") or item.get("LTP") or item.get("lastPrice") or 0),
                change_pct  = float(item.get("perChange") or item.get("changePct") or item.get("pChange") or 0),
                volume      = int(float(item.get("totalTradedVolume") or item.get("tradedVolume") or item.get("volume") or 0)),
                source      = item.get("source", ""),
                lot_size    = meta.get("lot_size", 1),
            ))

        # Sort: volume DESC, take top_n
        result.sort(key=lambda s: s.volume, reverse=True)
        self._stocks = result[:top_n]
        self._last_refresh = time.time()

        # Persist cache
        CACHE_FILE.write_text(json.dumps(
            [s.__dict__ for s in self._stocks], indent=2
        ))
        logger.info(f"Watchlist refreshed: {len(self._stocks)} stocks")

    # ── Fallback: guaranteed liquid names ─────────────────────────────────────

    def _fallback_stocks(self) -> List[dict]:
        """Use if NSE API is unavailable. These are consistently high-volume."""
        names = [
            "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY",
            "HINDUNILVR", "KOTAKBANK", "LT", "AXISBANK", "BAJFINANCE",
            "SBIN", "MARUTI", "TITAN", "SUNPHARMA", "WIPRO",
        ]
        return [{"symbol": s, "source": "fallback"} for s in names]

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self) -> List[WatchlistStock]:
        return list(self._stocks)

    def get_security_ids(self) -> List[str]:
        return [s.security_id for s in self._stocks]

    def summary(self) -> dict:
        return {
            "count":        len(self._stocks),
            "last_refresh": datetime.fromtimestamp(self._last_refresh, IST).isoformat() if self._last_refresh else None,
            "stocks":       [s.__dict__ for s in self._stocks],
        }

    def update_signals(self, signals: dict):
        """signals = {security_id: {action, reason}}"""
        for s in self._stocks:
            if s.security_id in signals:
                s.signal        = signals[s.security_id].get("action", "")
                s.signal_reason = signals[s.security_id].get("reason", "")

    # ── Background refresh task ───────────────────────────────────────────────

    async def run(self):
        """Refresh once at 09:05 IST on weekdays, then sleep until next day."""
        logger.info("Watchlist manager started")
        while True:
            now = datetime.now(IST)
            wd  = now.weekday()
            t   = now.time()

            from datetime import time as dtime
            market_open = dtime(9, 5)

            if wd < 5 and t >= market_open:
                # Already past 09:05 today — refresh now if stale
                if not self._last_refresh or (time.time() - self._last_refresh) > CACHE_TTL_H * 3600:
                    await self.refresh()

            # Sleep 30 min between checks
            await asyncio.sleep(1800)
