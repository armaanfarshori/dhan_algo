"""
Strike / wing / DTE / exit-rule sweep over the F&O iron-condor backtest.

Grids the condor's structural knobs and ranks the configs by OUT-OF-SAMPLE
return-on-margin (walk-forward 70/30 chronological split, with an optional
purge/embargo gap), so we can ask "which short-strike distance × wing × DTE ×
exit policy is ROBUST out-of-sample?" rather than picking the in-sample winner.

Lane discipline (same posture as ``fno_risk_sweep`` / ``fno_orchestrator``):
this module **calls** ``research.backtest.fno_condor.run_backtest`` and **reuses**
the engine metric helpers (``_sharpe_from_pnls`` / ``_max_drawdown`` from
``fno_strategies`` and ``go_no_go`` from ``fno_condor``). It NEVER re-implements
pricing / resolution / cost / Sharpe / drawdown / go_no_go math. The sweep only
*chooses which structural params to run and how to rank the results*.

The condor backtest carries the Phase-0c knobs natively:
  * ``strike_method`` ∈ {"move","delta"} with ``move_mult`` / ``target_delta``,
  * ``wing_strikes`` (wing width in grid steps),
  * the entry DTE comes from ``cycles_from_db(entry_dte=...)`` (re-anchored cycles),
  * ``exit_params`` (profit-target / stop-loss / time-stop; default = expiry-hold).

Each grid cell is one ``run_backtest`` over the (DTE-specific) cycle list.

Honesty ledger (inherited verbatim — never dropped)
---------------------------------------------------
VIX-as-weekly-IV proxy · CLOSE-not-FSP settlement · EXPIRY-ONLY when no
intra-cycle spot path is supplied (exit rules need ``cycle["spot_path"]``) ·
single-σ Black-76 (delta selection + MTM are flat-IV until real per-strike IV
lands) · grid selection is IN-SAMPLE-OPTIMISTIC → read ROM_oos, not ROM ·
PRELIMINARY · PAPER / research only.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Reused surface — import, never re-implement (lane discipline)
# ---------------------------------------------------------------------------
from research.backtest.fno_condor import (
    STRIKE_METHOD_DELTA,
    STRIKE_METHOD_MOVE,
    ExitParams,
    cycles_from_db,  # re-exported (the only DB touch; via the CLI)
    go_no_go,
    run_backtest,
)
from research.backtest.fno_strategies import _max_drawdown, _sharpe_from_pnls

logger = logging.getLogger("dhan.backtest.fno_strike_sweep")

DEFAULT_CAPITAL = 200_000.0
WEEKS_PER_YEAR = 52.0

CAVEATS = (
    "VIX-as-weekly-IV proxy · CLOSE-not-FSP settlement · "
    "EXPIRY-ONLY (no path → only time-stop can fire) · single-σ B76 · "
    "grid selection IN-SAMPLE-OPTIMISTIC → read ROM_oos · PRELIMINARY · PAPER only"
)


# ---------------------------------------------------------------------------
# 1. One grid cell
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StrikeConfig:
    """One point in the strike / wing / DTE / exit grid.

    ``move_mult`` is used only for ``strike_method="move"``; ``target_delta`` only
    for ``strike_method="delta"``. ``entry_dte=None`` → the legacy prior-expiry
    anchor. The three exit fields default to None → hold-to-expiry.
    """

    strike_method: str = STRIKE_METHOD_MOVE
    move_mult: float = 1.5
    target_delta: float = 0.16
    wing_strikes: int = 2
    entry_dte: Optional[int] = None
    profit_target_pct: Optional[float] = None
    stop_loss_mult: Optional[float] = None
    time_stop_dte: Optional[int] = None

    @property
    def exit_params(self) -> ExitParams:
        return ExitParams(
            profit_target_pct=self.profit_target_pct,
            stop_loss_mult=self.stop_loss_mult,
            time_stop_dte=self.time_stop_dte,
        )

    @property
    def label(self) -> str:
        if self.strike_method == STRIKE_METHOD_DELTA:
            strike = f"Δ={self.target_delta:g}"
        else:
            strike = f"mm={self.move_mult:g}"
        dte = "prior" if self.entry_dte is None else f"{self.entry_dte}d"
        ex_bits: list[str] = []
        if self.profit_target_pct is not None:
            ex_bits.append(f"pt{self.profit_target_pct:g}")
        if self.stop_loss_mult is not None:
            ex_bits.append(f"sl{self.stop_loss_mult:g}")
        if self.time_stop_dte is not None:
            ex_bits.append(f"ts{self.time_stop_dte}")
        ex = ",".join(ex_bits) if ex_bits else "hold"
        return f"{strike} w={self.wing_strikes} dte={dte} exit={ex}"


# ---------------------------------------------------------------------------
# 2. Grid enumeration (pure)
# ---------------------------------------------------------------------------
def enumerate_grid(
    *,
    strike_methods: list[str],
    move_mults: list[float],
    target_deltas: list[float],
    wing_strikes_opts: list[int],
    entry_dtes: list[Optional[int]],
    exit_configs: list[tuple[Optional[float], Optional[float], Optional[int]]],
) -> list[StrikeConfig]:
    """Cross-product the knobs into a de-duplicated list of :class:`StrikeConfig`.

    The strike-distance axis is method-aware: ``move_mults`` is only crossed under
    ``strike_method="move"`` and ``target_deltas`` only under ``"delta"`` — so the
    irrelevant axis never inflates the grid (a move config does not multiply by the
    delta list, and vice-versa). ``exit_configs`` is a list of
    ``(profit_target_pct, stop_loss_mult, time_stop_dte)`` tuples.
    """
    seen: set[StrikeConfig] = set()
    grid: list[StrikeConfig] = []
    for method in strike_methods:
        strike_axis = (
            [(None, d) for d in target_deltas]
            if method == STRIKE_METHOD_DELTA
            else [(m, None) for m in move_mults]
        )
        for (mm, td), wings, dte, (pt, sl, ts) in itertools.product(
            strike_axis, wing_strikes_opts, entry_dtes, exit_configs
        ):
            cfg = StrikeConfig(
                strike_method=method,
                move_mult=mm if mm is not None else 1.5,
                target_delta=td if td is not None else 0.16,
                wing_strikes=wings,
                entry_dte=dte,
                profit_target_pct=pt,
                stop_loss_mult=sl,
                time_stop_dte=ts,
            )
            if cfg not in seen:
                seen.add(cfg)
                grid.append(cfg)
    return grid


def default_grid() -> list[StrikeConfig]:
    """A compact default grid: move {1.0,1.5,2.0} & delta {0.10,0.16,0.25} ×
    wings {1,2,3} × DTE {None,4,2} × exit {hold, pt0.5, sl2.0}."""
    return enumerate_grid(
        strike_methods=[STRIKE_METHOD_MOVE, STRIKE_METHOD_DELTA],
        move_mults=[1.0, 1.5, 2.0],
        target_deltas=[0.10, 0.16, 0.25],
        wing_strikes_opts=[1, 2, 3],
        entry_dtes=[None, 4, 2],
        exit_configs=[
            (None, None, None),   # hold to expiry
            (0.5, None, None),    # profit-target 50% of credit
            (None, 2.0, None),    # stop-loss 2× credit
        ],
    )


# ---------------------------------------------------------------------------
# 3. Walk-forward OOS metrics on one config's trades (reuse engine helpers)
# ---------------------------------------------------------------------------
def walk_forward_split(
    n_trades: int,
    *,
    oos_frac: float = 0.30,
    purge: int = 0,
) -> tuple[int, int]:
    """Return ``(is_end, oos_start)`` indices for a chronological 70/30 split with
    a ``purge`` (a.k.a. embargo) gap of ``purge`` trades dropped on EACH side of
    the boundary.

    Mirrors the engine's ``split_idx = int(0.7*n)`` boundary; the purge removes
    ``purge`` trades straddling it so a long-dated entry that overlaps the boundary
    cannot leak between IS and OOS. ``purge=0`` → identical to the plain engine
    split. IS = trades[:is_end]; OOS = trades[oos_start:].
    """
    split_idx = max(1, int((1.0 - oos_frac) * n_trades))
    is_end = max(0, split_idx - purge)
    oos_start = min(n_trades, split_idx + purge)
    return is_end, oos_start


def evaluate_config(
    cfg: StrikeConfig,
    cycles: list[dict[str, Any]],
    *,
    k: float = 0.9,
    capital: float = DEFAULT_CAPITAL,
    purge: int = 0,
) -> dict[str, Any]:
    """Run the condor backtest for ``cfg`` and compute full + OOS metrics.

    The condor's ``max_loss`` (per-lot defined-risk loss) is the SPAN proxy for
    ROM — a condor is fully defined-risk so this is exact, not an approximation.
    Trades come back from ``run_backtest`` in cycle order; we sort by entry_date
    before the walk-forward split (defensive — mirrors the engine).
    """
    m = run_backtest(
        cycles,
        k=k,
        move_mult=cfg.move_mult,
        capital=capital,
        wing_strikes=cfg.wing_strikes,
        strike_method=cfg.strike_method,
        target_delta=cfg.target_delta,
        exit_params=cfg.exit_params,
    )
    trades = sorted(m.get("trades", []), key=lambda t: t.entry_date)
    n_trades = len(trades)

    base = {
        "config": cfg.label,
        "strike_method": cfg.strike_method,
        "move_mult": cfg.move_mult,
        "target_delta": cfg.target_delta,
        "wing_strikes": cfg.wing_strikes,
        "entry_dte": cfg.entry_dte,
        "profit_target_pct": cfg.profit_target_pct,
        "stop_loss_mult": cfg.stop_loss_mult,
        "time_stop_dte": cfg.time_stop_dte,
        "n_cycles": m.get("n_cycles", len(cycles)),
        "n_trades": n_trades,
    }

    if n_trades == 0:
        base.update(
            {
                "win_rate": 0.0,
                "net_pnl": 0.0,
                "return_on_margin": 0.0,
                "rom_oos": 0.0,
                "sharpe": 0.0,
                "sharpe_oos": 0.0,
                "max_drawdown": 0.0,
                "max_drawdown_pct": 0.0,
                "mean_span": 0.0,
                "go_no_go": m.get("go_no_go", go_no_go(base, capital=capital)),
            }
        )
        return base

    pnls = [t.net_pnl for t in trades]
    spans = [t.max_loss for t in trades]  # defined-risk SPAN proxy

    is_end, oos_start = walk_forward_split(n_trades, purge=purge)
    pnls_oos = pnls[oos_start:]
    spans_oos = spans[oos_start:]

    net_pnl = sum(pnls)
    total_span = sum(spans)
    return_on_margin = net_pnl / total_span if total_span > 0 else 0.0
    net_oos = sum(pnls_oos)
    span_oos = sum(spans_oos)
    rom_oos = net_oos / span_oos if span_oos > 0 else 0.0

    sharpe = _sharpe_from_pnls(pnls)
    sharpe_oos = _sharpe_from_pnls(pnls_oos) if len(pnls_oos) >= 2 else 0.0
    max_dd = _max_drawdown(pnls)
    wins = sum(1 for p in pnls if p > 0)

    base.update(
        {
            "win_rate": wins / n_trades,
            "net_pnl": net_pnl,
            "return_on_margin": return_on_margin,
            "rom_oos": rom_oos,
            "sharpe": sharpe,
            "sharpe_oos": sharpe_oos,
            "max_drawdown": max_dd,
            "max_drawdown_pct": abs(max_dd) / capital if capital > 0 else 0.0,
            "mean_span": total_span / n_trades,
            "is_end": is_end,
            "oos_start": oos_start,
            "go_no_go": m["go_no_go"],
        }
    )
    return base


# ---------------------------------------------------------------------------
# 4. The sweep
# ---------------------------------------------------------------------------
@dataclass
class SweepResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    n_configs: int = 0
    n_cycles: int = 0


def run_sweep(
    cycles_by_dte: dict[Optional[int], list[dict[str, Any]]],
    *,
    grid: Optional[list[StrikeConfig]] = None,
    k: float = 0.9,
    capital: float = DEFAULT_CAPITAL,
    purge: int = 0,
    min_trades: int = 30,
) -> dict[str, Any]:
    """Run every config in ``grid`` and RANK by OOS ROM (desc), then full Sharpe.

    Parameters
    ----------
    cycles_by_dte:
        Map of ``entry_dte → cycle list`` (each DTE re-anchors the entry day, so a
        cell uses the right cycles). ``None`` key = the legacy prior-expiry anchor.
        The CLI builds this by calling ``cycles_from_db(entry_dte=...)`` once per
        distinct DTE in the grid (so the DB is hit once per DTE, not per cell).
    grid:        Configs to run (default :func:`default_grid`).
    purge:       Walk-forward purge/embargo (trades dropped each side of the split).
    min_trades:  Configs with fewer than this many trades are flagged
                 ``robust=False`` regardless of ROM_oos (small-sample guard).

    Returns
    -------
    ``{"rows": [...ranked...], "robust": [...], "n_configs", "n_cycles", ...}``
    where ``robust`` is the subset that is GO **and** n_trades >= min_trades,
    ranked the same way.
    """
    grid = grid or default_grid()
    rows: list[dict[str, Any]] = []
    n_cycles = 0
    for cfg in grid:
        cycles = cycles_by_dte.get(cfg.entry_dte, [])
        n_cycles = max(n_cycles, len(cycles))
        row = evaluate_config(cfg, cycles, k=k, capital=capital, purge=purge)
        row["robust"] = bool(row["go_no_go"][0]) and row["n_trades"] >= min_trades
        rows.append(row)

    ranked = sorted(rows, key=lambda r: (r["rom_oos"], r["sharpe"]), reverse=True)
    robust = [r for r in ranked if r["robust"]]
    return {
        "rows": ranked,
        "robust": robust,
        "n_configs": len(grid),
        "n_cycles": n_cycles,
        "capital": capital,
        "k": k,
        "purge": purge,
        "min_trades": min_trades,
    }


# ---------------------------------------------------------------------------
# 5. Rendering
# ---------------------------------------------------------------------------
def _fmt_pct(x: float) -> str:
    return f"{x:>8.2%}"


def render_sweep(sweep: dict[str, Any], *, top: int = 20) -> str:
    """Human-readable RANKED table (by OOS ROM) + robust-config callout + caveats."""
    lines: list[str] = []
    lines.append(
        f"F&O STRIKE/WING/DTE/EXIT SWEEP — n_configs={sweep['n_configs']} "
        f"n_cycles={sweep['n_cycles']} capital=₹{sweep['capital']:,.0f} "
        f"k={sweep['k']:g} purge={sweep['purge']} min_trades={sweep['min_trades']}"
    )
    lines.append(f"caveats: {CAVEATS}")
    lines.append("")

    hdr = (
        f"{'config':<40} {'n':>4} {'net':>12} {'ROM':>8} {'ROM_oos':>8} "
        f"{'win':>6} {'sharpe':>7} {'sh_oos':>7} {'DD%':>7} {'GO':>6} {'robust':>7}"
    )
    lines.append(hdr)
    lines.append("-" * len(hdr))

    def _row(m: dict[str, Any]) -> str:
        go = "GO" if m["go_no_go"][0] else "NO-GO"
        return (
            f"{m['config']:<40} {m['n_trades']:>4} {m['net_pnl']:>12,.0f} "
            f"{_fmt_pct(m['return_on_margin'])} {_fmt_pct(m['rom_oos'])} "
            f"{m['win_rate']:>5.1%} {m['sharpe']:>7.2f} {m['sharpe_oos']:>7.2f} "
            f"{m['max_drawdown_pct']:>6.1%} {go:>6} {'yes' if m['robust'] else 'no':>7}"
        )

    for m in sweep["rows"][:top]:
        lines.append(_row(m))
    lines.append("-" * len(hdr))

    robust = sweep["robust"]
    lines.append("")
    if robust:
        lines.append(f"ROBUST configs (GO + n_trades>={sweep['min_trades']}): {len(robust)}")
        best = robust[0]
        lines.append(
            f"TOP robust by ROM_oos: {best['config']} → ROM_oos {best['rom_oos']:.2%} "
            f"(ROM {best['return_on_margin']:.2%}) sharpe {best['sharpe']:.2f} "
            f"n={best['n_trades']}"
        )
    else:
        lines.append("ROBUST configs: NONE — no GO config cleared the min-trades bar.")
    lines.append("")
    lines.append(
        "VERDICT (PRELIMINARY): pick the config that maximises ROM_oos AMONG the "
        "robust set (GO + sample size), not the in-sample ROM winner. Real-IV / FSP "
        "forward paper is the truth test. PAPER only."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6. S3 archival (fail-open — mirrors fno_risk_sweep)
# ---------------------------------------------------------------------------
def _sweep_to_json(sweep: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe sweep dict (tuple go_no_go → list)."""
    def _clean(m: dict[str, Any]) -> dict[str, Any]:
        out = dict(m)
        gng = out.get("go_no_go")
        if isinstance(gng, tuple):
            out["go_no_go"] = [gng[0], gng[1]]
        return out

    return {
        "n_configs": sweep["n_configs"],
        "n_cycles": sweep["n_cycles"],
        "capital": sweep["capital"],
        "k": sweep["k"],
        "purge": sweep["purge"],
        "min_trades": sweep["min_trades"],
        "rows": [_clean(m) for m in sweep["rows"]],
        "robust": [_clean(m) for m in sweep["robust"]],
    }


def archive_s3(
    sweep: dict[str, Any],
    *,
    args: dict[str, Any],
    summary_text: str,
) -> Optional[str]:  # pragma: no cover — needs network/boto3
    """Write results.json + summary.txt + manifest.json under
    ``s3://<S3_BUCKET>/kronos/m3/strike_sweep/<ts>/``. Fail-open."""
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        print("WARNING: --upload-s3 set but S3_BUCKET is empty — skipping upload.")
        return None
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"kronos/m3/strike_sweep/{run_ts}"
    manifest = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "args": args,
        "caveats": CAVEATS.split(" · "),
        "decision": "PRELIMINARY",
    }
    try:
        import boto3

        s3 = boto3.client(
            "s3",
            region_name=os.environ.get(
                "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")
            ),
        )
        s3.put_object(
            Bucket=bucket, Key=f"{prefix}/results.json",
            Body=json.dumps(_sweep_to_json(sweep), indent=2, default=str).encode(),
        )
        s3.put_object(Bucket=bucket, Key=f"{prefix}/summary.txt", Body=summary_text.encode())
        s3.put_object(
            Bucket=bucket, Key=f"{prefix}/manifest.json",
            Body=json.dumps(manifest, indent=2, default=str).encode(),
        )
        uri = f"s3://{bucket}/{prefix}/"
        print(f"Archived strike-sweep results to {uri}")
        return uri
    except Exception as exc:  # noqa: BLE001 — fail-open
        print(f"WARNING: S3 archival failed ({exc}) — results NOT uploaded.")
        return None


# ---------------------------------------------------------------------------
# 7. CLI (DB-init only after arg-parse so --help never touches it)
# ---------------------------------------------------------------------------
def main() -> None:  # pragma: no cover — needs the dhan_trading DB
    """``python -m research.backtest.fno_strike_sweep --capital 200000``."""
    parser = argparse.ArgumentParser(
        description="F&O iron-condor strike/wing/DTE/exit sweep (ranked by OOS ROM)",
    )
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--k", type=float, default=0.9)
    parser.add_argument(
        "--mode", default="weekly", choices=["weekly", "expiry_calendar"],
        help="cycle-boundary mode passed to cycles_from_db",
    )
    parser.add_argument(
        "--purge", type=int, default=0,
        help="walk-forward purge/embargo (trades dropped each side of the 70/30 split)",
    )
    parser.add_argument("--min-trades", type=int, default=30, dest="min_trades")
    parser.add_argument("--top", type=int, default=20, help="rows to print")
    parser.add_argument(
        "--upload-s3", action="store_true", dest="upload_s3",
        help="archive results to s3://$S3_BUCKET/kronos/m3/strike_sweep/",
    )
    args = parser.parse_args()

    grid = default_grid()
    distinct_dtes = sorted(
        {cfg.entry_dte for cfg in grid}, key=lambda x: (x is not None, x)
    )

    from config import get_config
    from db import init_db

    init_db(get_config().db_url)
    # One DB hit per distinct entry DTE (re-anchored cycles), reused across cells.
    cycles_by_dte: dict[Optional[int], list[dict[str, Any]]] = {
        dte: cycles_from_db(mode=args.mode, entry_dte=dte) for dte in distinct_dtes
    }

    sweep = run_sweep(
        cycles_by_dte, grid=grid, k=args.k, capital=args.capital,
        purge=args.purge, min_trades=args.min_trades,
    )
    summary_text = render_sweep(sweep, top=args.top)
    print(summary_text)

    if args.upload_s3:
        archive_s3(sweep, args=vars(args), summary_text=summary_text)


if __name__ == "__main__":  # pragma: no cover
    main()
