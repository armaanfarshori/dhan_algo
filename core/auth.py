"""
DhanHQ Auth Manager
====================
Handles the 24-hour access token lifecycle:
  - Loads cached token from dhan_token.json if still valid
  - Generates a fresh token via PIN + TOTP (pyotp) when expired
  - Renews the token 30 minutes before expiry (background task)
  - Auto-refreshes when DhanClient gets a DH-901 / DH-807 error

Required .env vars:
    DHAN_CLIENT_ID
    DHAN_PIN           (6-digit login PIN)
    DHAN_TOTP_SECRET   (base32 secret from Dhan authenticator setup)
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Callable
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

import pyotp
from dhanhq import DhanLogin

logger = logging.getLogger("dhan.auth")

TOKEN_FILE = Path(__file__).parent.parent / "dhan_token.json"
REFRESH_BEFORE_EXPIRY_MIN = 30


class DhanAuthManager:
    """
    Manages DhanHQ access token generation and refresh.
    Integrates with DhanClient via an on_token_refresh callback.
    """

    def __init__(
        self,
        client_id: str,
        pin: str,
        totp_secret: str,
        token_file: Path = TOKEN_FILE,
    ):
        self.client_id   = client_id
        self.pin         = pin
        self.totp_secret = totp_secret
        self.token_file  = token_file

        self._access_token: Optional[str]      = None
        self._expiry:       Optional[datetime]  = None
        self._refresh_callbacks: list[Callable] = []

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def access_token(self) -> str:
        if not self._access_token:
            raise RuntimeError("Token not loaded. Call load_or_generate() first.")
        return self._access_token

    def is_valid(self) -> bool:
        if not self._access_token or not self._expiry:
            return False
        return datetime.now(timezone.utc) < self._expiry

    def on_token_refresh(self, callback: Callable):
        """Register a callback invoked with the new token after every refresh."""
        self._refresh_callbacks.append(callback)

    async def load_or_generate(self) -> str:
        """Load a valid cached token or generate a fresh one."""
        if self.token_file.exists():
            try:
                data   = json.loads(self.token_file.read_text())
                expiry = self._parse_expiry(data.get("expiryTime", ""))
                if expiry and datetime.now(timezone.utc) < expiry:
                    self._access_token = data["accessToken"]
                    self._expiry       = expiry
                    remaining = (expiry - datetime.now(timezone.utc)).seconds // 60
                    logger.info(f"Loaded cached token — valid for {remaining} min")
                    return self._access_token
                else:
                    logger.info("Cached token expired — generating new one")
            except Exception as e:
                logger.warning(f"Token cache unreadable ({e}) — generating new one")

        return await self.generate()

    async def generate(self) -> str:
        """Generate a fresh token via PIN + TOTP and cache it."""
        logger.info("Generating new DhanHQ access token via PIN + TOTP…")
        totp = pyotp.TOTP(self.totp_secret).now()

        try:
            dhan_login = DhanLogin(self.client_id)
            data       = dhan_login.generate_token(self.pin, totp)
        except Exception as e:
            raise RuntimeError(f"Token generation failed: {e}") from e

        token    = data.get("accessToken") or data.get("access_token")
        exp_str  = data.get("expiryTime") or data.get("expiry_time", "")
        expiry   = self._parse_expiry(exp_str)

        if not token:
            raise RuntimeError(f"No accessToken in response: {data}")

        self._access_token = token
        self._expiry       = expiry

        self.token_file.write_text(json.dumps({
            "accessToken":  token,
            "expiryTime":   exp_str,
            "dhanClientId": self.client_id,
            "generatedAt":  datetime.now(timezone.utc).isoformat(),
        }, indent=2))

        logger.info(f"New token generated — expires {expiry or 'unknown'}")
        await self._notify_callbacks(token)
        return token

    async def run(self):
        """
        Background loop — checks token every 10 min and refreshes
        30 min before expiry so the platform never hits an expired token.
        """
        logger.info("Auth manager started — monitoring token expiry")
        while True:
            await asyncio.sleep(600)  # check every 10 minutes
            if not self._expiry:
                continue
            remaining = self._expiry - datetime.now(timezone.utc)
            if remaining < timedelta(minutes=REFRESH_BEFORE_EXPIRY_MIN):
                logger.warning(f"Token expiring in {remaining.seconds // 60} min — refreshing now")
                try:
                    await self._try_renew_then_generate()
                except Exception as e:
                    logger.error(f"Token refresh failed: {e}")

    async def handle_auth_error(self) -> str:
        """Called by DhanClient on DH-901 / DH-807 — force-generates a new token."""
        logger.warning("Auth error detected — forcing token regeneration")
        return await self._try_renew_then_generate()

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _try_renew_then_generate(self) -> str:
        """Try renew_token first (cheaper), fall back to full generate."""
        if self._access_token:
            try:
                dhan_login = DhanLogin(self.client_id)
                data       = dhan_login.renew_token(self._access_token)
                token      = data.get("accessToken") or data.get("access_token")
                exp_str    = data.get("expiryTime", "")
                if token:
                    self._access_token = token
                    self._expiry       = self._parse_expiry(exp_str)
                    self.token_file.write_text(json.dumps({
                        "accessToken":  token,
                        "expiryTime":   exp_str,
                        "dhanClientId": self.client_id,
                        "generatedAt":  datetime.now(timezone.utc).isoformat(),
                    }, indent=2))
                    logger.info("Token renewed successfully")
                    await self._notify_callbacks(token)
                    return token
            except Exception as e:
                logger.warning(f"renew_token failed ({e}) — falling back to generate")

        return await self.generate()

    async def _notify_callbacks(self, token: str):
        for cb in self._refresh_callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(token)
                else:
                    cb(token)
            except Exception as e:
                logger.error(f"Token refresh callback error: {e}")

    @staticmethod
    def _parse_expiry(exp_str: str) -> Optional[datetime]:
        """Parse expiry string to UTC-aware datetime.
        Dhan returns IST without timezone suffix — attach IST then convert to UTC."""
        if not exp_str:
            return None
        try:
            dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)   # Dhan returns IST naive strings
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None

    def summary(self) -> dict:
        remaining = None
        if self._expiry:
            rem = self._expiry - datetime.now(timezone.utc)
            remaining = max(rem.seconds // 60, 0)
        return {
            "token_valid":        self.is_valid(),
            "expiry":             self._expiry.isoformat() if self._expiry else None,
            "remaining_minutes":  remaining,
        }
