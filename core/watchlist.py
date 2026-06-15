"""
Static fallback watchlist (liquid NIFTY-50 names).
==================================================
This is ONLY the emergency fallback. The live watchlist is chosen by the
ATR% screener (core/nse_screener.get_top_volatile) over our own bars
hypertable — that is the source of truth.

History: this module used to scrape NSE's unofficial website endpoints
(live-analysis-volume-spurts etc.) for "most active" / gainers. Dhan has
no most-traded/movers API, and those scraped endpoints are undocumented
and change without notice (most_active started 404-ing 2026-06-15). Since
the DB screener supplanted this entirely, the scraping was removed: there
is nothing to scrape for, and any "most traded today" signal we want we
compute from our own volume data (reproducible + backtestable).

Usage:
    wl = await WatchlistManager.build()
    stocks = wl.get()          # list[WatchlistStock] — liquid fallback names
"""

import csv
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("dhan.watchlist")

IST         = ZoneInfo("Asia/Kolkata")
CACHE_DIR   = Path(__file__).parent.parent / ".cache"
CACHE_FILE  = CACHE_DIR / "watchlist.json"
MASTER_CSV  = CACHE_DIR / "scrip_master.csv"
CACHE_TTL_H = 6


@dataclass
class WatchlistStock:
    symbol:      str
    security_id: str
    name:        str       = ""
    ltp:         float     = 0.0
    change_pct:  float     = 0.0
    volume:      int       = 0
    source:      str       = ""   # "nifty50_static" (fallback origin)
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

    # ── Build the static fallback list ─────────────────────────────────────────

    async def refresh(self, top_n: int = 30):
        """Populate the fallback watchlist from the static liquid-names list,
        mapped to Dhan security_ids. No external calls — this list only ever
        feeds the genuine 'screener returned empty' fallback path."""
        CACHE_DIR.mkdir(exist_ok=True)
        result: List[WatchlistStock] = []
        for item in self._fallback_stocks():
            sym = item["symbol"].strip().upper()
            sid = self._sym_to_sid.get(sym)
            if not sid:
                continue
            meta = self._sym_to_meta.get(sym, {})
            result.append(WatchlistStock(
                symbol=sym, security_id=sid, name=meta.get("name", sym),
                source="nifty50_static", lot_size=meta.get("lot_size", 1)))

        self._stocks = result[:top_n]
        self._last_refresh = time.time()
        CACHE_FILE.write_text(json.dumps([s.__dict__ for s in self._stocks], indent=2))
        logger.info("Fallback watchlist built: %d liquid names (screener is the "
                    "live source of truth)", len(self._stocks))

    # ── Fallback: guaranteed liquid names ─────────────────────────────────────

    def _fallback_stocks(self) -> List[dict]:
        """
        NIFTY 50 liquid F&O stocks — used when NSE API unavailable.
        These have guaranteed liquidity, active F&O, and cleaner breakouts
        than dynamic top-movers which can include illiquid penny stocks.
        """
        names = [
            # Large caps — always liquid
            "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY",
            "HINDUNILVR", "KOTAKBANK", "LT", "AXISBANK", "BAJFINANCE",
            "SBIN", "MARUTI", "TITAN", "SUNPHARMA", "WIPRO",
            # Mid-large — active F&O
            "ADANIENT", "ADANIPORTS", "ASIANPAINT", "BAJAJFINSV", "BHARTIARTL",
            "BPCL", "COALINDIA", "DIVISLAB", "DRREDDY", "EICHERMOT",
            "GRASIM", "HCLTECH", "HEROMOTOCO", "HINDALCO", "INDUSINDBK",
            "ITC", "JSWSTEEL", "M&M", "NESTLEIND", "NTPC",
        ]
        return [{"symbol": s, "source": "nifty50_static"} for s in names]

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
