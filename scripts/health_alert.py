#!/usr/bin/env python3
"""
External health-alert monitor — runs every 5 min from cron, completely
decoupled from the trading engine (reads files + logs; never imports engine
code at runtime).

Checks (all gated to market hours where noted):
  1. Heartbeat stale  — run/trader_heartbeat.json missing or ts > 90s old
                        (weekday 09:15–15:30 IST only)
  2. Feed down        — heartbeat feed.connected == false (market hours only)
  3. Risk halted      — heartbeat risk.halted == true (any hour, de-duped)
  4. Disk full        — / usage > 80% (any hour)
  5. CRITICAL/REJECTED lines in /var/log/dhan/trader.log since last run

De-duplication: run/health_alert_state.json records
  {
    "last_offset": <byte offset into trader.log>,
    "active": {<condition_key>: <ISO timestamp first alerted>}
  }
Conditions only alert once per edge transition (False→True); they clear
automatically when the condition resolves (True→False transition writes a
"cleared" message and removes the key from active).

Usage:
  python scripts/health_alert.py             # runs checks, sends Telegram
  python scripts/health_alert.py --dry-run   # prints instead of sending
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── Bootstrap path so imports resolve from repo root ─────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s")
logger = logging.getLogger("dhan.health_alert")

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR   = Path("/opt/dhan-trading")
# Fallback to the repo root when run locally (e.g. --dry-run on the Mac)
if not BASE_DIR.exists():
    BASE_DIR = Path(__file__).parent.parent

RUN_DIR           = BASE_DIR / "run"
HEARTBEAT_FILE    = RUN_DIR / "trader_heartbeat.json"
STATE_FILE        = RUN_DIR / "health_alert_state.json"
TRADER_LOG        = Path("/var/log/dhan/trader.log")

DISK_ALERT_PCT    = 80
HEARTBEAT_STALE_S = 90
LOG_TAIL_LINES    = 200  # max lines scanned from new content per run


# ── IST helpers ───────────────────────────────────────────────────────────────
def _ist_now() -> datetime:
    IST = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(IST)


def _is_market_hours() -> bool:
    """True during weekday 09:15–15:30 IST (inclusive)."""
    now = _ist_now()
    if now.weekday() >= 5:       # Saturday = 5, Sunday = 6
        return False
    t = now.time()
    from datetime import time as _time
    return _time(9, 15) <= t <= _time(15, 30)


# ── State file helpers ────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {"last_offset": 0, "active": {}}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, default=str))
        tmp.replace(STATE_FILE)
    except Exception as exc:
        logger.warning("Could not write state file: %s", exc)


# ── Heartbeat reader ──────────────────────────────────────────────────────────
def _read_heartbeat() -> dict | None:
    try:
        if not HEARTBEAT_FILE.exists():
            return None
        return json.loads(HEARTBEAT_FILE.read_text())
    except Exception:
        return None


# ── Individual checks — each returns (condition_key, alert_message | None) ───

def check_heartbeat_stale(hb: dict | None) -> tuple[str, str | None]:
    key = "heartbeat_stale"
    if not _is_market_hours():
        return key, None
    if hb is None:
        return key, "ALERT: trader heartbeat file missing — process may be down"
    try:
        ts = datetime.fromisoformat(hb["ts"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > HEARTBEAT_STALE_S:
            return key, f"ALERT: trader heartbeat stale ({age:.0f}s old) — process may be down"
    except Exception:
        return key, "ALERT: trader heartbeat unreadable — process may be down"
    return key, None


def check_feed_down(hb: dict | None) -> tuple[str, str | None]:
    key = "feed_down"
    if not _is_market_hours():
        return key, None
    if hb is None:
        return key, None   # heartbeat_stale already covers this
    try:
        if not hb.get("feed", {}).get("connected", True):
            return key, "ALERT: WebSocket feed disconnected during market hours"
    except Exception:
        pass
    return key, None


def check_risk_halted(hb: dict | None) -> tuple[str, str | None]:
    key = "risk_halted"
    if hb is None:
        return key, None
    try:
        risk = hb.get("risk", {})
        if risk.get("halted", False):
            reason = risk.get("halt_reason", "unknown reason")
            return key, f"ALERT: trading halted — {reason}"
    except Exception:
        pass
    return key, None


def check_disk_usage() -> tuple[str, str | None]:
    key = "disk_full"
    try:
        usage = shutil.disk_usage("/")
        pct = usage.used * 100 // usage.total
        if pct > DISK_ALERT_PCT:
            return key, f"ALERT: disk {pct}% full (threshold {DISK_ALERT_PCT}%)"
    except Exception:
        pass
    return key, None


def check_log_errors(state: dict) -> tuple[str, list[str]]:
    """
    Tail new content from trader.log since last run (tracked by byte offset).
    Returns (condition_key, list_of_alert_lines).  Unlike the other checks this
    is edge-triggered per matched line, not de-duped by condition key — each
    new CRITICAL / REJECTED line is its own alert.
    """
    key = "log_errors"
    alerts: list[str] = []
    try:
        if not TRADER_LOG.exists():
            return key, alerts
        size = TRADER_LOG.stat().st_size
        last_offset = state.get("last_offset", 0)
        # If the log was rotated (shrunk), reset to a safe look-back
        if last_offset > size:
            last_offset = max(0, size - 8192)
        with TRADER_LOG.open("rb") as f:
            f.seek(last_offset)
            new_bytes = f.read()
            state["last_offset"] = f.tell()
        if not new_bytes:
            return key, alerts
        lines = new_bytes.decode("utf-8", errors="replace").splitlines()
        # Honour the tail cap to avoid flooding on a huge burst
        lines = lines[-LOG_TAIL_LINES:]
        for line in lines:
            upper = line.upper()
            if "CRITICAL" in upper or "REJECTED" in upper:
                # Strip ANSI and keep it short
                snippet = line.strip()[:200]
                alerts.append(f"ALERT [trader.log]: {snippet}")
    except Exception as exc:
        logger.warning("check_log_errors failed: %s", exc)
    return key, alerts


# ── De-dup logic for edge-based single-condition checks ──────────────────────

def _process_condition(key: str, message: str | None, state: dict,
                       dry_run: bool) -> list[str]:
    """
    Returns a list of messages to dispatch (may be empty, a new-alert, or a
    cleared message).
    """
    active = state.setdefault("active", {})
    to_send: list[str] = []
    if message is not None:
        if key not in active:
            # New condition — alert and record
            active[key] = _ist_now().isoformat()
            to_send.append(message)
    else:
        if key in active:
            # Condition cleared
            del active[key]
            cleared_map = {
                "heartbeat_stale": "CLEARED: trader heartbeat is fresh again",
                "feed_down":       "CLEARED: WebSocket feed reconnected",
                "risk_halted":     "CLEARED: trading halt lifted",
                "disk_full":       "CLEARED: disk usage back below threshold",
            }
            cleared_msg = cleared_map.get(key, f"CLEARED: {key}")
            to_send.append(cleared_msg)
    return to_send


# ── Dispatch ──────────────────────────────────────────────────────────────────

def _dispatch(messages: list[str], dry_run: bool) -> None:
    if not messages:
        return
    combined = "\n".join(messages)
    if dry_run:
        print(combined)
        return
    try:
        from core.notify import send
        send(combined)
    except Exception as exc:
        logger.error("Telegram send failed: %s — messages: %s", exc, combined)


# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    state = _load_state()
    hb = _read_heartbeat()
    to_send: list[str] = []

    # Single-condition edge-based checks
    single_checks = [
        check_heartbeat_stale(hb),
        check_feed_down(hb),
        check_risk_halted(hb),
        check_disk_usage(),
    ]
    for key, msg in single_checks:
        to_send.extend(_process_condition(key, msg, state, dry_run))

    # Log-error check (one alert per new matched line — no de-dup needed)
    _log_key, log_alerts = check_log_errors(state)
    to_send.extend(log_alerts)

    _dispatch(to_send, dry_run)

    if dry_run and not to_send:
        print("[dry-run] all checks passed — nothing to alert")

    _save_state(state)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DhanAIBot external health monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="print alerts instead of sending to Telegram")
    args = parser.parse_args()
    try:
        run(dry_run=args.dry_run)
    except Exception as exc:
        # Top-level catch: the cron must never crash loudly
        logger.error("health_alert top-level error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
