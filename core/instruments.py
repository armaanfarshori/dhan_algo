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
        # Index: (expiry, strike, option_type) → OptionContract
        self._idx: Dict[tuple, OptionContract] = {
            (c.expiry, c.strike, c.option_type): c
            for c in contracts
        }
        # Sorted unique expiries
        self._expiries: List[str] = sorted({c.expiry for c in contracts})

    # ── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    async def load(cls) -> "InstrumentMaster":
        """Download (or use cache) and parse the scrip master."""
        csv_text = await cls._fetch_csv()
        contracts = cls._parse(csv_text)
        logger.info(f"Instrument master loaded: {len(contracts)} NIFTY option contracts")
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

    @staticmethod
    def _parse(csv_text: str) -> List[OptionContract]:
        reader  = csv.DictReader(io.StringIO(csv_text))
        result  = []
        today   = date.today().isoformat()

        for row in reader:
            sym = row.get("SEM_TRADING_SYMBOL", "")
            opt = row.get("SEM_OPTION_TYPE", "")

            # Pure NIFTY index options only (excludes BANKNIFTY, NIFTYMID, etc.)
            if not sym.startswith("NIFTY-"):
                continue
            if opt not in ("CE", "PE"):
                continue
            if row.get("SEM_INSTRUMENT_NAME") != "OPTIDX":
                continue

            expiry_raw = row.get("SEM_EXPIRY_DATE", "")[:10]
            if expiry_raw < today:
                continue  # skip expired contracts

            try:
                contract = OptionContract(
                    security_id    = row["SEM_SMST_SECURITY_ID"].strip(),
                    trading_symbol = sym,
                    strike         = float(row["SEM_STRIKE_PRICE"]),
                    option_type    = opt,
                    expiry         = expiry_raw,
                    lot_size       = int(float(row.get("SEM_LOT_UNITS", "75"))),
                    expiry_flag    = row.get("SEM_EXPIRY_FLAG", ""),
                )
                result.append(contract)
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
    ) -> Dict[str, OptionContract]:
        """
        Find ATM call and put security IDs for a given underlying price and expiry.

        Returns {"CE": OptionContract, "PE": OptionContract}
        or {} if not found.

        Tries exact ATM first, then searches ±N strikes if ATM is missing from master.
        """
        atm_strike = round(underlying_price / strike_step) * strike_step

        for offset in range(0, 5):
            for direction in ([0] if offset == 0 else [offset, -offset]):
                strike = atm_strike + direction * strike_step
                ce = self._idx.get((expiry, float(strike), "CE"))
                pe = self._idx.get((expiry, float(strike), "PE"))
                if ce and pe:
                    logger.info(f"ATM strike {strike} | CE {ce.security_id} | PE {pe.security_id} | expiry {expiry}")
                    return {"CE": ce, "PE": pe}

        logger.warning(f"No ATM contracts found for NIFTY @ {underlying_price:.0f} expiry {expiry}")
        return {}

    def get_contract(self, security_id: str) -> Optional[OptionContract]:
        for c in self._contracts:
            if c.security_id == security_id:
                return c
        return None

    def strikes_for_expiry(self, expiry: str) -> List[float]:
        return sorted({c.strike for c in self._contracts if c.expiry == expiry})
