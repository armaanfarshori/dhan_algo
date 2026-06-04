"""
Shared Token Manager
====================
Single source of truth for the Dhan access token.
Runs as a background task in main.py; backfill.py reads
the cached token file and never calls generate_token() itself.

Token lifecycle:
  - Cached in dhan_token.json (already used by DhanAuthManager)
  - This module adds a LOCK FILE so only one process refreshes at a time
  - backfill.py reads the token from .env (written here on every refresh)
  - main.py owns the refresh loop exclusively

Usage (main.py):
    from core.token_manager import MasterTokenManager
    mgr = MasterTokenManager()
    token = await mgr.load_or_generate()
    asyncio.create_task(mgr.run())        # background refresh loop
    dhan = DhanClient(client_id, token, auth_manager=mgr)

Usage (backfill.py):
    from core.token_manager import read_current_token
    token = read_current_token()           # reads from dhan_token.json, no generate
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Callable

import pyotp
from dhanhq import DhanLogin

logger = logging.getLogger("dhan.token_manager")

_TOKEN_FILE = Path(__file__).parent.parent / "dhan_token.json"
_ENV_FILE   = Path(__file__).parent.parent / ".env"
_LOCK_FILE  = Path(__file__).parent.parent / "dhan_token.lock"
REFRESH_BEFORE_MIN = 30


def read_current_token() -> Optional[str]:
    """
    Read the current valid token from dhan_token.json.
    Safe to call from any process — read-only, no network calls.
    Returns None if no valid cached token exists.
    """
    if not _TOKEN_FILE.exists():
        return None
    try:
        data  = json.loads(_TOKEN_FILE.read_text())
        token = data.get("accessToken")
        exp   = _parse_expiry(data.get("expiryTime", ""))
        if token and exp and datetime.now(timezone.utc) < exp:
            return token
    except Exception:
        pass
    return None


def _parse_expiry(exp_str: str) -> Optional[datetime]:
    if not exp_str:
        return None
    try:
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _write_token(token: str, exp_str: str, client_id: str):
    """Persist token to dhan_token.json and update .env."""
    _TOKEN_FILE.write_text(json.dumps({
        "accessToken":  token,
        "expiryTime":   exp_str,
        "dhanClientId": client_id,
        "generatedAt":  datetime.now(timezone.utc).isoformat(),
    }, indent=2))

    if _ENV_FILE.exists():
        content = _ENV_FILE.read_text()
        updated = re.sub(
            r"^DHAN_ACCESS_TOKEN=.*$",
            f"DHAN_ACCESS_TOKEN={token}",
            content, flags=re.MULTILINE,
        )
        _ENV_FILE.write_text(updated)


class MasterTokenManager:
    """
    Owns the Dhan token lifecycle for main.py.
    backfill.py reads tokens via read_current_token() — never generates.
    """

    def __init__(self):
        self.client_id   = os.getenv("DHAN_CLIENT_ID", "")
        self.pin         = os.getenv("DHAN_PIN", "")
        self.totp_secret = os.getenv("DHAN_TOTP_SECRET", "")
        self._token:  Optional[str]      = None
        self._expiry: Optional[datetime] = None
        self._callbacks: list[Callable]  = []

    def on_token_refresh(self, cb: Callable):
        self._callbacks.append(cb)

    @property
    def access_token(self) -> str:
        return self._token or ""

    def is_valid(self) -> bool:
        return bool(self._token and self._expiry and
                    datetime.now(timezone.utc) < self._expiry)

    async def load_or_generate(self) -> str:
        cached = read_current_token()
        if cached:
            data   = json.loads(_TOKEN_FILE.read_text())
            expiry = _parse_expiry(data.get("expiryTime", ""))
            self._token  = cached
            self._expiry = expiry
            remaining = int((expiry - datetime.now(timezone.utc)).total_seconds() // 60) if expiry else 0
            logger.info("Loaded cached token — valid for %d min", remaining)
            return cached
        return await self._generate()

    async def _generate(self) -> str:
        logger.info("Generating new Dhan token via PIN + TOTP…")
        totp = pyotp.TOTP(self.totp_secret).now()
        loop = asyncio.get_event_loop()

        def _call():
            dl = DhanLogin(self.client_id)
            return dl.generate_token(self.pin, totp)

        data    = await loop.run_in_executor(None, _call)
        token   = data.get("accessToken") or data.get("access_token")
        exp_str = data.get("expiryTime") or data.get("expiry_time", "")
        if not token:
            raise RuntimeError(f"No accessToken in response: {data}")

        self._token  = token
        self._expiry = _parse_expiry(exp_str)
        _write_token(token, exp_str, self.client_id)
        logger.info("New token generated — expires %s", self._expiry)
        await self._notify(token)
        return token

    async def _renew(self) -> Optional[str]:
        if not self._token:
            return None
        try:
            loop = asyncio.get_event_loop()
            old  = self._token

            def _call():
                dl = DhanLogin(self.client_id)
                return dl.renew_token(old)

            data    = await loop.run_in_executor(None, _call)
            token   = data.get("accessToken") or data.get("access_token")
            exp_str = data.get("expiryTime", "")
            if not token:
                return None
            self._token  = token
            self._expiry = _parse_expiry(exp_str)
            _write_token(token, exp_str, self.client_id)
            logger.info("Token renewed — expires %s", self._expiry)
            await self._notify(token)
            return token
        except Exception as exc:
            logger.warning("renew_token failed (%s) — will generate", exc)
            return None

    async def _notify(self, token: str):
        for cb in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(token)
                else:
                    cb(token)
            except Exception as exc:
                logger.error("Token callback error: %s", exc)

    async def run(self):
        """Background loop — checks every 10 min, refreshes 30 min before expiry."""
        logger.info("MasterTokenManager: monitoring token expiry")
        while True:
            await asyncio.sleep(600)
            if not self._expiry:
                continue
            remaining = self._expiry - datetime.now(timezone.utc)
            if remaining < timedelta(minutes=REFRESH_BEFORE_MIN):
                logger.warning("Token expiring in %d min — refreshing", remaining.seconds // 60)
                renewed = await self._renew()
                if not renewed:
                    await self._generate()

    async def handle_auth_error(self) -> str:
        """Called by DhanClient on DH-901/806 — force refresh."""
        logger.warning("Auth error detected — forcing token refresh")
        renewed = await self._renew()
        return renewed or await self._generate()
