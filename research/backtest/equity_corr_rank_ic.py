"""GO/NO-GO check: does realized stock-futures correlation predict the condor?

The dispersion-gate thesis (research task #11): does the realized cross-sectional
correlation ρ̄ (and dispersion) of the ~188-name stock-futures panel, measured at
condor *entry*, carry information about that NIFTY iron-condor cycle's realized
P&L?  If the rank-IC of [ρ̄, dispersion] vs cycle P&L is ≈ 0 (and its bootstrap CI
straddles 0), the conditioning thesis is dead and a dispersion-gate is not worth
building.  If there's a clean monotone tercile spread, it's worth a real-IV test.

This is **descriptive research only**.  Honest caveats baked into the verdict:
  * small n (~60–75 weekly cycles over the ~18-mo futures window),
  * VIX-as-IV proxy + close-not-FSP (inherited from ``fno_condor.cycles_from_db``),
  * stock *futures* vs *spot*, equal-weight basket, daily resolution.

Reuses ``fno_condor.cycles_from_db`` + ``run_backtest`` for cycle generation and
per-cycle P&L (never reimplemented), and ``equity_correlation`` for the features.
Strict point-in-time: each cycle's feature uses ONLY data ``≤ entry_date``.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Optional

import numpy as np
import pandas as pd

from research.backtest.equity_correlation import (
    DEFAULT_EWMA_LAMBDA,
    DEFAULT_MIN_NAMES,
    DEFAULT_WINDOW,
    dispersion_frame,
)

logger = logging.getLogger("dhan.research.equity_corr_rank_ic")


# ── pure stats ──────────────────────────────────────────────────────────────────
def spearman_rank_ic(x: list[float], y: list[float]) -> Optional[float]:
    """Spearman rank correlation (rank-IC) between ``x`` and ``y``.

    Pairs with a NaN in either coordinate are dropped.  Returns None if < 3 valid
    pairs or if either ranked series is constant.  Implemented via average ranks +
    Pearson on ranks (no scipy dependency).
    """
    xs, ys = [], []
    for a, b in zip(x, y):
        if a is None or b is None:
            continue
        if isinstance(a, float) and math.isnan(a):
            continue
        if isinstance(b, float) and math.isnan(b):
            continue
        xs.append(float(a))
        ys.append(float(b))
    if len(xs) < 3:
        return None
    rx = pd.Series(xs).rank().to_numpy()
    ry = pd.Series(ys).rank().to_numpy()
    if rx.std() == 0 or ry.std() == 0:
        return None
    rx = rx - rx.mean()
    ry = ry - ry.mean()
    denom = math.sqrt(float((rx * rx).sum()) * float((ry * ry).sum()))
    if denom == 0:
        return None
    return float((rx * ry).sum() / denom)


def bootstrap_rank_ic_ci(
    x: list[float],
    y: list[float],
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 7,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Bootstrap CI for the Spearman rank-IC.

    Returns ``(point_estimate, lo, hi)`` where lo/hi are the ``alpha/2`` and
    ``1-alpha/2`` percentiles of the resampled rank-IC (paired resampling with
    replacement).  Any component is None if undefined (too few pairs).
    """
    pairs = [
        (float(a), float(b))
        for a, b in zip(x, y)
        if a is not None and b is not None
        and not (isinstance(a, float) and math.isnan(a))
        and not (isinstance(b, float) and math.isnan(b))
    ]
    point = spearman_rank_ic([p[0] for p in pairs], [p[1] for p in pairs])
    n = len(pairs)
    if n < 5 or point is None:
        return point, None, None
    rng = np.random.default_rng(seed)
    arr = np.array(pairs)
    boots: list[float] = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        sample = arr[idx]
        ic = spearman_rank_ic(sample[:, 0].tolist(), sample[:, 1].tolist())
        if ic is not None:
            boots.append(ic)
    if not boots:
        return point, None, None
    lo = float(np.percentile(boots, 100 * alpha / 2))
    hi = float(np.percentile(boots, 100 * (1 - alpha / 2)))
    return point, lo, hi


def tercile_table(feature: list[float], pnl: list[float]) -> dict[str, Any]:
    """Split cycles into low/mid/high terciles by ``feature`` → P&L stats.

    Returns a dict with per-tercile ``n``, ``mean_pnl``, ``median_pnl``,
    ``win_rate`` and an overall ``high_minus_low`` mean-P&L spread.  NaN pairs
    dropped.
    """
    pairs = [
        (float(f), float(p))
        for f, p in zip(feature, pnl)
        if f is not None and p is not None
        and not (isinstance(f, float) and math.isnan(f))
        and not (isinstance(p, float) and math.isnan(p))
    ]
    if len(pairs) < 3:
        return {"terciles": [], "high_minus_low": None, "n": len(pairs)}
    df = pd.DataFrame(pairs, columns=["feature", "pnl"]).sort_values("feature")
    n = len(df)
    # qcut with 3 bins; fall back to rank-based split on ties.
    try:
        df["bin"] = pd.qcut(df["feature"].rank(method="first"), 3,
                            labels=["low", "mid", "high"])
    except ValueError:  # pragma: no cover - extreme tie degeneracy
        df["bin"] = pd.cut(df["feature"].rank(method="first"), 3,
                           labels=["low", "mid", "high"])
    terciles = []
    means = {}
    for label in ["low", "mid", "high"]:
        sub = df[df["bin"] == label]["pnl"]
        if len(sub) == 0:
            terciles.append({"bin": label, "n": 0, "mean_pnl": None,
                             "median_pnl": None, "win_rate": None})
            continue
        m = float(sub.mean())
        means[label] = m
        terciles.append({
            "bin": label,
            "n": int(len(sub)),
            "mean_pnl": m,
            "median_pnl": float(sub.median()),
            "win_rate": float((sub > 0).mean()),
        })
    hml = (
        means["high"] - means["low"]
        if "high" in means and "low" in means else None
    )
    return {"terciles": terciles, "high_minus_low": hml, "n": n}


# ── point-in-time feature attachment ────────────────────────────────────────────
def attach_features_to_cycles(
    cycles: list[dict],
    feature_frame: pd.DataFrame,
) -> pd.DataFrame:
    """For each cycle, take the feature row at the latest feature date ``≤
    entry_date`` (strict point-in-time).  Asserts no chosen feature date is in
    the future relative to entry_date.

    Returns a tidy frame: one row per cycle with entry_date, net_pnl, rom,
    rho_bar, rho_bar_ewma, dispersion, feature_date.
    """
    feat_dates = pd.DatetimeIndex(feature_frame.index)
    recs = []
    for c in cycles:
        entry = c["entry_date"]
        entry_ts = pd.Timestamp(entry)
        eligible = feat_dates[feat_dates <= entry_ts]
        if len(eligible) == 0:
            continue
        fdate = eligible.max()
        # PIT assertion — never use a feature dated after entry.
        assert fdate <= entry_ts, f"PIT violation: feature {fdate} > entry {entry_ts}"
        row = feature_frame.loc[fdate]
        recs.append({
            "entry_date": entry,
            "feature_date": fdate.date() if hasattr(fdate, "date") else fdate,
            "net_pnl": c.get("net_pnl"),
            "rom": c.get("rom"),
            "rho_bar": float(row.get("rho_bar", math.nan)),
            "rho_bar_ewma": float(row.get("rho_bar_ewma", math.nan)),
            "dispersion": float(row.get("dispersion", math.nan)),
        })
    return pd.DataFrame(recs)


def _cycles_with_pnl(
    *, mode: str, k: float, capital: float
) -> list[dict]:
    """Generate NIFTY condor cycles + attach per-cycle net P&L / ROM.

    Reuses ``fno_condor.cycles_from_db`` + ``run_backtest``.  ``run_backtest``
    emits a CondorTrade per *traded* (gate-passed) cycle; we key those back to
    their entry_date so the rank-IC sees real realized outcomes.  ROM =
    net_pnl / max_loss (per-cycle risk-adjusted).
    """
    from research.backtest.fno_condor import cycles_from_db, run_backtest

    raw_cycles = cycles_from_db(mode=mode)
    result = run_backtest(raw_cycles, k=k, capital=capital)
    out = []
    for t in result["trades"]:
        rom = (t.net_pnl / t.max_loss) if t.max_loss and t.max_loss > 0 else None
        out.append({
            "entry_date": t.entry_date,
            "expiry_date": t.expiry_date,
            "net_pnl": t.net_pnl,
            "rom": rom,
        })
    return out


# ── verdict render ──────────────────────────────────────────────────────────────
def _fmt_ci(point: Optional[float], lo: Optional[float], hi: Optional[float]) -> str:
    if point is None:
        return "n/a (insufficient pairs)"
    ci = f"[{lo:+.3f}, {hi:+.3f}]" if lo is not None and hi is not None else "[n/a]"
    return f"{point:+.3f}  95% CI {ci}"


def render_verdict(attached: pd.DataFrame, *, n_boot: int = 2000) -> str:
    """Build the human-readable GO/NO-GO verdict block."""
    n = len(attached)
    lines = [
        "================ DISPERSION-GATE GO/NO-GO (descriptive) ================",
        f"cycles with feature + P&L : n = {n}",
    ]
    if n < 3:
        lines.append("INSUFFICIENT DATA — cannot compute rank-IC.")
        return "\n".join(lines)

    pnl = attached["net_pnl"].tolist()
    rom = attached["rom"].tolist()
    for feat_name in ["rho_bar", "rho_bar_ewma", "dispersion"]:
        feat = attached[feat_name].tolist()
        ic_pnl = bootstrap_rank_ic_ci(feat, pnl, n_boot=n_boot)
        ic_rom = bootstrap_rank_ic_ci(feat, rom, n_boot=n_boot)
        lines.append("")
        lines.append(f"[{feat_name}]")
        lines.append(f"  rank-IC vs net_pnl : {_fmt_ci(*ic_pnl)}")
        lines.append(f"  rank-IC vs ROM     : {_fmt_ci(*ic_rom)}")
        tt = tercile_table(feat, pnl)
        for row in tt["terciles"]:
            if row["mean_pnl"] is None:
                lines.append(f"    {row['bin']:>4}: (empty)")
            else:
                lines.append(
                    f"    {row['bin']:>4}: n={row['n']:<3} "
                    f"mean_pnl=₹{row['mean_pnl']:>10,.0f} "
                    f"median=₹{row['median_pnl']:>10,.0f} "
                    f"win={row['win_rate']:.0%}"
                )
        if tt["high_minus_low"] is not None:
            lines.append(
                f"    high−low mean_pnl spread : ₹{tt['high_minus_low']:,.0f}"
            )

    # Verdict heuristic: a thesis is alive only if at least one feature's
    # rank-IC CI excludes 0.
    alive = False
    for feat_name in ["rho_bar", "rho_bar_ewma", "dispersion"]:
        feat = attached[feat_name].tolist()
        _, lo, hi = bootstrap_rank_ic_ci(feat, pnl, n_boot=n_boot)
        if lo is not None and hi is not None and (lo > 0 or hi < 0):
            alive = True
            break
    lines.append("")
    if alive:
        lines.append("VERDICT: AT LEAST ONE FEATURE'S rank-IC CI EXCLUDES 0 → "
                     "thesis WORTH a real-IV follow-up (still descriptive, small n).")
    else:
        lines.append("VERDICT: ALL rank-IC CIs STRADDLE 0 → dispersion-gate "
                     "thesis NOT SUPPORTED on this sample.")
    lines.append("CAVEATS: small n; VIX-as-IV proxy + close-not-FSP (fno_condor); "
                 "stock futures vs spot; equal-weight basket; daily resolution.")
    lines.append("=======================================================================")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────────────
def main() -> None:  # pragma: no cover - thin CLI
    import argparse

    parser = argparse.ArgumentParser(
        description="Rank-IC of stock-futures correlation/dispersion vs condor P&L."
    )
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    parser.add_argument("--ewma-lambda", type=float, default=DEFAULT_EWMA_LAMBDA)
    parser.add_argument("--min-names", type=int, default=DEFAULT_MIN_NAMES)
    parser.add_argument("--mode", default="weekly", choices=["weekly", "expiry_calendar"])
    parser.add_argument("--k", type=float, default=0.9, help="vol-gate threshold")
    parser.add_argument("--capital", type=float, default=200_000.0)
    parser.add_argument("--n-boot", type=int, default=2000)
    args = parser.parse_args()

    from config import get_config
    from db import init_db

    from research.backtest.equity_correlation import load_returns

    cfg = get_config()
    init_db(getattr(cfg, "backtest_db_url", None) or cfg.db_url)

    cycles = _cycles_with_pnl(mode=args.mode, k=args.k, capital=args.capital)
    if not cycles:
        print("No condor cycles with P&L produced — check DB / gate.")
        return
    entry_dates = [c["entry_date"] for c in cycles]
    start = min(entry_dates)
    # Pull futures returns up to the last condor entry (features only need ≤ entry).
    data = load_returns(timeframe=args.timeframe, end=str(max(entry_dates)))
    returns = data["returns"]
    if returns.empty:
        print("No futures_bars returns loaded — cannot compute features.")
        return

    frame = dispersion_frame(
        returns, window=args.window, ewma_lambda=args.ewma_lambda,
        min_names=args.min_names,
    )
    attached = attach_features_to_cycles(cycles, frame)
    print(f"condor cycles: {len(cycles)} | first entry {start} | "
          f"feature-attached: {len(attached)}")
    print(render_verdict(attached, n_boot=args.n_boot))


if __name__ == "__main__":  # pragma: no cover
    main()
