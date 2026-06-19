"""Reproducibility stamp for backtest results.

Every result JSON embeds the exact git SHA (+ a -dirty marker if the working
tree wasn't clean) and the full parameter set, so a number can always be traced
back to the code + inputs that produced it. Identical inputs ⇒ identical output.

Callers should include in params:
  kronos_model          — base HF model id (cfg.kronos_model)
  kronos_checkpoint     — fine-tuned checkpoint URI, or "" for zero-shot
  kronos_loaded_from    — engine._loaded_from string (how the weights were sourced)
  universe_lookback_days — point_in_time_universe lookback window (default 30)
"""
import logging
import subprocess
from datetime import datetime, timezone

logger = logging.getLogger("dhan.backtest.provenance")


def git_sha() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True).stdout.strip()
        return f"{sha}{'-dirty' if dirty else ''}"
    except Exception:
        return "unknown"


def package_versions() -> dict:
    """Return installed versions of key ML/data packages (best-effort; missing = None)."""
    from importlib.metadata import version, PackageNotFoundError

    result: dict = {}
    for pkg in ("torch", "numpy", "pandas"):
        try:
            result[pkg] = version(pkg)
        except PackageNotFoundError:
            result[pkg] = None
    return result


def clean_db_build_dates() -> dict:
    """
    Query dhan_clean.clean_universe for the earliest and latest built_at timestamps.

    Returns a dict with keys ``min_built_at`` and ``max_built_at`` (ISO strings),
    or both None when the table is absent or the query fails.  The shared db engine
    must already be pointed at dhan_clean (init_db called with backtest_db_url).
    """
    out: dict = {"min_built_at": None, "max_built_at": None}
    try:
        from sqlalchemy import text
        from db import get_engine

        with get_engine().connect() as conn:
            row = conn.execute(
                text("SELECT min(built_at), max(built_at) FROM clean_universe")
            ).fetchone()
        if row:
            out["min_built_at"] = row[0].isoformat() if row[0] else None
            out["max_built_at"] = row[1].isoformat() if row[1] else None
    except Exception as exc:
        logger.debug("provenance: clean_universe build-date query failed (%s)", exc)
    return out


def provenance(params: dict) -> dict:
    """Build the reproducibility block embedded at the top of every result JSON."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": git_sha(),
        "package_versions": package_versions(),
        "params": params,
    }
