"""System / ops handlers: log tail, backfill status, system health.

Note: postback_handler was intentionally moved to apps/api.py so that
monkeypatching apps.api._reconcile_postback in tests takes effect at
call time (the handler resolves it from the apps.api module namespace).
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

logger = logging.getLogger("dhan.api")


async def logs_handler(request: web.Request) -> web.Response:
    from apps.api import TRADER_LOG, _db_query
    limit = int(request.rel_url.query.get("limit", 50))
    limit = max(1, min(limit, 500))

    def _tail():
        import re
        if not TRADER_LOG.exists():
            return []
        lines = TRADER_LOG.read_text(errors="replace").splitlines()[-limit:]
        today = datetime.now(timezone.utc).date()
        out = []
        for ln in lines:
            parts = ln.split("  ", 2)
            level = parts[1].strip() if len(parts) > 2 else "INFO"
            ts_raw = parts[0].strip() if len(parts) > 2 else ""
            # Old log lines carry HH:MM:SS only — the dashboard runs
            # new Date(ts), so a bare time renders as "Invalid Date".
            # Lift it to full ISO (server logs in UTC).
            if re.fullmatch(r"\d{2}:\d{2}:\d{2}", ts_raw):
                ts = f"{today}T{ts_raw}+00:00"
            else:
                ts = ts_raw
            body = parts[2].lstrip() if len(parts) > 2 else ln
            name = "trader"
            if " — " in body:
                name, body = body.split(" — ", 1)
                name = name.replace("dhan.", "")
            out.append({
                "ts": ts,
                "level": level,
                "icon": {"INFO": "·", "WARNING": "⚠", "ERROR": "✗", "CRITICAL": "⛔"}.get(level, "·"),
                "name": name,
                "msg": body,
            })
        return out

    logs = await _db_query(_tail)
    return web.json_response({"ok": True, "logs": logs})


async def backfill_status_handler(_r: web.Request) -> web.Response:
    """Backfill progress: checkpoint (authoritative) + process + log tail."""
    import subprocess
    from apps.api import cfg
    ckpt = {}
    try:
        ckpt = json.loads(Path(cfg.backfill_checkpoint_path).read_text())
        ckpt["pct"] = round(ckpt["index"] / max(ckpt["total"], 1) * 100, 1)
    except Exception:
        pass
    lines: list[str] = []
    try:
        result = subprocess.run(["tail", "-30", cfg.backfill_log_path],
                                capture_output=True, text=True, timeout=3)
        lines = [l for l in result.stdout.strip().split("\n") if l]
    except Exception:
        pass
    running = subprocess.run(["pgrep", "-f", "backfill.py"],
                             capture_output=True).returncode == 0
    return web.json_response({"ok": True, "running": running,
                              "checkpoint": ckpt, "log_tail": lines})


async def system_health_handler(_r: web.Request) -> web.Response:
    """Automation health: cron schedule, alert channel, recent error count.
    Replaces the Hermes panel — the gateway was retired 2026-06-11."""
    from apps.api import _cache_get, _cache_set, _db_query, cfg
    cached = _cache_get("system_health", 30)
    if cached:
        return web.json_response(cached)

    def _collect():
        import subprocess
        crons = []
        try:
            out = subprocess.run(["crontab", "-l"], capture_output=True,
                                 text=True, timeout=3).stdout
            for ln in out.splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    sched = " ".join(ln.split()[:5])
                    label = ("backfill watchdog" if "backfill_resume" in ln else
                             "EOD summary" if "eod_summary" in ln else
                             "calibration" if "ml.calibration" in ln else
                             ln.split("&&")[-1][:40])
                    crons.append({"schedule": sched, "job": label})
        except Exception:
            pass
        errors_today = 0
        try:
            log = Path("/var/log/dhan/trader.log").read_text(errors="replace")
            today = datetime.now(timezone.utc).date().isoformat()
            errors_today = sum(1 for l in log.splitlines()
                               if l.startswith(today) and ("ERROR" in l or "CRITICAL" in l))
        except Exception:
            pass
        return {
            "ok": True,
            "telegram_configured": bool(cfg.telegram_bot_token and cfg.telegram_chat_id),
            "crons": crons,
            "trader_errors_today": errors_today,
            "hermes": "retired 2026-06-11 — plain Telegram alerts via core/notify.py",
        }

    result = await _db_query(_collect)
    _cache_set("system_health", result)
    return web.json_response(result)


