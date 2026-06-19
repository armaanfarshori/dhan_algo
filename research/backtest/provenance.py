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
import subprocess
from datetime import datetime, timezone


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


def provenance(params: dict) -> dict:
    """Build the reproducibility block embedded at the top of every result JSON."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": git_sha(),
        "params": params,
    }
