"""
Kronos Live Scanner
==================
Background loop that continuously:
  1. Runs the ATR screener → top-N volatile securities (with names)
  2. Scores each with Kronos → BUY/SELL/HOLD + confidence
  3. Writes signals to the DB + maintains in-memory live state
  4. Tracks the day's screened universe (persisted to disk)

The dashboard reads scanner state via /api/kronos/live — instant, no
DB round-trip per poll. Names are resolved from the instruments table.

Usage (main.py):
    scanner = KronosScanner(kronos_engine, db_backend)
    asyncio.create_task(scanner.run())
    app["kronos_scanner"] = scanner
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, date, time as dtime
from pathlib import Path
from typing import Optional

import pytz

logger = logging.getLogger("dhan.kronos_scanner")
IST = pytz.timezone("Asia/Kolkata")

_DAILY_LIST_FILE = Path("/tmp/kronos_screened_today.json")
_SCAN_INTERVAL   = 180   # seconds between full scans (3 min)
_WATCHLIST_N     = 5


def _is_market_hours() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 0) <= t <= dtime(15, 35)


def _resolve_names(security_ids: list[str]) -> dict[str, dict]:
    """Map security_id → {ticker, name} from instruments table."""
    from db import get_session
    from sqlalchemy import text
    if not security_ids:
        return {}
    with get_session() as s:
        rows = s.execute(text("""
            SELECT security_id, ticker, name FROM instruments
            WHERE security_id = ANY(:ids)
        """), {"ids": security_ids}).fetchall()
    return {r[0]: {"ticker": r[1] or r[0], "name": r[2] or ""} for r in rows}


class KronosScanner:
    def __init__(self, kronos_engine, db_backend=None, n: int = _WATCHLIST_N):
        self.kronos = kronos_engine
        self.db     = db_backend
        self.n      = n

        # Live state (read by /api/kronos/live)
        self.last_scan_ts: Optional[str] = None
        self.scan_count   = 0
        self.scanning     = False
        self.results: list[dict] = []          # current scan: scored securities
        self.screened_today: dict = self._load_daily()  # {date, securities:[...]}

    # ── Daily screened list persistence ───────────────────────────────────────

    def _load_daily(self) -> dict:
        try:
            if _DAILY_LIST_FILE.exists():
                d = json.loads(_DAILY_LIST_FILE.read_text())
                if d.get("date") == str(date.today()):
                    return d
        except Exception:
            pass
        return {"date": str(date.today()), "securities": {}}

    def _save_daily(self):
        try:
            _DAILY_LIST_FILE.write_text(json.dumps(self.screened_today))
        except Exception as exc:
            logger.warning("Could not save daily screened list: %s", exc)

    def _record_screened(self, candidates: list[dict], names: dict):
        """Add securities to the day's screened set (union over the day)."""
        today = str(date.today())
        if self.screened_today.get("date") != today:
            self.screened_today = {"date": today, "securities": {}}
        for c in candidates:
            sid = c["security_id"]
            meta = names.get(sid, {})
            if sid not in self.screened_today["securities"]:
                self.screened_today["securities"][sid] = {
                    "ticker":     meta.get("ticker", sid),
                    "name":       meta.get("name", ""),
                    "first_seen": datetime.now(IST).strftime("%H:%M"),
                    "atr_reason": c.get("reason", ""),
                    "times_seen": 1,
                }
            else:
                self.screened_today["securities"][sid]["times_seen"] += 1
        self._save_daily()

    # ── Main scan loop ─────────────────────────────────────────────────────────

    async def run(self):
        logger.info("KronosScanner started — scans every %ds during market hours", _SCAN_INTERVAL)
        # Pre-load the model once
        try:
            await self.kronos.load()
            logger.info("KronosScanner: model ready")
        except Exception as exc:
            logger.warning("KronosScanner: model load failed (%s) — will retry on scan", exc)

        while True:
            try:
                if _is_market_hours() or self.scan_count == 0:
                    await self._scan_once()
            except Exception as exc:
                logger.error("KronosScanner scan error: %s", exc)
            await asyncio.sleep(_SCAN_INTERVAL)

    async def _scan_once(self):
        from core.nse_screener import get_top_volatile
        loop = asyncio.get_running_loop()

        self.scanning = True
        # 1. Screener (sync — run in executor)
        candidates = await loop.run_in_executor(
            None, lambda: get_top_volatile(n=self.n, min_avg_volume=10_000)
        )
        if not candidates:
            self.scanning = False
            return

        sids  = [c["security_id"] for c in candidates]
        names = await loop.run_in_executor(None, _resolve_names, sids)
        self._record_screened(candidates, names)

        # 2. Score each with Kronos
        scored = []
        for c in candidates:
            sid = c["security_id"]
            try:
                r = await self.kronos.score_from_db(sid, "NSE_EQ")
                meta = names.get(sid, {})
                entry = {
                    "security_id": sid,
                    "ticker":      meta.get("ticker", sid),
                    "name":        meta.get("name", ""),
                    "side":        r["side"],
                    "confidence":  r["confidence"],
                    "score":       r["score"],
                    "forecast_return": r.get("forecasted_return", 0),
                    "current_price":   r.get("current_price", 0),
                    "atr_reason":  c.get("reason", ""),
                    "ts":          datetime.now(timezone.utc).isoformat(),
                }
                scored.append(entry)
                # Write to signals table
                if self.db:
                    await self.db.log_signal(
                        security_id=sid, side=r["side"], strategy="kronos_scan",
                        score=r["score"], confidence=r["confidence"],
                        features={"forecast_return": r.get("forecasted_return"),
                                  "ticker": meta.get("ticker")},
                    )
            except Exception as exc:
                logger.warning("Kronos score failed for %s: %s", sid, exc)

        # Sort: BUY first, then SELL, then HOLD, by confidence
        order = {"BUY": 0, "SELL": 1, "HOLD": 2}
        scored.sort(key=lambda x: (order.get(x["side"], 3), -x["confidence"]))

        self.results      = scored
        self.last_scan_ts = datetime.now(timezone.utc).isoformat()
        self.scan_count  += 1
        self.scanning     = False
        logger.info("Kronos scan #%d: %d scored (%d BUY, %d SELL, %d HOLD)",
                    self.scan_count, len(scored),
                    sum(1 for x in scored if x["side"]=="BUY"),
                    sum(1 for x in scored if x["side"]=="SELL"),
                    sum(1 for x in scored if x["side"]=="HOLD"))

    # ── State for API ──────────────────────────────────────────────────────────

    def get_state(self) -> dict:
        return {
            "ok":             True,
            "scanning":       self.scanning,
            "scan_count":     self.scan_count,
            "last_scan":      self.last_scan_ts,
            "results":        self.results,
            "screened_today": self.screened_today,
        }
