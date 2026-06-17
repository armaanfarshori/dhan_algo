"""Backtest results API — read-only viewer for the M3 study.

Serves the result JSONs that `python -m research.backtest ... --json` writes into
research/backtest/results/. The dashboard's Backtest tab renders these (KPI
panel, IS/OOS, gate diagnostics). This NEVER triggers a backtest — heavy
2y×1700 runs compete with the live trader and are launched off-hours from the
CLI, not the browser.
"""
import json
import logging
from pathlib import Path

from aiohttp import web

logger = logging.getLogger("dhan.api")

# apps/routes/backtest.py → parents[2] = repo root → research/backtest/results
_RESULTS = Path(__file__).resolve().parents[2] / "research" / "backtest" / "results"


def _safe_stem(name: str) -> str:
    """Strip any path-traversal — only a bare file stem is allowed."""
    return Path(name).name.replace("..", "")


async def backtest_runs_handler(_r: web.Request) -> web.Response:
    """List completed runs (newest first) with a one-line headline each."""
    runs = []
    if _RESULTS.is_dir():
        for f in sorted(_RESULTS.glob("*.json"), reverse=True):
            try:
                d = json.loads(f.read_text())
            except Exception as exc:
                logger.warning("backtest result %s unreadable: %s", f.name, exc)
                continue
            prov = d.get("provenance", {})
            params = prov.get("params", {})
            full = (d.get("panel") or {}).get("full", {})
            runs.append({
                "name": f.stem,
                "label": d.get("label", f.stem),
                "generated_at": prov.get("generated_at"),
                "git_sha": prov.get("git_sha"),
                "gate": params.get("gate"),
                "from": params.get("from"),
                "to": params.get("to"),
                "split_date": params.get("split_date"),
                "sharpe": full.get("sharpe_daily_ann"),
                "net_pnl": full.get("net_pnl"),
                "trades": full.get("trades"),
                "survivorship": d.get("survivorship"),
            })
    return web.json_response({"runs": runs, "count": len(runs)})


async def backtest_run_handler(r: web.Request) -> web.Response:
    """Return one full result JSON (panel, daily, per_security, trades, gate)."""
    stem = _safe_stem(r.match_info.get("name", ""))
    f = _RESULTS / f"{stem}.json"
    if not f.is_file():
        return web.json_response({"error": f"run '{stem}' not found"}, status=404)
    try:
        return web.json_response(json.loads(f.read_text()))
    except Exception as exc:
        return web.json_response({"error": f"unreadable: {exc}"}, status=500)
