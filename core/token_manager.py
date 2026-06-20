"""
Shared Token Manager
====================
Single source of truth for the Dhan access token.
Runs as a background task in main.py; backfill.py reads
the cached token file and never calls generate_token() itself.

Token lifecycle:
  - The rotating access token is a RUNTIME CACHE, not a provisioned secret.
    It lives only in dhan_token.json (written atomically). It is NOT written
    back into .env — that churn was non-atomic (corruption risk on a crash
    mid-write) and pointless: every reader uses read_current_token().
  - dhan-trader owns the refresh loop; backfill.py + dhan-api read the cache.
  - Durable secrets (client_id, TOTP secret, PIN, DB password) stay in .env
    (or SSM) and are never rewritten by this module.

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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Callable

import pyotp

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


def _atomic_write(path: Path, text: str):
    """Write via a temp file + os.replace so a crash mid-write can never leave
    a half-written (corrupt) file at `path`. os.replace is atomic on POSIX."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _write_token(token: str, exp_str: str, client_id: str):
    """Persist the rotating token to dhan_token.json — atomically, and ONLY
    there. We intentionally do not touch .env (see module docstring)."""
    _atomic_write(_TOKEN_FILE, json.dumps({
        "accessToken":  token,
        "expiryTime":   exp_str,
        "dhanClientId": client_id,
        "generatedAt":  datetime.now(timezone.utc).isoformat(),
    }, indent=2))


class MasterTokenManager:
    """
    Owns the Dhan token lifecycle for main.py.
    backfill.py reads tokens via read_current_token() — never generates.
    """

    def __init__(self):
        from config import get_config
        cfg = get_config()
        self.client_id   = cfg.dhan_client_id
        self.pin         = cfg.dhan_pin
        self.totp_secret = cfg.dhan_totp_secret
        self._token:  Optional[str]      = None
        self._expiry: Optional[datetime] = None
        self._callbacks: list[Callable]  = []
        # Serializes concurrent DH-901 auth-error handling so that multiple
        # in-flight requests racing on the same expired token only generate
        # one new token instead of each spawning an independent network call.
        # Initialized lazily on first use to avoid requiring an event loop at
        # construction time (Python 3.9 asyncio.Lock() binds to the running
        # loop at __init__, which may not exist yet outside a coroutine).
        self._refresh_lock: Optional[asyncio.Lock] = None

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
        from dhanhq import DhanLogin   # lazy — only token GENERATION needs the SDK
        totp = pyotp.TOTP(self.totp_secret).now()
        loop = asyncio.get_running_loop()

        def _call():
            dl = DhanLogin(self.client_id)
            return dl.generate_token(self.pin, totp)

        data    = await loop.run_in_executor(None, _call)
        token   = data.get("accessToken") or data.get("access_token")
        exp_str = data.get("expiryTime") or data.get("expiry_time", "")
        if not token:
            raise RuntimeError(
                "No accessToken in generate_token response (check PIN/TOTP config)"
            )

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
            from dhanhq import DhanLogin   # lazy — see _generate()
            loop = asyncio.get_running_loop()
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
        """Called by DhanClient on DH-901/806 — force refresh.

        Serialized via _refresh_lock so that concurrent DH-901 responses from
        multiple in-flight requests only trigger one actual token generation.
        Double-check pattern: if another caller already refreshed the token
        while we waited for the lock, return the fresh cached token immediately
        rather than generating a second new token.

        The lock is created lazily here (inside a running event loop) to stay
        compatible with Python 3.9, where asyncio.Lock() at __init__ time
        requires an active event loop.
        """
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        token_before_lock = self._token
        async with self._refresh_lock:
            # Double-check: another waiter may have already refreshed the token
            if self._token and self._token != token_before_lock and self.is_valid():
                logger.info("handle_auth_error: token already refreshed by concurrent caller")
                return self._token

            logger.warning("Auth error detected — forcing token refresh")
            renewed = await self._renew()
            return renewed or await self._generate()
