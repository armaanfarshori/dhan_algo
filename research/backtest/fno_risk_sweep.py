"""
Portfolio risk-governor + parameter sweep over the F&O weekly-cycle backtest.

The F&O backtest (``research/backtest/fno_strategies.run_strategy_backtest``) is
one-position-per-weekly-cycle, held to expiry — it has no max-trades / drawdown-limit
knob.  This module adds a thin **portfolio governor** ON TOP of that engine and a CLI
that sweeps a grid of governor settings, so we can ask "what happens to the validated
iron-condor edge under a trade cap or a drawdown circuit-breaker on ₹500K?".

Lane discipline (same posture as ``fno_orchestrator``): this module **calls**
``fno_strategies`` (``run_strategy_backtest`` over ``cycles_from_db``) and **reuses** its
metric helpers (``_sharpe_from_pnls`` / ``_max_drawdown`` / ``go_no_go``).  It NEVER edits
their internals and NEVER re-implements pricing / resolution / cost / SPAN / Sharpe / DD
math.  The governor only *selects which already-priced cycles count* — it changes nothing
about how a single cycle is priced or resolved.

Governor semantics (deterministic over the chronological cycle sequence)
------------------------------------------------------------------------
The engine returns one ``StrategyTrade`` per traded cycle, sorted by ``entry_date``.
The governor walks that sequence in order and decides which cycles are *deployed*:

* ``capital`` (default ₹500,000):
    Allocated portfolio capital.  Scales ``return_on_capital`` / CAGR and is the base
    for ``go_no_go``'s 15%-of-capital drawdown limit.  Does NOT change which cycles
    deploy (SPAN per cycle is fixed by the engine; we do not size positions to capital).

* ``max_trades`` (default None = unlimited):
    Cap the number of cycles deployed in the run.  Once ``max_trades`` positions have
    been opened, NO further cycles are opened for the rest of the run.  ``None`` =
    unlimited.

* ``dd_limit_pct`` (default None = off):
    A drawdown circuit-breaker.  As the governor walks the deployed sequence it tracks
    cumulative net P&L and its running peak.  When the drawdown from the equity peak
    exceeds ``dd_limit_pct`` % of ``capital``, the governor HALTS — it opens no further
    positions for the remainder of the run.
    Resume rule (simplest, by design): **halt is permanent for the run** — there is no
    re-arm.  Cycles are weekly and NON-OVERLAPPING (each is held to expiry before the
    next opens), so ``max_concurrent`` is effectively 1: a halt never strands an open
    position — the breaching trade has already resolved when we evaluate the breach, and
    everything after it is simply never opened.  (A trade that itself causes the breach
    is KEPT — it was already open when the loss landed; we only stop *opening new* ones.)

* ``max_concurrent`` (default 1):
    Documented for completeness.  Weekly cycles never overlap, so the only meaningful
    value is 1; values > 1 are accepted but have no effect on this expiry-only,
    non-overlapping sequence.  Kept as an explicit knob so the semantics are stated, not
    implied.

The governor is a pure function of the (chronological) ``(net_pnl, span)`` sequence:
no DB, no randomness.  A DD-halt removes ALL subsequent trades; ``max_trades`` truncates
to the first N.  Both are applied together (whichever bites first wins per-step).

Metrics
-------
Per governed combo we report, on ``capital`` (default ₹500K): ``net_pnl``,
``return_on_margin`` (ROM = Σnet / Σspan), ``return_on_capital``, ``sharpe`` (+ OOS via
the engine's 70/30 chronological split), ``max_drawdown`` (abs ₹ and % of capital),
``win_rate``, ``n_trades``, ``participation`` (deployed / available cycles), and a
CAGR-ish annualised return (weekly cycles → ``(1+ROC)**(52/n) − 1`` proxy).  All Sharpe /
DD / go_no_go math is the engine's, imported — never reimplemented.

Honesty ledger (inherited verbatim from ``fno_strategies`` — never dropped)
---------------------------------------------------------------------------
VIX-as-weekly-IV proxy · CLOSE-not-FSP settlement · EXPIRY-ONLY (tail-blind, no
path/intra-cycle stop) · single-σ Black-76 · SPAN approximate.  Every verdict here is
PRELIMINARY and PAPER / research only — no live order paths.  The governor is a portfolio
overlay; it does NOT add an intra-cycle stop, so the tail-blindness caveat still holds.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Reused surface — import, never re-implement (lane discipline)
# ---------------------------------------------------------------------------
from research.backtest.fno_strategies import (
    FNO_STRATEGIES,
    _max_drawdown,
    _sharpe_from_pnls,
    cycles_from_db,  # re-exported unchanged (the only DB touch)
    go_no_go,
    run_strategy_backtest,
)

logger = logging.getLogger("dhan.backtest.fno_risk_sweep")

DEFAULT_CAPITAL = 500_000.0
WEEKS_PER_YEAR = 52.0

# Honesty caveats — surfaced in every printed table + archived summary/manifest.
CAVEATS = (
    "VIX-as-weekly-IV proxy · CLOSE-not-FSP settlement · "
    "EXPIRY-ONLY (tail-blind, no path stop) · single-σ B76 · SPAN approximate · "
    "PRELIMINARY · PAPER only"
)


# ---------------------------------------------------------------------------
# 1. Governor knobs + result
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GovernorParams:
    """Portfolio-governor knobs (see module docstring for full semantics).

    ``max_trades=None`` → unlimited.  ``dd_limit_pct=None`` → no circuit-breaker.
    ``max_concurrent`` is documented-only (weekly cycles never overlap → effectively 1).
    """

    capital: float = DEFAULT_CAPITAL
    max_trades: Optional[int] = None
    dd_limit_pct: Optional[float] = None
    max_concurrent: int = 1

    @property
    def label(self) -> str:
        mt = "∞" if self.max_trades is None else str(self.max_trades)
        dd = "off" if self.dd_limit_pct is None else f"{self.dd_limit_pct:g}%"
        return f"mt={mt} dd={dd}"


@dataclass
class GovernedRun:
    """The governor's deterministic walk over one chronological cycle sequence.

    ``kept`` is the per-trade ``(net_pnl, span)`` list of DEPLOYED cycles (in order);
    ``halt_reason`` records why opening stopped (or "" if the whole sequence ran).
    """

    kept_pnls: list[float] = field(default_factory=list)
    kept_spans: list[float] = field(default_factory=list)
    n_available: int = 0          # cycles the engine produced (pre-governor)
    n_deployed: int = 0           # cycles the governor opened
    halt_reason: str = ""


# ---------------------------------------------------------------------------
# 2. The governor (pure — no DB, no randomness)
# ---------------------------------------------------------------------------
def apply_governor(
    pnls: list[float],
    spans: list[float],
    gov: GovernorParams,
) -> GovernedRun:
    """Walk the chronological ``(pnls, spans)`` sequence applying the governor.

    Deterministic.  ``max_trades`` truncates to the first N opened; ``dd_limit_pct``
    permanently halts opening once cumulative drawdown from the equity peak exceeds
    ``dd_limit_pct`` % of ``capital``.  The trade that causes a breach is KEPT (it was
    already open); everything strictly AFTER it is never opened.  Both knobs apply
    together — whichever halts first wins.

    Returns a :class:`GovernedRun` with the kept (deployed) trades + a halt reason.
    """
    if len(pnls) != len(spans):
        raise ValueError("pnls and spans must be the same length")

    run = GovernedRun(n_available=len(pnls))
    dd_threshold = (
        (gov.dd_limit_pct / 100.0) * gov.capital
        if gov.dd_limit_pct is not None
        else None
    )

    cum = 0.0
    peak = 0.0
    for i, (pnl, span) in enumerate(zip(pnls, spans)):
        # max_trades: stop OPENING once N positions are already deployed.
        if gov.max_trades is not None and run.n_deployed >= gov.max_trades:
            run.halt_reason = f"max_trades={gov.max_trades} reached"
            break

        # Open this cycle.
        run.kept_pnls.append(pnl)
        run.kept_spans.append(span)
        run.n_deployed += 1

        # Update equity + drawdown AFTER opening (the trade resolved at expiry).
        cum += pnl
        if cum > peak:
            peak = cum
        drawdown = peak - cum  # >= 0

        # dd_limit: if this resolved trade pushed drawdown past the limit, KEEP it
        # (already open) but open nothing further for the rest of the run.
        if dd_threshold is not None and drawdown > dd_threshold:
            run.halt_reason = (
                f"dd_limit={gov.dd_limit_pct:g}% breached "
                f"(drawdown ₹{drawdown:,.0f} > ₹{dd_threshold:,.0f}) at trade {i + 1}"
            )
            break

    return run


# ---------------------------------------------------------------------------
# 3. Metrics on a governed sequence (reuse engine helpers; never reimplement)
# ---------------------------------------------------------------------------
def _cagr_ish(return_on_capital: float, n_trades: int) -> float:
    """Annualised-return proxy for weekly cycles.

    n weekly trades cover ~n/52 of a year, so we annualise the total return-on-capital
    by ``(1 + ROC) ** (52 / n) − 1``.  Returns 0.0 for < 1 trade or a wipeout
    (1 + ROC <= 0, where the power is undefined for fractional exponents).
    """
    if n_trades < 1:
        return 0.0
    base = 1.0 + return_on_capital
    if base <= 0:
        return -1.0  # total wipeout (or worse) → -100% annualised, floored
    return base ** (WEEKS_PER_YEAR / n_trades) - 1.0


def metrics_for_combo(
    run: GovernedRun,
    gov: GovernorParams,
    *,
    strategy: str,
    gate: str,
) -> dict[str, Any]:
    """Compute the per-combo metrics dict on ``gov.capital``.

    Reuses ``_sharpe_from_pnls`` (full + OOS via the engine's 70/30 chronological split),
    ``_max_drawdown`` and ``go_no_go`` — no metric math is reimplemented here.
    """
    pnls = run.kept_pnls
    spans = run.kept_spans
    n_trades = len(pnls)
    capital = gov.capital

    if n_trades == 0:
        metrics: dict[str, Any] = {
            "strategy": strategy,
            "gate": gate,
            "governor": gov.label,
            "max_trades": gov.max_trades,
            "dd_limit_pct": gov.dd_limit_pct,
            "max_concurrent": gov.max_concurrent,
            "capital": capital,
            "n_available": run.n_available,
            "n_trades": 0,
            "participation": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "sharpe": 0.0,
            "sharpe_oos": 0.0,
            "net_pnl": 0.0,
            "return_on_margin": 0.0,
            "rom_oos": 0.0,
            "return_on_capital": 0.0,
            "cagr": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
            "mean_span": 0.0,
            "halt_reason": run.halt_reason,
        }
        metrics["go_no_go"] = go_no_go(metrics, capital=capital)
        return metrics

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / n_trades
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")

    sharpe = _sharpe_from_pnls(pnls)
    # 70/30 chronological IS/OOS split — IDENTICAL to the engine's split.
    split_idx = max(1, int(0.7 * n_trades))
    pnls_oos = pnls[split_idx:]
    spans_oos = spans[split_idx:]
    sharpe_oos = _sharpe_from_pnls(pnls_oos) if len(pnls_oos) >= 2 else 0.0

    max_dd = _max_drawdown(pnls)           # NEGATIVE ₹ (or 0.0)
    max_dd_pct = abs(max_dd) / capital if capital > 0 else 0.0

    net_pnl = sum(pnls)
    total_span = sum(spans)
    return_on_margin = net_pnl / total_span if total_span > 0 else 0.0
    total_span_oos = sum(spans_oos)
    net_oos = sum(pnls_oos)
    rom_oos = net_oos / total_span_oos if total_span_oos > 0 else 0.0
    return_on_capital = net_pnl / capital
    mean_span = total_span / n_trades
    participation = n_trades / run.n_available if run.n_available > 0 else 0.0
    cagr = _cagr_ish(return_on_capital, n_trades)

    metrics = {
        "strategy": strategy,
        "gate": gate,
        "governor": gov.label,
        "max_trades": gov.max_trades,
        "dd_limit_pct": gov.dd_limit_pct,
        "max_concurrent": gov.max_concurrent,
        "capital": capital,
        "n_available": run.n_available,
        "n_trades": n_trades,
        "participation": participation,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "sharpe": sharpe,
        "sharpe_oos": sharpe_oos,
        "net_pnl": net_pnl,
        "return_on_margin": return_on_margin,
        "rom_oos": rom_oos,
        "return_on_capital": return_on_capital,
        "cagr": cagr,
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd_pct,
        "mean_span": mean_span,
        "halt_reason": run.halt_reason,
    }
    metrics["go_no_go"] = go_no_go(metrics, capital=capital)
    return metrics


# ---------------------------------------------------------------------------
# 4. Base sequence (the engine call) + per-combo evaluation
# ---------------------------------------------------------------------------
def base_sequence(
    strategy: str,
    cycles: list[dict[str, Any]],
    *,
    gate: str = "vol",
    capital: float = DEFAULT_CAPITAL,
    params: Optional[dict[str, Any]] = None,
    slip_pct: float = 0.005,
) -> tuple[list[float], list[float]]:
    """Run ``strategy`` over ``cycles`` ONCE and return the chronological
    ``(net_pnls, spans)`` sequence the governor operates on.

    Calls ``run_strategy_backtest`` (engine) — trades come back already sorted by
    ``entry_date``.  This is the ONLY engine invocation; the whole grid replays the
    governor over this single sequence (the governor never re-prices anything).
    """
    if strategy not in FNO_STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; choices: {sorted(FNO_STRATEGIES)}")
    spec = FNO_STRATEGIES[strategy]
    m = run_strategy_backtest(
        spec, cycles, params, capital=capital, slip_pct=slip_pct, gate=gate,
    )
    trades = m.get("trades", [])  # already chronological (engine sorts)
    pnls = [t.net_pnl for t in trades]
    spans = [t.span for t in trades]
    return pnls, spans


def evaluate_combo(
    pnls: list[float],
    spans: list[float],
    gov: GovernorParams,
    *,
    strategy: str,
    gate: str,
) -> dict[str, Any]:
    """Apply the governor to the base sequence and compute the combo's metrics."""
    run = apply_governor(pnls, spans, gov)
    return metrics_for_combo(run, gov, strategy=strategy, gate=gate)


# ---------------------------------------------------------------------------
# 5. The grid + sweep
# ---------------------------------------------------------------------------
def default_grid(capital: float = DEFAULT_CAPITAL) -> list[GovernorParams]:
    """A ~17-combo sweep: max_trades ∈ {None,250,200,150,100,50} × dd ∈ {None,10,20,30},
    trimmed to sensible pairs, plus the explicit no-governor baseline first.

    The baseline (mt=∞, dd=off) is always element 0.  The remaining combos are the
    cross-product with the redundant (∞,off) duplicate removed.
    """
    baseline = GovernorParams(capital=capital, max_trades=None, dd_limit_pct=None)
    max_trades_opts: list[Optional[int]] = [None, 250, 200, 150, 100, 50]
    dd_opts: list[Optional[float]] = [None, 10.0, 20.0, 30.0]

    grid: list[GovernorParams] = [baseline]
    for mt in max_trades_opts:
        for dd in dd_opts:
            if mt is None and dd is None:
                continue  # that's the baseline, already added
            grid.append(GovernorParams(capital=capital, max_trades=mt, dd_limit_pct=dd))
    return grid


def run_sweep(
    strategy: str,
    cycles: list[dict[str, Any]],
    *,
    gate: str = "vol",
    capital: float = DEFAULT_CAPITAL,
    grid: Optional[list[GovernorParams]] = None,
    params: Optional[dict[str, Any]] = None,
    slip_pct: float = 0.005,
) -> dict[str, Any]:
    """Run the full governor sweep over ONE engine backtest of ``strategy``.

    Returns ``{"strategy","gate","capital","baseline",(metrics),"rows":[metrics,...]}``
    where ``rows`` is RANKED by OOS ROM (desc), then full Sharpe (desc).  The baseline
    (no governor) is also returned separately for easy diffing.
    """
    pnls, spans = base_sequence(
        strategy, cycles, gate=gate, capital=capital, params=params, slip_pct=slip_pct,
    )
    grid = grid or default_grid(capital)

    rows = [
        evaluate_combo(pnls, spans, gov, strategy=strategy, gate=gate)
        for gov in grid
    ]
    # Baseline = the unlimited / dd-off combo.
    baseline = next(
        (r for r in rows if r["max_trades"] is None and r["dd_limit_pct"] is None),
        rows[0] if rows else None,
    )
    ranked = sorted(rows, key=lambda r: (r["rom_oos"], r["sharpe"]), reverse=True)
    return {
        "strategy": strategy,
        "gate": gate,
        "capital": capital,
        "n_available": len(pnls),
        "baseline": baseline,
        "rows": ranked,
    }


# ---------------------------------------------------------------------------
# 6. Rendering
# ---------------------------------------------------------------------------
def _fmt_pct(x: float) -> str:
    return f"{x:>8.2%}"


def render_sweep(sweep: dict[str, Any]) -> str:
    """Human-readable RANKED table (by OOS ROM) + a baseline line + caveats."""
    lines: list[str] = []
    lines.append(
        f"F&O RISK-GOVERNOR SWEEP — strategy={sweep['strategy']} gate={sweep['gate']} "
        f"capital=₹{sweep['capital']:,.0f} n_available={sweep['n_available']}"
    )
    lines.append(f"caveats: {CAVEATS}")
    lines.append("")

    hdr = (
        f"{'governor':<18} {'n':>4} {'part':>6} {'net':>12} {'ROM':>8} {'ROM_oos':>8} "
        f"{'ROC':>8} {'CAGR':>8} {'win':>6} {'sharpe':>7} {'sh_oos':>7} "
        f"{'maxDD':>12} {'DD%':>7} {'GO':>6}"
    )
    lines.append(hdr)
    lines.append("-" * len(hdr))

    def _row(m: dict[str, Any]) -> str:
        go = "GO" if m["go_no_go"][0] else "NO-GO"
        return (
            f"{m['governor']:<18} {m['n_trades']:>4} {m['participation']:>6.1%} "
            f"{m['net_pnl']:>12,.0f} {_fmt_pct(m['return_on_margin'])} "
            f"{_fmt_pct(m['rom_oos'])} {_fmt_pct(m['return_on_capital'])} "
            f"{_fmt_pct(m['cagr'])} {m['win_rate']:>5.1%} {m['sharpe']:>7.2f} "
            f"{m['sharpe_oos']:>7.2f} {m['max_drawdown']:>12,.0f} "
            f"{m['max_drawdown_pct']:>6.1%} {go:>6}"
        )

    for m in sweep["rows"]:
        lines.append(_row(m))
    lines.append("-" * len(hdr))

    base = sweep.get("baseline")
    if base:
        lines.append("baseline (no governor):")
        lines.append(_row(base))
        if base.get("halt_reason"):
            lines.append(f"  baseline halt: {base['halt_reason']}")

    # Top combo callout.
    if sweep["rows"]:
        top = sweep["rows"][0]
        lines.append("")
        lines.append(
            f"TOP by ROM_oos: {top['governor']} → ROM_oos {top['rom_oos']:.2%} "
            f"sharpe_oos {top['sharpe_oos']:.2f} maxDD {top['max_drawdown_pct']:.1%} "
            f"({'GO' if top['go_no_go'][0] else 'NO-GO'})"
        )
        if top.get("halt_reason"):
            lines.append(f"  top halt: {top['halt_reason']}")
    lines.append("")
    lines.append(
        "VERDICT (PRELIMINARY): a governor that beats the baseline ROM_oos WITHOUT "
        "cutting GO is the candidate; real-IV / FSP forward paper is the truth test. "
        "PAPER only."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7. S3 archival (fail-open, bucket from env — mirrors fno_orchestrator)
# ---------------------------------------------------------------------------
def _sweep_to_json(sweep: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe sweep dict (tuple go_no_go → list; ∞ profit_factor → None)."""
    def _clean(m: dict[str, Any]) -> dict[str, Any]:
        out = dict(m)
        gng = out.get("go_no_go")
        if isinstance(gng, tuple):
            out["go_no_go"] = [gng[0], gng[1]]
        if out.get("profit_factor") == float("inf"):
            out["profit_factor"] = None
        return out

    return {
        "strategy": sweep["strategy"],
        "gate": sweep["gate"],
        "capital": sweep["capital"],
        "n_available": sweep["n_available"],
        "baseline": _clean(sweep["baseline"]) if sweep.get("baseline") else None,
        "rows": [_clean(m) for m in sweep["rows"]],
    }


def archive_s3(
    sweep: dict[str, Any],
    *,
    args: dict[str, Any],
    summary_text: str,
) -> Optional[str]:  # pragma: no cover — needs network/boto3
    """Write results.json + summary.txt + manifest.json under
    ``s3://<S3_BUCKET>/kronos/m3/risk_sweep/<strategy>/<ts>/``.

    Fail-open: a missing bucket or any S3 error logs + warns but never crashes the run
    (the stdout table is the primary deliverable).  Bucket resolves from ``S3_BUCKET``.
    """
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        print("WARNING: --upload-s3 set but S3_BUCKET is empty — skipping upload.")
        return None
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    prefix = f"kronos/m3/risk_sweep/{sweep['strategy']}/{run_ts}"
    manifest = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "args": args,
        "caveats": CAVEATS.split(" · "),
        "decision": "PRELIMINARY",
    }
    try:
        import boto3

        s3 = boto3.client("s3")
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
        print(f"Archived risk-sweep results to {uri}")
        return uri
    except Exception as exc:  # noqa: BLE001 — fail-open
        print(f"WARNING: S3 archival failed ({exc}) — results NOT uploaded.")
        return None


# ---------------------------------------------------------------------------
# 8. CLI (DB-init only after arg-parse so --help never touches it)
# ---------------------------------------------------------------------------
def main() -> None:  # pragma: no cover — needs the dhan_trading DB
    """``python -m research.backtest.fno_risk_sweep --strategy iron_condor --capital 500000``."""
    parser = argparse.ArgumentParser(
        description="F&O portfolio risk-governor sweep (max-trades × drawdown-limit)",
    )
    parser.add_argument(
        "--strategy", default="iron_condor", choices=sorted(FNO_STRATEGIES),
        help="strategy to govern (default iron_condor — the validated edge)",
    )
    parser.add_argument(
        "--gate", default="vol", choices=["vol", "none"],
        help="vol = vol-regime gated (default) · none = ungated baseline",
    )
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument(
        "--mode", default="weekly", choices=["weekly", "expiry_calendar"],
        help="cycle-boundary mode passed to cycles_from_db",
    )
    parser.add_argument("--slip-pct", type=float, default=0.005)
    parser.add_argument(
        "--param", action="append", metavar="KEY=VAL", default=[],
        help="extra builder params key=val (repeatable; float-if-possible)",
    )
    parser.add_argument(
        "--upload-s3", action="store_true", dest="upload_s3",
        help="archive results.json + summary.txt to s3://$S3_BUCKET/kronos/m3/risk_sweep/",
    )
    args = parser.parse_args()

    extra: dict[str, Any] = {}
    for kv in args.param:
        kk, _, vv = kv.partition("=")
        try:
            extra[kk.strip()] = float(vv)
        except ValueError:
            extra[kk.strip()] = vv

    # DB-init only after arg-parse so --help never touches it (mirrors fno_strategies.main).
    from config import get_config
    from db import init_db

    init_db(get_config().db_url)
    cycles = cycles_from_db(mode=args.mode)

    sweep = run_sweep(
        args.strategy, cycles, gate=args.gate, capital=args.capital,
        params=extra or None, slip_pct=args.slip_pct,
    )
    summary_text = render_sweep(sweep)
    print(summary_text)

    if args.upload_s3:
        archive_s3(sweep, args=vars(args), summary_text=summary_text or "(see stdout)")


if __name__ == "__main__":  # pragma: no cover
    main()
