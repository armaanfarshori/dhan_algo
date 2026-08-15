"""System / ops handlers: log tail, backfill status, system health, host facts.

Note: postback_handler was intentionally moved to apps/api.py so that
monkeypatching apps.api._reconcile_postback in tests takes effect at
call time (the handler resolves it from the apps.api module namespace).
"""
import json
import logging
import os
import platform
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

logger = logging.getLogger("dhan.api")

# Host facts that cannot change while this process lives (CPU model, thread
# count, kernel/OS string, hostname, installed RAM) — read once, keep here.
# Everything that *does* move (free RAM, free disk, DB facts) is re-read per
# request and rides the 30 s response cache instead.
_STATIC_HOST: dict | None = None

# alembic/versions/NNN_slug.py  →  ("NNN", "slug")
_MIGRATION_RE = re.compile(r"^(\d+)_(.+)\.py$")


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


# ── Host facts (GET /api/system/host) ────────────────────────────────────────
# The dashboard's Infrastructure card used to hardcode the (now terminated) AWS
# instance types.  This endpoint reports what the box ACTUALLY is, using only
# the stdlib (/proc, shutil, platform) plus the existing DB layer — no new
# dependency, no cloud metadata service.  Every field degrades to None rather
# than failing the request: a wrong-but-confident panel is worse than a blank.

def _empty_host() -> dict:
    return {
        "hostname": None, "platform": None, "cpu_model": None, "cpu_count": None,
        "mem_total_bytes": None, "mem_available_bytes": None,
        "disk_total_bytes": None, "disk_used_bytes": None, "disk_free_bytes": None,
    }


def _empty_db() -> dict:
    return {
        "up": False, "version": None, "server_version": None,
        "timescaledb": None, "alembic_head": None, "hypertables": None,
    }


def _cpu_model() -> str | None:
    """First `model name` line from /proc/cpuinfo (Linux only; None elsewhere)."""
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                key, sep, val = line.partition(":")
                if sep and key.strip().lower() == "model name":
                    return val.strip() or None
    except Exception:
        return None
    return None


def _meminfo() -> dict:
    """MemTotal / MemAvailable from /proc/meminfo, in BYTES (the file is kB)."""
    out = {"total": None, "available": None}
    try:
        with open("/proc/meminfo", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                key, sep, val = line.partition(":")
                if not sep:
                    continue
                if key == "MemTotal":
                    out["total"] = int(val.split()[0]) * 1024
                elif key == "MemAvailable":
                    out["available"] = int(val.split()[0]) * 1024
    except Exception:
        pass
    return out


def _static_host_facts() -> dict:
    """Immutable host identity, computed once per process."""
    global _STATIC_HOST
    if _STATIC_HOST is None:
        try:
            node = platform.node() or None
        except Exception:
            node = None
        try:
            plat = platform.platform()
        except Exception:
            plat = None
        try:
            cpus = os.cpu_count()
        except Exception:
            cpus = None
        _STATIC_HOST = {
            "hostname": node,
            "platform": plat,
            "cpu_model": _cpu_model(),
            "cpu_count": cpus,
            "mem_total_bytes": _meminfo()["total"],
        }
    return dict(_STATIC_HOST)


def _migrations() -> list[dict]:
    """alembic/versions/*.py → [{'id': '014', 'desc': 'scalper tables'}, …].

    Newest first, sorted NUMERICALLY (string sort breaks at 010 vs 9).  The
    service's working dir is the repo checkout, so the directory is readable
    at runtime — this is what keeps the dashboard's migration list honest
    instead of a hand-maintained array that stops being true.
    """
    try:
        from apps.api import ROOT
        versions = Path(ROOT) / "alembic" / "versions"
        rows = []
        for path in versions.glob("*.py"):
            m = _MIGRATION_RE.match(path.name)
            if m:
                rows.append({"id": m.group(1), "desc": m.group(2).replace("_", " ")})
        rows.sort(key=lambda r: int(r["id"]), reverse=True)
        return rows
    except Exception as exc:
        logger.warning("host_info: migration list unavailable: %s", exc)
        return []


def _db_facts() -> dict:
    """PostgreSQL / TimescaleDB / schema facts. Any single query may fail."""
    facts = _empty_db()
    try:
        from sqlalchemy import text

        from db import get_engine
        with get_engine().connect() as conn:
            def _scalar(sql: str):
                # One failing statement poisons the implicit transaction, so
                # roll back before the next probe (TimescaleDB absent, say,
                # must not also null out the Postgres version).
                try:
                    return conn.execute(text(sql)).scalar()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    return None

            facts["version"] = _scalar("SELECT version()")
            facts["server_version"] = _scalar("SHOW server_version")
            facts["alembic_head"] = _scalar(
                "SELECT version_num FROM alembic_version")
            facts["timescaledb"] = _scalar(
                "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
            hypertables = _scalar(
                "SELECT count(*) FROM timescaledb_information.hypertables")
            facts["hypertables"] = int(hypertables) if hypertables is not None else None
            facts["up"] = facts["version"] is not None
    except Exception as exc:
        logger.warning("host_info: DB facts unavailable: %s", exc)
    return facts


async def host_info_handler(_r: web.Request) -> web.Response:
    """Real hardware / OS / DB facts for the dashboard Infrastructure card.

    Cached 30 s (free RAM and free disk move; nothing here is expensive).
    """
    from apps.api import _cache_get, _cache_set, _db_query
    cached = _cache_get("system_host", 30)
    if cached:
        return web.json_response(cached)

    def _collect():
        host = _empty_host()
        try:
            host.update(_static_host_facts())
        except Exception as exc:
            logger.warning("host_info: static host facts unavailable: %s", exc)
        mem = _meminfo()
        host["mem_available_bytes"] = mem["available"]
        if host.get("mem_total_bytes") is None:
            host["mem_total_bytes"] = mem["total"]
        try:
            usage = shutil.disk_usage("/")
            host["disk_total_bytes"] = usage.total
            host["disk_used_bytes"] = usage.used
            host["disk_free_bytes"] = usage.free
        except Exception as exc:
            logger.warning("host_info: disk usage unavailable: %s", exc)
        return {
            "ok": True,
            "host": host,
            "db": _db_facts(),
            "migrations": _migrations(),
        }

    try:
        result = await _db_query(_collect)
        _cache_set("system_host", result)
        return web.json_response(result)
    except Exception:
        logger.exception("host_info_handler failed")
        return web.json_response({
            "ok": False, "error": "internal error",
            "host": _empty_host(), "db": _empty_db(), "migrations": [],
        })


