"""
Dhan Instrument Master
======================
Downloads and caches the Dhan scrip master CSV daily.
Provides ATM option lookup by underlying price + expiry date.

Usage:
    master = await InstrumentMaster.load()
    expiries = master.nifty_expiries()
    ids = master.find_atm(underlying_price=24500, expiry="2026-05-08", strike_step=50)
    # ids = {"CE": {"sid": "12345", "strike": 24500, "lot_size": 75}, "PE": {...}}
"""

import asyncio
import csv
import io
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp

logger = logging.getLogger("dhan.instruments")

SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
CACHE_DIR        = Path(__file__).parent.parent / ".cache"
CACHE_FILE       = CACHE_DIR / "scrip_master.csv"
CACHE_TTL_HOURS  = 6   # refresh mid-session if stale


@dataclass
class OptionContract:
    security_id: str
    trading_symbol: str
    strike: float
    option_type: str   # CE or PE
    expiry: str        # YYYY-MM-DD
    lot_size: int
    expiry_flag: str   # W=weekly, M=monthly


class InstrumentMaster:
    """
    In-memory index of NIFTY option contracts built from the Dhan scrip master.
    Keyed by (expiry, strike, option_type) for O(1) ATM lookup.
    """

    def __init__(self, contracts: List[OptionContract]):
        self._contracts = contracts

        # Index keyed by (prefix, expiry, strike, option_type) to avoid
        # collisions between indices sharing the same strike (e.g. BANKNIFTY
        # and NIFTYNXT50 both had a 56000 CE — flat key caused wrong lookup).
        self._idx: Dict[tuple, OptionContract] = {}
        for c in contracts:
            prefix = next(
                (p for p in self._VALID_PREFIXES if c.trading_symbol.startswith(p)),
                ""
            )
            key = (prefix, c.expiry, c.strike, c.option_type)
            self._idx[key] = c

        # Sorted unique expiries
        self._expiries: List[str] = sorted({c.expiry for c in contracts})

    # ── Factory ──────────────────────────────────────────────────────────────

    # All tradeable index option configs
    # underlying_id: IDX_I security ID verified via live API
    INDEX_CONFIGS = {
        "NIFTY": {
            "underlying_id":  "13", "underlying_segment": "IDX_I",
            "option_segment": "NSE_FNO", "option_prefix": "NIFTY-",
            "strike_step": 50,  "lot_size": 65,
        },
        "BANKNIFTY": {
            "underlying_id":  "25", "underlying_segment": "IDX_I",
            "option_segment": "NSE_FNO", "option_prefix": "BANKNIFTY-",
            "strike_step": 100, "lot_size": 30,
        },
        "SENSEX": {
            "underlying_id":  "51", "underlying_segment": "IDX_I",
            "option_segment": "BSE_FNO", "option_prefix": "SENSEX",
            "strike_step": 100, "lot_size": 20,
        },
        "FINNIFTY": {
            "underlying_id":  "27", "underlying_segment": "IDX_I",
            "option_segment": "NSE_FNO", "option_prefix": "FINNIFTY-",
            "strike_step": 50,  "lot_size": 60,
        },
        "NIFTYNXT50": {
            "underlying_id":  "38", "underlying_segment": "IDX_I",
            "option_segment": "NSE_FNO", "option_prefix": "NIFTYNXT50-",
            "strike_step": 100, "lot_size": 25,
        },
        "MIDCPNIFTY": {
            "underlying_id":  "93", "underlying_segment": "IDX_I",
            "option_segment": "NSE_FNO", "option_prefix": "MIDCPNIFTY-",
            "strike_step": 25,  "lot_size": 120,
        },
    }

    @classmethod
    async def load(cls) -> "InstrumentMaster":
        """Download (or use cache) and parse the scrip master."""
        csv_text = await cls._fetch_csv()
        contracts = cls._parse(csv_text)
        logger.info(f"Instrument master loaded: {len(contracts)} index option contracts "
                    f"(NIFTY + BANKNIFTY + SENSEX)")
        return cls(contracts)

    @classmethod
    async def _fetch_csv(cls) -> str:
        CACHE_DIR.mkdir(exist_ok=True)

        # Use cache if fresh enough
        if CACHE_FILE.exists():
            age_hours = (time.time() - CACHE_FILE.stat().st_mtime) / 3600
            if age_hours < CACHE_TTL_HOURS:
                logger.info(f"Using cached scrip master (age {age_hours:.1f}h)")
                return CACHE_FILE.read_text(encoding="utf-8", errors="replace")

        logger.info("Downloading scrip master from Dhan…")
        async with aiohttp.ClientSession() as session:
            async with session.get(SCRIP_MASTER_URL, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                text = await resp.text(encoding="utf-8", errors="replace")

        CACHE_FILE.write_text(text, encoding="utf-8")
        logger.info(f"Scrip master cached ({len(text)//1024} KB)")
        return text

    # All option symbol prefixes we want to load
    _VALID_PREFIXES = (
        "NIFTY-", "BANKNIFTY-", "SENSEX", "FINNIFTY-",
        "NIFTYNXT50-", "MIDCPNIFTY-",
    )

    @classmethod
    def _parse(cls, csv_text: str) -> List[OptionContract]:
        reader = csv.DictReader(io.StringIO(csv_text))
        result = []
        today  = date.today().isoformat()

        for row in reader:
            sym = row.get("SEM_TRADING_SYMBOL", "")
            opt = row.get("SEM_OPTION_TYPE", "")

            # All index options (NIFTY, BANKNIFTY, SENSEX, FINNIFTY, etc.)
            if not any(sym.startswith(p) for p in cls._VALID_PREFIXES):
                continue
            if opt not in ("CE", "PE"):
                continue
            if row.get("SEM_INSTRUMENT_NAME") != "OPTIDX":
                continue

            expiry_raw = row.get("SEM_EXPIRY_DATE", "")[:10]
            if expiry_raw < today:
                continue

            try:
                result.append(OptionContract(
                    security_id    = row["SEM_SMST_SECURITY_ID"].strip(),
                    trading_symbol = sym,
                    strike         = float(row["SEM_STRIKE_PRICE"]),
                    option_type    = opt,
                    expiry         = expiry_raw,
                    lot_size       = int(float(row.get("SEM_LOT_UNITS", "65"))),
                    expiry_flag    = row.get("SEM_EXPIRY_FLAG", ""),
                ))
            except (ValueError, KeyError):
                continue

        return result

    # ── Queries ───────────────────────────────────────────────────────────────

    def nifty_expiries(self) -> List[str]:
        """Sorted list of all upcoming NIFTY option expiry dates."""
        return list(self._expiries)

    def nearest_expiry(self) -> Optional[str]:
        """The next upcoming expiry date (today or later)."""
        today = date.today().isoformat()
        for exp in self._expiries:
            if exp >= today:
                return exp
        return None

    def weekly_expiries(self) -> List[str]:
        weeks = {c.expiry for c in self._contracts if c.expiry_flag == "W"}
        return sorted(weeks)

    def monthly_expiries(self) -> List[str]:
        months = {c.expiry for c in self._contracts if c.expiry_flag == "M"}
        return sorted(months)

    def find_atm(
        self,
        underlying_price: float,
        expiry: str,
        strike_step: int = 50,
        prefix: str = "",
    ) -> Dict[str, OptionContract]:
        """
        Find ATM CE + PE. `prefix` scopes the lookup to a single index
        (e.g. "NIFTY-") preventing cross-index strike collisions.
        """
        atm_strike = round(underlying_price / strike_step) * strike_step

        for offset in range(0, 5):
            for direction in ([0] if offset == 0 else [offset, -offset]):
                strike = atm_strike + direction * strike_step
                ce = self._idx.get((prefix, expiry, float(strike), "CE"))
                pe = self._idx.get((prefix, expiry, float(strike), "PE"))
                if ce and pe:
                    logger.info(f"ATM strike {strike} | CE {ce.security_id} | PE {pe.security_id} | expiry {expiry}")
                    return {"CE": ce, "PE": pe}

        logger.warning(f"No ATM contracts found @ {underlying_price:.0f} expiry {expiry} prefix={prefix!r}")
        return {}

    def get_contract(self, security_id: str) -> Optional[OptionContract]:
        for c in self._contracts:
            if c.security_id == security_id:
                return c
        return None

    def find_atm_for_index(
        self,
        index_name: str,
        underlying_price: float,
        expiry: str,
    ) -> Dict[str, "OptionContract"]:
        """Find ATM CE + PE for a named index using its specific prefix to avoid
        cross-index strike collisions (e.g. BANKNIFTY vs NIFTYNXT50 at 56000)."""
        cfg = self.INDEX_CONFIGS.get(index_name.upper())
        if not cfg:
            logger.warning(f"Unknown index: {index_name}")
            return {}
        return self.find_atm(
            underlying_price, expiry,
            cfg["strike_step"],
            prefix=cfg["option_prefix"],
        )

    def index_expiries(self, index_name: str) -> List[str]:
        """Return expiries available for a specific index."""
        prefix = self.INDEX_CONFIGS.get(index_name.upper(), {}).get("option_prefix", "")
        if not prefix:
            return self._expiries
        return sorted({
            c.expiry for c in self._contracts
            if c.trading_symbol.startswith(prefix)
        })

    def nearest_expiry_for_index(self, index_name: str) -> Optional[str]:
        expiries = self.index_expiries(index_name)
        today = date.today().isoformat()
        for e in expiries:
            if e >= today:
                return e
        return None

    def strikes_for_expiry(self, expiry: str) -> List[float]:
        return sorted({c.strike for c in self._contracts if c.expiry == expiry})

    @classmethod
    def search_instruments(cls, query: str, segment: str, max_results: int = 20) -> list:
        """
        Search equity (NSE_EQ) or commodity (MCX) instruments from the cached CSV.
        Runs synchronously — call via run_in_executor from async handlers.
        """
        if not CACHE_FILE.exists():
            return []

        q     = query.strip().upper()
        today = date.today().isoformat()
        results: dict = {}  # keyed by security_id to deduplicate

        with open(CACHE_FILE, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                exch       = row.get("SEM_EXM_EXCH_ID", "")
                instrument = row.get("SEM_INSTRUMENT_NAME", "")
                series     = row.get("SEM_SERIES", "")
                sym        = row.get("SM_SYMBOL_NAME", "").upper()
                trading    = row.get("SEM_TRADING_SYMBOL", "").upper()
                sid        = row.get("SEM_SMST_SECURITY_ID", "").strip()

                if segment == "NSE_EQ":
                    if exch != "NSE" or instrument != "EQUITY" or series != "EQ":
                        continue
                    if q not in sym and q not in trading:
                        continue

                elif segment == "MCX":
                    if exch != "MCX" or instrument != "FUTCOM":
                        continue
                    expiry_raw = row.get("SEM_EXPIRY_DATE", "")[:10]
                    if expiry_raw < today:
                        continue
                    if q not in sym and q not in trading:
                        continue
                    # Keep only nearest expiry per commodity symbol
                    if sym in results:
                        continue
                else:
                    continue

                if not sid:
                    continue

                try:
                    results[sid] = {
                        "security_id": sid,
                        "symbol":      row.get("SEM_TRADING_SYMBOL", "").strip(),
                        "name":        row.get("SM_SYMBOL_NAME", "").strip(),
                        "exchange":    exch,
                        "lot_size":    int(float(row.get("SEM_LOT_UNITS", "1") or "1")),
                        "segment":     segment,
                        "instrument":  instrument,
                    }
                except (ValueError, KeyError):
                    continue

                if len(results) >= max_results:
                    break

        return list(results.values())
