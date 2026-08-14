#!/usr/bin/env python3
"""
Static-egress identity check — proves the Dhan order path still leaves the
network from the IP Dhan has whitelisted.

Order placement is the one Dhan call gated on a fixed public IP (see
config.dhan_proxy_url). The failure mode this guards is silent: if the egress
proxy dies, hangs, or the cloud VM's public IP is reassigned, aiohttp does NOT
fall back to a direct connection — but a *misconfigured* proxy URL, a restarted
tinyproxy bound elsewhere, or a re-IP'd VM all leave the platform looking
healthy right up to the moment the first order of the session is rejected. So
the identity is verified out-of-band, before the open, and shouted about via
Telegram when it is wrong.

Exit codes (cron-meaningful):
  0  egress IP matches the whitelist, or the proxy is not configured (skip)
  1  proxy reachable but egressing from the WRONG IP  → orders will be rejected
  2  proxy unreachable                                → orders cannot be placed

Usage:
  python scripts/egress_check.py
Cron (repo venv, e.g. 09:00 IST weekdays, before the open):
  0 9 * * 1-5 /opt/dhan-trading/.venv/bin/python /opt/dhan-trading/scripts/egress_check.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Bootstrap sys.path so `config` / `core` resolve when run by cron from any cwd.
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import aiohttp  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger("dhan.egress_check")

# Plain-text echo service: returns the caller's public IP as a bare string, no
# JSON parsing and no auth. Whatever answers here is the identity Dhan sees.
IP_ECHO_URL = "https://api.ipify.org"
TIMEOUT_S = 15


async def _fetch_egress_ip(proxy_url: str) -> str:
    """Public IP as observed through the proxy. Raises on any transport error."""
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(IP_ECHO_URL, proxy=proxy_url) as resp:
            resp.raise_for_status()
            return (await resp.text()).strip()


def _alert(message: str) -> None:
    """Telegram, best-effort — an alerting failure must not mask the exit code."""
    try:
        from core.notify import send
        send(message)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("egress_check: Telegram send failed: %s", exc)


def main() -> int:
    from config import get_config

    cfg = get_config()
    proxy_url = cfg.dhan_proxy_url
    expected = cfg.dhan_whitelisted_egress_ip.strip()

    if not proxy_url or not expected:
        # Direct egress (or nothing to compare against) — there is no proxy
        # identity to verify, and a noisy alert here would train the operator
        # to ignore this check.
        logger.info(
            "egress_check: skipped — DHAN_PROXY_URL and/or "
            "DHAN_WHITELISTED_EGRESS_IP not configured"
        )
        return 0

    try:
        actual = asyncio.run(_fetch_egress_ip(proxy_url))
    except Exception as exc:
        # The local log keeps the full exception — diagnosing a dead proxy needs
        # it. The Telegram message does NOT: aiohttp's proxy errors stringify as
        # "Cannot connect to host <proxy-host>:<port> …", which would push the
        # egress VM's address out over an external channel. Type name only.
        logger.critical("egress_check: proxy UNREACHABLE (%s) — orders cannot be placed", exc)
        _alert(
            f"🚨 EGRESS CHECK: Dhan proxy unreachable ({type(exc).__name__}). "
            "Order placement will FAIL — see the egress_check log for detail."
        )
        return 2

    if actual == expected:
        logger.info("egress_check: OK — egress IP matches the Dhan whitelist")
        return 0

    # Never log/alert the expected IP: it is the one value in this check that is
    # a secret-by-convention (see CLAUDE.md — no IPs in the repo or its output).
    logger.critical(
        "egress_check: WRONG egress IP (got %s) — Dhan will reject orders", actual
    )
    _alert(
        f"🚨 EGRESS CHECK: Dhan proxy is egressing from {actual}, which is NOT "
        "the whitelisted IP. Orders will be REJECTED — fix before the open."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
