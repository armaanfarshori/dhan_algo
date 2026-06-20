"""
Honest edge-restatement for the F&O iron-condor (α / β / γ + falsification gates).

PURPOSE
-------
The condor backtest reports a headline ROM. This module asks the harder,
project-mandated question (`orchestrator_specs/09_validation_rigor.md`): *is that
ROM an edge, or is it short-vol beta, passive premium capture, regime luck, or an
overfit/proxy artifact?* It takes the condor's per-cycle P&L / ROM series and
produces an HONEST edge statement broken into three lenses plus a battery of
falsification gates:

  β (factor)  — regress condor cycle returns on a short-vol / VRP factor (e.g.
                −Δimplied-vol, or the VRP spread). If the "edge" is mostly
                short-vol beta, the alpha-intercept is small and R² is high.
  α (excess)  — excess return versus a PASSIVE always-on condor benchmark (same
                strikes, no gating) AND versus a random-entry benchmark. Does the
                gating / selection add α beyond just being short premium? (the bar
                the orchestrator MVP failed.)
  γ / tail    — gamma / tail metrics: worst-cycle loss, CVaR(5%), max drawdown,
                % cycles at max-loss, skew / kurtosis of the P&L distribution.

  Falsification gates (from 09_validation_rigor §2/§3/§5):
    * walk-forward OOS (chronological 70/30 + purge/embargo)
    * deflated Sharpe (haircut for the number of trials / configs tried)
    * IV-haircut survival (re-run at 0.80× IV — provided by the caller)
    * beat-passive-AND-random by more than a block-bootstrap CI band
    * regime / stress-tercile non-catastrophic
  Each gate emits GO / NO-GO; an overall honest verdict string is assembled.

LANE DISCIPLINE
---------------
This module is PURE and TESTABLE: no DB, no network, no model. It is fed
per-cycle series (P&L, ROM, the short-vol factor, IV, regime label) by the
caller — the condor's ``run_backtest`` trades, or a synthetic series in tests.
It REUSES the engine metric helpers (``_sharpe_from_pnls`` / ``_max_drawdown``
from ``fno_strategies``) and never re-implements Sharpe / drawdown / pricing /
cost math. Statistics here (OLS, CVaR, skew/kurtosis, bootstrap, deflated Sharpe)
are math-only — no scipy / numpy dependency, matching the rest of the harness.

HONESTY LEDGER (inherited verbatim — never dropped)
---------------------------------------------------
VIX-as-weekly-IV proxy · CLOSE-not-FSP settlement · EXPIRY-ONLY (tail-blind for
undefined-risk; defined-risk loss is wing-capped) · single-σ Black-76 · small
sample (n ≈ 166–233 weekly cycles) → wide CIs · selection / config trials
inflate Sharpe → deflate it · IV source is the VIX proxy unless real ATM IV is
supplied → flag PRELIMINARY when VIX-proxy · PAPER / research only.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Reused surface — import, never re-implement (lane discipline)
# ---------------------------------------------------------------------------
from research.backtest.fno_condor import (
    IV_SOURCE_REAL,
    IV_SOURCE_VIX_PROXY,
)
from research.backtest.fno_strategies import _max_drawdown, _sharpe_from_pnls

logger = logging.getLogger("dhan.backtest.fno_edge_restate")

WEEKS_PER_YEAR = 52.0
CVAR_ALPHA_DEFAULT = 0.05          # 5% tail for CVaR
DEFAULT_BOOTSTRAP_SEEDS = 1000
DEFAULT_BLOCK_LEN = 5              # block-bootstrap block (vol autocorrelation)
EULER_GAMMA = 0.5772156649015329  # for the deflated-Sharpe expected-max-Sharpe term

# Verdict tokens.
GO = "GO"
NO_GO = "NO-GO"

CAVEATS = (
    "VIX-as-weekly-IV proxy · CLOSE-not-FSP settlement · EXPIRY-ONLY (tail-blind) · "
    "single-σ B76 · small sample → wide CIs · selection/config trials inflate "
    "Sharpe → deflated · PRELIMINARY when VIX-proxy · PAPER only"
)


# ===========================================================================
# Input container
# ===========================================================================
@dataclass(frozen=True)
class CycleSeries:
    """Per-cycle inputs for the edge restatement (one entry per traded cycle).

    All list fields are parallel (index i = cycle i), ordered CHRONOLOGICALLY by
    entry date (the walk-forward split relies on this order). The caller builds
    this from the condor's ``run_backtest`` trades, or synthetically in tests.

    Attributes
    ----------
    pnl:
        Net P&L per cycle (₹). The headline return series.
    margin:
        Per-cycle SPAN/defined-risk margin (₹, the condor ``max_loss``). Used to
        form margin-weighted ROM (Σpnl/Σmargin) — never the mean of per-cycle
        ROMs (R3 in 09_validation_rigor).
    short_vol_factor:
        The short-vol / VRP factor for the β regression, one value per cycle —
        e.g. −Δimplied-vol over the cycle, or the VRP spread (implied − realized).
        ``None`` (or an all-equal series) → β regression is skipped / degenerate.
    passive_pnl:
        Net P&L per cycle of the PASSIVE always-on condor (same strikes, NO gate)
        on the SAME cycles. The α benchmark. ``None`` → α-vs-passive skipped.
    regime:
        Optional per-cycle regime label (e.g. "calm"/"mid"/"stress" by VIX
        tercile) for the regime-robustness gate. ``None`` → regime gate skipped.
    iv_source:
        Per-cycle IV provenance (``IV_SOURCE_REAL`` / ``IV_SOURCE_VIX_PROXY``).
        Drives the PRELIMINARY flag. Empty → assumed VIX proxy (conservative).
    capital:
        Fixed book capital (₹) for return-on-book / DD-on-book honesty metrics.
    n_configs_tried:
        Number of configs / strategies tried before quoting this result (for the
        deflated-Sharpe trials haircut). Default 1 (no selection).
    """

    pnl: list[float]
    margin: list[float]
    short_vol_factor: list[float] | None = None
    passive_pnl: list[float] | None = None
    regime: list[str] | None = None
    iv_source: list[str] = field(default_factory=list)
    capital: float = 200_000.0
    n_configs_tried: int = 1

    def __post_init__(self) -> None:
        n = len(self.pnl)
        if len(self.margin) != n:
            raise ValueError(
                f"CycleSeries: margin length {len(self.margin)} != pnl length {n}"
            )
        for name, seq in (
            ("short_vol_factor", self.short_vol_factor),
            ("passive_pnl", self.passive_pnl),
            ("regime", self.regime),
        ):
            if seq is not None and len(seq) != n:
                raise ValueError(
                    f"CycleSeries: {name} length {len(seq)} != pnl length {n}"
                )
        if self.iv_source and len(self.iv_source) != n:
            raise ValueError(
                f"CycleSeries: iv_source length {len(self.iv_source)} != pnl length {n}"
            )
        if self.n_configs_tried < 1:
            raise ValueError("CycleSeries: n_configs_tried must be >= 1")
        # Finiteness: a NaN/inf in pnl/margin would silently poison every metric
        # (NaN compares False against every gate → quiet NO-GO; inf blows up ROM).
        # Reject at construction so a bad upstream value can't masquerade as a
        # NO-GO verdict.
        if any(not math.isfinite(p) for p in self.pnl):
            raise ValueError("CycleSeries: pnl contains non-finite (NaN/inf) values")
        if any(not math.isfinite(m) for m in self.margin):
            raise ValueError("CycleSeries: margin contains non-finite (NaN/inf) values")
        # Margins are defined-risk capital at risk: they must be non-negative
        # (0 = a stand-aside cycle that contributes no ROM denominator).
        if any(m < 0 for m in self.margin):
            raise ValueError("CycleSeries: margin contains negative values")

    @property
    def n(self) -> int:
        return len(self.pnl)


# ===========================================================================
# β — short-vol / VRP factor regression (OLS, math-only)
# ===========================================================================
def ols_regression(y: list[float], x: list[float]) -> dict[str, float]:
    """Simple OLS of ``y`` on ``x`` (single factor + intercept), math-only.

    Returns ``{"beta", "alpha", "r_squared", "n"}`` where ``y ≈ alpha + beta·x``.

    Degenerate cases (n < 2, or zero variance in x) → beta=0, alpha=mean(y),
    r_squared=0. This makes the β-gate honest: a factor with no spread can never
    "explain" the returns.
    """
    n = len(y)
    if n != len(x):
        raise ValueError(f"ols_regression: len(y)={n} != len(x)={len(x)}")
    if n < 2:
        return {"beta": 0.0, "alpha": (y[0] if n else 0.0), "r_squared": 0.0, "n": float(n)}

    mean_x = sum(x) / n
    mean_y = sum(y) / n
    sxx = sum((xi - mean_x) ** 2 for xi in x)
    if sxx <= 1e-12:
        # No variance in the factor → it explains nothing.
        return {"beta": 0.0, "alpha": mean_y, "r_squared": 0.0, "n": float(n)}

    sxy = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    beta = sxy / sxx
    alpha = mean_y - beta * mean_x

    # R² = 1 - SS_res / SS_tot
    ss_tot = sum((yi - mean_y) ** 2 for yi in y)
    ss_res = sum((yi - (alpha + beta * xi)) ** 2 for yi, xi in zip(y, x))
    r_squared = 0.0 if ss_tot <= 1e-12 else max(0.0, 1.0 - ss_res / ss_tot)
    return {"beta": beta, "alpha": alpha, "r_squared": r_squared, "n": float(n)}


def beta_analysis(series: CycleSeries) -> dict[str, Any]:
    """β lens: regress condor cycle P&L on the short-vol / VRP factor.

    Reports beta, alpha-intercept (the per-cycle return AFTER removing the factor
    loading — the candidate "real" edge), R² (how much of the return the factor
    explains), and an interpretation flag ``edge_is_mostly_beta`` (R² high AND the
    alpha-intercept small relative to mean P&L).

    When no factor is supplied / the factor has no spread, returns
    ``available=False`` and the analysis is skipped (no false β claim).
    """
    factor = series.short_vol_factor
    if factor is None:
        return {"available": False, "reason": "no short_vol_factor supplied"}

    reg = ols_regression(series.pnl, factor)
    mean_pnl = sum(series.pnl) / series.n if series.n else 0.0
    # "Edge is mostly beta" heuristic: the factor explains most of the variance
    # (R² >= 0.5) AND the residual intercept is small vs the mean return
    # (|alpha| < 0.25·|mean_pnl|). Informational, not a hard gate.
    if mean_pnl != 0.0:
        edge_is_mostly_beta = (
            reg["r_squared"] >= 0.5 and abs(reg["alpha"]) < 0.25 * abs(mean_pnl)
        )
    else:
        edge_is_mostly_beta = reg["r_squared"] >= 0.5
    return {
        "available": True,
        "beta": reg["beta"],
        "alpha_intercept": reg["alpha"],
        "r_squared": reg["r_squared"],
        "mean_pnl": mean_pnl,
        "edge_is_mostly_beta": bool(edge_is_mostly_beta),
    }


# ===========================================================================
# α — excess return vs passive + random benchmarks
# ===========================================================================
def _margin_weighted_rom(pnls: list[float], margins: list[float]) -> float:
    """Σ pnl / Σ margin (NOT the mean of per-cycle ROMs — R3). 0.0 if no margin."""
    total_margin = sum(margins)
    if total_margin <= 0:
        return 0.0
    return sum(pnls) / total_margin


def random_entry_benchmark(
    series: CycleSeries,
    *,
    take_fraction: float = 0.7,
    seeds: int = 100,
    rng_seed: int = 12345,
) -> dict[str, float]:
    """Random-entry benchmark: the "monkey" that books a random subset of cycles
    with no selection skill.

    Population valued for the benchmark:
      * When ``series.passive_pnl`` is supplied, each cycle's P&L is the PASSIVE
        (ungated, same-strikes) outcome — so the monkey earns the always-on
        short-premium return on a random subset. This is the bar the gate's
        selection must beat to add α (§2c). NOTE: the population is still the
        per-cycle series the caller supplied (one entry per row), valued passively
        — it is NOT a superset that re-introduces cycles dropped from the series.
        Building the series so that stand-aside cycles are present (with their
        passive P&L) is the CALLER's job (see ``series_from_trades``); only then
        does this test true selection rather than just re-ordering.
      * When no passive series is present the population is the gated series itself,
        so the benchmark only probes timing/ordering of the SAME cycles, not
        selection — the caller flags this ("passive UNCHECKED").

    On each seed it draws a random ``take_fraction`` of the population (without
    replacement) and books its margin-weighted ROM (margins are the shared per-row
    defined-risk margin). Returns the DISTRIBUTION over ``seeds`` draws as
    ``{"mean", "p95", "p05"}``; the selection layer must beat the 95th percentile.
    Seeded → deterministic. ``take_fraction`` is clamped to (0, 1].
    """
    n = series.n
    if n == 0:
        return {"mean": 0.0, "p95": 0.0, "p05": 0.0}
    # Population to sample from: the ungated universe if available, else self.
    pop_pnl = series.passive_pnl if series.passive_pnl is not None else series.pnl
    pop_margin = series.margin  # shared per-row defined-risk margin
    # Clamp take_fraction into (0, 1] so rng.sample can never over-draw the
    # population (Sample-larger-than-population ValueError) or under-draw to 0.
    take_fraction = min(max(take_fraction, 1e-9), 1.0)
    take = max(1, min(n, int(round(take_fraction * n))))
    rng = random.Random(rng_seed)
    roms: list[float] = []
    idx = list(range(n))
    for _ in range(seeds):
        chosen = rng.sample(idx, take)
        pnls = [pop_pnl[i] for i in chosen]
        margins = [pop_margin[i] for i in chosen]
        roms.append(_margin_weighted_rom(pnls, margins))
    roms.sort()
    return {
        "mean": sum(roms) / len(roms),
        "p95": _percentile(roms, 0.95),
        "p05": _percentile(roms, 0.05),
    }


def alpha_analysis(series: CycleSeries, **random_kwargs: Any) -> dict[str, Any]:
    """α lens: gated condor ROM vs (1) passive always-on condor, (2) random entry.

    ``alpha_vs_passive`` = gated margin-weighted ROM − passive margin-weighted ROM
    (on the SAME cycles; passive uses NO gate, so it includes the stand-aside
    cycles' premium too). A positive value means the gate's SELECTION added return
    beyond just being short premium. ``alpha_vs_random`` compares to the random
    benchmark's mean and 95th percentile.
    """
    gated_rom = _margin_weighted_rom(series.pnl, series.margin)
    out: dict[str, Any] = {"gated_rom": gated_rom}

    if series.passive_pnl is not None:
        passive_rom = _margin_weighted_rom(series.passive_pnl, series.margin)
        out["passive_rom"] = passive_rom
        out["alpha_vs_passive"] = gated_rom - passive_rom
        out["beats_passive"] = gated_rom > passive_rom
    else:
        out["passive_rom"] = None
        out["alpha_vs_passive"] = None
        out["beats_passive"] = None

    rand = random_entry_benchmark(series, **random_kwargs)
    out["random_rom_mean"] = rand["mean"]
    out["random_rom_p95"] = rand["p95"]
    out["alpha_vs_random_mean"] = gated_rom - rand["mean"]
    out["beats_random_p95"] = gated_rom > rand["p95"]
    return out


# ===========================================================================
# γ / tail — worst-cycle, CVaR, drawdown, max-loss %, skew, kurtosis
# ===========================================================================
def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile of an ALREADY-SORTED ascending list.

    ``q`` in [0, 1]. Empty → 0.0. Matches numpy's default 'linear' method so the
    bootstrap CIs read intuitively.
    """
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_vals[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def cvar(pnls: list[float], alpha: float = CVAR_ALPHA_DEFAULT) -> float:
    """Conditional Value-at-Risk: the MEAN of the worst ``alpha`` fraction of P&L.

    Returns a value in P&L units (NEGATIVE for a loss tail). Defined as the
    average of the lowest ceil(alpha·n) outcomes — the expected loss conditional
    on being in the tail. Empty → 0.0. ``alpha`` is clamped to (0, 1].
    """
    n = len(pnls)
    if n == 0:
        return 0.0
    alpha = min(max(alpha, 1e-9), 1.0)
    k = max(1, int(math.ceil(alpha * n)))
    worst = sorted(pnls)[:k]
    return sum(worst) / len(worst)


def _moments(pnls: list[float]) -> tuple[float, float, float, float]:
    """Return (mean, std_sample, skew, excess_kurtosis), math-only.

    Skew / kurtosis use the population (biased) moment definitions about the mean,
    standardised by the population std — the standard descriptive form. Excess
    kurtosis subtracts 3 (so a normal distribution reads ~0). n < 2 → zeros;
    near-zero variance → skew/kurtosis 0 (undefined, reported as flat).
    """
    n = len(pnls)
    if n < 2:
        return (pnls[0] if n else 0.0), 0.0, 0.0, 0.0
    mean = sum(pnls) / n
    var_pop = sum((p - mean) ** 2 for p in pnls) / n
    var_sample = sum((p - mean) ** 2 for p in pnls) / (n - 1)
    std_sample = math.sqrt(var_sample)
    if var_pop <= 1e-18:
        return mean, std_sample, 0.0, 0.0
    sd_pop = math.sqrt(var_pop)
    m3 = sum((p - mean) ** 3 for p in pnls) / n
    m4 = sum((p - mean) ** 4 for p in pnls) / n
    skew = m3 / (sd_pop ** 3)
    kurt_excess = m4 / (sd_pop ** 4) - 3.0
    return mean, std_sample, skew, kurt_excess


def tail_analysis(
    series: CycleSeries,
    *,
    alpha: float = CVAR_ALPHA_DEFAULT,
    max_loss_tol: float = 0.98,
) -> dict[str, Any]:
    """γ / tail lens on the per-cycle P&L distribution.

    Reports:
      * ``worst_cycle``       — most negative single-cycle P&L.
      * ``cvar``              — mean of the worst ``alpha`` fraction (CVaR_alpha).
      * ``max_drawdown``      — peak-to-trough on cumulative P&L (engine helper).
      * ``pct_at_max_loss``   — fraction of cycles whose loss is ≥ ``max_loss_tol``
                                × that cycle's defined-risk margin (a tail-blowout
                                proxy for a defined-risk book).
      * ``skew`` / ``kurtosis`` (excess) of the P&L distribution.
    """
    pnls = series.pnl
    if not pnls:
        return {
            "worst_cycle": 0.0, "cvar": 0.0, "max_drawdown": 0.0,
            "pct_at_max_loss": 0.0, "skew": 0.0, "kurtosis": 0.0, "n": 0,
        }
    _mean, _std, skew, kurt = _moments(pnls)
    # % of cycles realising (near) their full defined-risk loss. A short condor's
    # max loss is -(margin) at expiry; a cycle is "at max loss" when pnl <=
    # -max_loss_tol·margin (margin>0). Margin 0 → that cycle can't be at max loss.
    n_at_max = 0
    for p, m in zip(pnls, series.margin):
        if m > 0 and p <= -max_loss_tol * m:
            n_at_max += 1
    return {
        "worst_cycle": min(pnls),
        "cvar": cvar(pnls, alpha=alpha),
        "cvar_alpha": alpha,
        "max_drawdown": _max_drawdown(pnls),
        "pct_at_max_loss": n_at_max / len(pnls),
        "skew": skew,
        "kurtosis": kurt,
        "n": len(pnls),
    }


# ===========================================================================
# Deflated Sharpe (trials haircut)
# ===========================================================================
def deflated_sharpe(
    sharpe: float,
    n_trials: int,
    n_obs: int,
    *,
    skew: float = 0.0,
    kurtosis: float = 0.0,
) -> dict[str, float]:
    """Deflated Sharpe Ratio (Bailey & López de Prado), math-only.

    Adjusts an observed (annualised) Sharpe for (a) the number of independent
    trials/configs tried (selection inflates the max Sharpe), and (b) the sample
    length + non-normality of returns. Returns the probability that the TRUE
    Sharpe exceeds the expected-max-under-the-null benchmark.

    Steps:
      1. Expected maximum Sharpe under the null of N independent zero-Sharpe trials
         (per-period units): SR0 ≈ √(Var) · ((1-γ)·Φ⁻¹(1-1/N) + γ·Φ⁻¹(1-1/(N·e)))
         with Var of the trials' Sharpe taken as 1/n_obs (the standard assumption
         when the per-trial Sharpe variance is unknown).
      2. DSR = Φ( ((SR_obs - SR0)·√(n-1)) / √(1 - skew·SR_obs + (κ̄-1)/4·SR_obs²) )
         where SR_obs / SR0 are in PER-PERIOD units and κ̄ is the RAW (non-excess)
         kurtosis (Gaussian = 3). The ``kurtosis`` argument here is EXCESS kurtosis
         (Gaussian = 0, the convention of :func:`_moments`), so the denominator
         uses ``(kurtosis + 3 - 1)/4 = (kurtosis + 2)/4`` to convert excess→raw.

    The annualised input ``sharpe`` is de-annualised by √WEEKS_PER_YEAR (weekly
    cycles). ``kurtosis`` is EXCESS kurtosis (Gaussian = 0). Returns
    ``{"dsr", "sr0_annual", "n_trials", "n_obs"}``. DSR near 1 = the edge survives
    the trials haircut; near 0 = it's likely selection noise.
    """
    if n_obs < 2:
        return {
            "dsr": 0.0, "sr0_annual": 0.0, "sr_obs_annual": sharpe,
            "n_trials": float(max(1, n_trials)), "n_obs": float(n_obs),
        }
    n_trials = max(1, n_trials)
    # De-annualise (weekly).
    sr_obs = sharpe / math.sqrt(WEEKS_PER_YEAR)

    # Expected max Sharpe under the null. Variance of trial Sharpes ≈ 1/n_obs.
    sigma_sr = math.sqrt(1.0 / n_obs)
    if n_trials == 1:
        sr0 = 0.0
    else:
        z1 = _norm_ppf(1.0 - 1.0 / n_trials)
        z2 = _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
        sr0 = sigma_sr * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)

    # DSR denominator (non-normality adjustment). ``kurtosis`` is EXCESS kurtosis
    # (Gaussian=0); B&LdP's formula uses RAW kurtosis κ̄ (Gaussian=3) as (κ̄-1)/4,
    # so excess→raw gives (kurtosis + 3 - 1)/4 = (kurtosis + 2)/4. Guard against a
    # negative radicand (extreme skew/kurtosis × high SR).
    denom_inner = 1.0 - skew * sr_obs + ((kurtosis + 2.0) / 4.0) * sr_obs * sr_obs
    denom_inner = max(denom_inner, 1e-9)
    denom = math.sqrt(denom_inner)
    z = ((sr_obs - sr0) * math.sqrt(n_obs - 1)) / denom
    dsr = _norm_cdf(z)
    return {
        "dsr": dsr,
        "sr0_annual": sr0 * math.sqrt(WEEKS_PER_YEAR),
        "sr_obs_annual": sharpe,
        "n_trials": float(n_trials),
        "n_obs": float(n_obs),
    }


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation), math-only.

    Accurate to ~1e-9 over (0,1). Clamps p into (0,1) to avoid ±inf. Used for the
    expected-max-Sharpe order statistic in :func:`deflated_sharpe`.
    """
    p = min(max(p, 1e-12), 1.0 - 1e-12)
    # Coefficients (Peter Acklam).
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow = 0.02425
    phigh = 1.0 - plow
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
        ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)


# ===========================================================================
# Walk-forward OOS (chronological 70/30 + purge/embargo)
# ===========================================================================
def walk_forward_oos(
    series: CycleSeries,
    *,
    oos_frac: float = 0.30,
    purge: int = 0,
) -> dict[str, Any]:
    """Chronological 70/30 split with a ``purge`` (embargo) gap each side.

    Returns OOS margin-weighted ROM, Sharpe, max-DD, n_oos plus the IS/OOS index
    boundaries — mirrors ``fno_strike_sweep.walk_forward_split`` so the OOS number
    is computed identically across the harness. Series MUST be chronological.
    """
    n = series.n
    split_idx = max(1, int((1.0 - oos_frac) * n)) if n else 0
    is_end = max(0, split_idx - purge)
    oos_start = min(n, split_idx + purge)
    pnls_oos = series.pnl[oos_start:]
    margins_oos = series.margin[oos_start:]
    return {
        "is_end": is_end,
        "oos_start": oos_start,
        "n_oos": len(pnls_oos),
        "rom_oos": _margin_weighted_rom(pnls_oos, margins_oos),
        "sharpe_oos": _sharpe_from_pnls(pnls_oos) if len(pnls_oos) >= 2 else 0.0,
        "max_drawdown_oos": _max_drawdown(pnls_oos),
        "net_oos": sum(pnls_oos),
    }


# ===========================================================================
# Block bootstrap CI over the P&L series
# ===========================================================================
def block_bootstrap_rom_ci(
    series: CycleSeries,
    *,
    block_len: int = DEFAULT_BLOCK_LEN,
    seeds: int = DEFAULT_BOOTSTRAP_SEEDS,
    rng_seed: int = 999,
    ci: float = 0.95,
) -> dict[str, float]:
    """Block-bootstrap 95% CI for margin-weighted ROM (respects vol autocorr).

    Resamples contiguous blocks of length ``block_len`` (with replacement) until
    the resample length ≈ n, recomputes margin-weighted ROM, and returns the
    point estimate + the [lo, hi] CI band. Block resampling preserves short-run
    autocorrelation (vol clusters) that an iid bootstrap would destroy. Seeded.
    """
    n = series.n
    point = _margin_weighted_rom(series.pnl, series.margin)
    if n < 2:
        return {"point": point, "lo": point, "hi": point, "half_width": 0.0, "ci": ci}
    block_len = max(1, min(block_len, n))
    n_blocks = int(math.ceil(n / block_len))
    rng = random.Random(rng_seed)
    roms: list[float] = []
    max_start = n - block_len
    for _ in range(seeds):
        pnls: list[float] = []
        margins: list[float] = []
        for _b in range(n_blocks):
            start = rng.randint(0, max_start)
            pnls.extend(series.pnl[start:start + block_len])
            margins.extend(series.margin[start:start + block_len])
        roms.append(_margin_weighted_rom(pnls, margins))
    roms.sort()
    lo_q = (1.0 - ci) / 2.0
    hi_q = 1.0 - lo_q
    lo = _percentile(roms, lo_q)
    hi = _percentile(roms, hi_q)
    return {"point": point, "lo": lo, "hi": hi, "half_width": (hi - lo) / 2.0, "ci": ci}


# ===========================================================================
# Regime / stress-tercile robustness
# ===========================================================================
def regime_breakdown(series: CycleSeries) -> dict[str, Any]:
    """Per-regime margin-weighted ROM + worst-cycle, for the stress-robustness gate.

    Groups cycles by their ``regime`` label and reports each group's ROM, n, and
    worst-cycle loss. ``available=False`` when no regime labels supplied. The gate
    (§5 R4) wants the STRESS tercile to be non-catastrophic — bounded, not a wipeout.
    """
    if series.regime is None:
        return {"available": False, "reason": "no regime labels supplied"}
    groups: dict[str, dict[str, Any]] = {}
    for p, m, r in zip(series.pnl, series.margin, series.regime):
        g = groups.setdefault(r, {"pnls": [], "margins": []})
        g["pnls"].append(p)
        g["margins"].append(m)
    out: dict[str, Any] = {"available": True, "regimes": {}}
    for r, g in groups.items():
        out["regimes"][r] = {
            "n": len(g["pnls"]),
            "rom": _margin_weighted_rom(g["pnls"], g["margins"]),
            "net_pnl": sum(g["pnls"]),
            "worst_cycle": min(g["pnls"]) if g["pnls"] else 0.0,
        }
    return out


# ===========================================================================
# Falsification gates
# ===========================================================================
def _gate(passed: bool, detail: str) -> dict[str, Any]:
    return {"verdict": GO if passed else NO_GO, "pass": bool(passed), "detail": detail}


def falsification_gates(
    series: CycleSeries,
    *,
    haircut_series: CycleSeries | None = None,
    min_oos_trades: int = 30,
    stress_regime: str = "stress",
    stress_rom_floor: float = -0.25,
    oos_frac: float = 0.30,
    purge: int = 0,
    block_len: int = DEFAULT_BLOCK_LEN,
    bootstrap_seeds: int = DEFAULT_BOOTSTRAP_SEEDS,
) -> dict[str, Any]:
    """Run every falsification gate and return per-gate GO/NO-GO + the evidence.

    Gates (each binary; from 09_validation_rigor §5):

      WF_OOS         — OOS margin-weighted ROM > 0 AND n_oos >= min_oos_trades AND
                       the FULL-sample block-bootstrap 95% CI lower bound on ROM is
                       > 0 (spec G2: "CI lower bound on net ROM > 0, not just the
                       point estimate") — a positive point estimate inside a CI that
                       straddles zero is NOT believed.
      DEFLATED_SHARPE— deflated Sharpe (trials haircut) DSR > 0.5 (true SR likely
                       beats the expected-max-under-null).
      IV_HAIRCUT     — at 0.80× IV (``haircut_series``, supplied by the caller),
                       OOS ROM still > 0. NO-GO (fail-closed) if absent.
      BEAT_PASSIVE_AND_RANDOM — gated full-sample ROM beats passive AND the random
                       p95 by MORE than the bootstrap CI half-width. NO-GO
                       (fail-closed) when no passive benchmark is supplied — the
                       always-on short-premium book is the bar the orchestrator MVP
                       actually failed, so a missing passive can never PASS.
      REGIME         — the stress tercile is non-catastrophic: stress margin-weighted
                       ROM > ``stress_rom_floor`` (default -0.25 ≈ bleeds at most 25%
                       of deployed stress margin in aggregate). NO-GO if no regime
                       labels / no stress cycles.

    ``stress_rom_floor`` sets the stress-tercile catastrophe bound (less negative =
    stricter). The -1.0 "lose all margin" bound is effectively unreachable for a
    defined-risk book, so the default is the meaningful -0.25.
    """
    gates: dict[str, dict[str, Any]] = {}

    # --- WF_OOS (+ bootstrap CI lower-bound > 0, spec G2) ---
    wf = walk_forward_oos(series, oos_frac=oos_frac, purge=purge)
    boot = block_bootstrap_rom_ci(series, block_len=block_len, seeds=bootstrap_seeds)
    wf_pass = (
        wf["rom_oos"] > 0
        and wf["n_oos"] >= min_oos_trades
        and boot["lo"] > 0
    )
    gates["WF_OOS"] = _gate(
        wf_pass,
        f"OOS ROM={wf['rom_oos']:.4f}, n_oos={wf['n_oos']}, "
        f"bootstrap ROM CI=[{boot['lo']:.4f},{boot['hi']:.4f}] "
        f"(need ROM>0 & n>={min_oos_trades} & CI_lo>0)",
    )

    # --- DEFLATED_SHARPE ---
    sharpe = _sharpe_from_pnls(series.pnl)
    _m, _s, skew, kurt = _moments(series.pnl)
    dsr = deflated_sharpe(
        sharpe, series.n_configs_tried, series.n, skew=skew, kurtosis=kurt,
    )
    dsr_pass = dsr["dsr"] > 0.5
    gates["DEFLATED_SHARPE"] = _gate(
        dsr_pass,
        f"DSR={dsr['dsr']:.3f} (sharpe={sharpe:.2f}, trials={series.n_configs_tried}, "
        f"n={series.n}; need DSR>0.5)",
    )

    # --- IV_HAIRCUT ---
    if haircut_series is not None:
        wf_hc = walk_forward_oos(haircut_series, oos_frac=oos_frac, purge=purge)
        hc_pass = wf_hc["rom_oos"] > 0
        gates["IV_HAIRCUT"] = _gate(
            hc_pass,
            f"0.80×IV OOS ROM={wf_hc['rom_oos']:.4f} (need >0)",
        )
    else:
        gates["IV_HAIRCUT"] = _gate(
            False,
            "no 0.80×IV haircut series supplied — survival UNPROVEN (treat as NO-GO "
            "until the haircut sweep is run)",
        )

    # --- BEAT_PASSIVE_AND_RANDOM (reuse the WF bootstrap CI half-width) ---
    alpha = alpha_analysis(series)
    hw = boot["half_width"]
    gated_rom = alpha["gated_rom"]
    if alpha["alpha_vs_passive"] is None:
        # No passive benchmark → fail-closed. The always-on short-premium book is
        # the bar the orchestrator MVP failed; a result that omits it can never
        # demonstrate selection value, so it cannot PASS (mirrors IV_HAIRCUT/REGIME).
        bpr_pass = False
        detail = (
            f"NO passive benchmark supplied — selection value UNPROVEN (fail-closed). "
            f"gated ROM={gated_rom:.4f}, random_p95={alpha['random_rom_p95']:.4f}, "
            f"CI half-width={hw:.4f}"
        )
    else:
        beats_passive = alpha["alpha_vs_passive"] > hw
        beats_random = (gated_rom - alpha["random_rom_p95"]) > hw
        bpr_pass = beats_passive and beats_random
        detail = (
            f"gated ROM={gated_rom:.4f}, passive={alpha['passive_rom']:.4f} "
            f"(α={alpha['alpha_vs_passive']:.4f}), random_p95="
            f"{alpha['random_rom_p95']:.4f}, CI half-width={hw:.4f}; "
            f"beats_passive={beats_passive} & beats_random={beats_random}"
        )
    gates["BEAT_PASSIVE_AND_RANDOM"] = _gate(bpr_pass, detail)

    # --- REGIME ---
    rb = regime_breakdown(series)
    if not rb.get("available"):
        gates["REGIME"] = _gate(
            False,
            "no regime labels supplied — stress-robustness UNPROVEN",
        )
    else:
        stress = rb["regimes"].get(stress_regime)
        if stress is None:
            gates["REGIME"] = _gate(
                False,
                f"no '{stress_regime}' regime cycles present — stress-robustness "
                f"UNPROVEN (regimes seen: {sorted(rb['regimes'])})",
            )
        else:
            # Non-catastrophic: aggregate stress ROM is bounded above the floor.
            # A full-margin wipeout (ROM -1.0) is effectively unreachable for a
            # defined-risk book, so the floor (default -0.25) is the meaningful
            # bound — the stress tercile may lose, but not bleed catastrophically.
            cat_pass = stress["rom"] > stress_rom_floor
            gates["REGIME"] = _gate(
                cat_pass,
                f"stress ROM={stress['rom']:.4f} (n={stress['n']}, "
                f"worst_cycle={stress['worst_cycle']:.0f}; "
                f"need > {stress_rom_floor:g} / non-catastrophic)",
            )

    return gates


# ===========================================================================
# Top-level orchestration → honest verdict
# ===========================================================================
def _is_preliminary(series: CycleSeries) -> bool:
    """PRELIMINARY when ANY traded cycle priced off the VIX proxy (not real ATM IV)."""
    if not series.iv_source:
        return True  # unknown provenance → assume proxy (conservative)
    return any(s != IV_SOURCE_REAL for s in series.iv_source)


def restate_edge(
    series: CycleSeries,
    *,
    haircut_series: CycleSeries | None = None,
    min_oos_trades: int = 30,
    stress_regime: str = "stress",
    stress_rom_floor: float = -0.25,
    oos_frac: float = 0.30,
    purge: int = 0,
    cvar_alpha: float = CVAR_ALPHA_DEFAULT,
    block_len: int = DEFAULT_BLOCK_LEN,
    bootstrap_seeds: int = DEFAULT_BOOTSTRAP_SEEDS,
) -> dict[str, Any]:
    """Full honest edge restatement: α / β / γ + falsification gates + verdict.

    Returns a dict with a STABLE key set — ``beta``, ``alpha``, ``tail``,
    ``deflated_sharpe``, ``walk_forward``, ``bootstrap``, ``regime``, ``gates``,
    ``sharpe``, ``iv_source_summary`` and the top-level ``verdict`` / ``overall_pass``
    / ``failed_gates`` / ``preliminary`` / ``n``. The verdict is GO only when ALL
    falsification gates pass; otherwise NO-GO with the failing gates named. A
    PRELIMINARY flag is set when any cycle priced off the VIX proxy. The empty
    (n==0) path returns the SAME key set with neutral defaults so consumers never
    hit a KeyError on a degenerate input.
    """
    if series.n == 0:
        return {
            "verdict": f"{NO_GO} — no cycles to evaluate.",
            "n": 0,
            "overall_pass": False,
            "failed_gates": [],
            "preliminary": True,
            "beta": {"available": False, "reason": "no cycles"},
            "alpha": {},
            "tail": {},
            "deflated_sharpe": {},
            "walk_forward": {},
            "bootstrap": {},
            "regime": {"available": False, "reason": "no cycles"},
            "gates": {},
            "sharpe": 0.0,
            "iv_source_summary": {"real": 0, "vix_proxy": 0, "unknown": 0},
        }

    beta = beta_analysis(series)
    alpha = alpha_analysis(series)
    tail = tail_analysis(series, alpha=cvar_alpha)
    boot = block_bootstrap_rom_ci(series, block_len=block_len, seeds=bootstrap_seeds)
    wf = walk_forward_oos(series, oos_frac=oos_frac, purge=purge)
    regime = regime_breakdown(series)

    sharpe = _sharpe_from_pnls(series.pnl)
    dsr = deflated_sharpe(
        sharpe, series.n_configs_tried, series.n,
        skew=tail["skew"], kurtosis=tail["kurtosis"],
    )

    gates = falsification_gates(
        series,
        haircut_series=haircut_series,
        min_oos_trades=min_oos_trades,
        stress_regime=stress_regime,
        stress_rom_floor=stress_rom_floor,
        oos_frac=oos_frac,
        purge=purge,
        block_len=block_len,
        bootstrap_seeds=bootstrap_seeds,
    )

    preliminary = _is_preliminary(series)
    failed = [name for name, g in gates.items() if not g["pass"]]
    overall_pass = len(failed) == 0
    prefix = GO if overall_pass else NO_GO
    iv_note = " [PRELIMINARY — VIX-proxy IV; real ATM IV / FSP needed]" if preliminary else ""

    if overall_pass:
        verdict = (
            f"{prefix} — honest condor edge survives all {len(gates)} falsification "
            f"gates (OOS ROM={wf['rom_oos']:.2%} [{boot['lo']:.2%},{boot['hi']:.2%}], "
            f"DSR={dsr['dsr']:.2f}, β R²={beta.get('r_squared', 0.0):.2f}, "
            f"CVaR{int(cvar_alpha * 100)}%=₹{tail['cvar']:,.0f}).{iv_note} {CAVEATS}"
        )
    else:
        verdict = (
            f"{prefix} — condor edge FAILS {len(failed)}/{len(gates)} falsification "
            f"gate(s): {', '.join(failed)}. OOS ROM={wf['rom_oos']:.2%} "
            f"[{boot['lo']:.2%},{boot['hi']:.2%}], DSR={dsr['dsr']:.2f}, "
            f"worst_cycle=₹{tail['worst_cycle']:,.0f}, "
            f"CVaR{int(cvar_alpha * 100)}%=₹{tail['cvar']:,.0f}.{iv_note} {CAVEATS}"
        )

    return {
        "verdict": verdict,
        "overall_pass": overall_pass,
        "failed_gates": failed,
        "preliminary": preliminary,
        "n": series.n,
        "beta": beta,
        "alpha": alpha,
        "tail": tail,
        "deflated_sharpe": dsr,
        "walk_forward": wf,
        "bootstrap": boot,
        "regime": regime,
        "gates": gates,
        "sharpe": sharpe,
        # unknown = cycles with no recognised IV provenance: those with no
        # iv_source entry at all (missing-length gap) PLUS any present-but-
        # unrecognised token (e.g. "" from the getattr default). The three buckets
        # always sum to n.
        "iv_source_summary": {
            "real": sum(1 for s in series.iv_source if s == IV_SOURCE_REAL),
            "vix_proxy": sum(1 for s in series.iv_source if s == IV_SOURCE_VIX_PROXY),
            "unknown": (
                (series.n - len(series.iv_source))
                + sum(
                    1
                    for s in series.iv_source
                    if s not in (IV_SOURCE_REAL, IV_SOURCE_VIX_PROXY)
                )
            ),
        },
    }


# ===========================================================================
# Convenience: build a CycleSeries from condor run_backtest trades
# ===========================================================================
def series_from_trades(
    trades: list[Any],
    *,
    passive_trades: list[Any] | None = None,
    short_vol_factor: list[float] | None = None,
    regime: list[str] | None = None,
    capital: float = 200_000.0,
    n_configs_tried: int = 1,
) -> CycleSeries:
    """Build a :class:`CycleSeries` from condor ``CondorTrade`` records.

    Pulls ``net_pnl`` / ``max_loss`` / ``iv_source`` off each trade (sorted by
    ``entry_date`` for chronological order). ``passive_trades`` (the always-on
    condor on the same cycles) supplies the α-vs-passive benchmark; it is aligned
    by ``entry_date`` so only cycles present in BOTH contribute the passive leg
    (missing → that cycle's passive P&L is treated as 0, i.e. stand-aside).

    By-date alignment requires ``entry_date`` to be UNIQUE within each of ``trades``
    and ``passive_trades``. A duplicate date (e.g. a multi-index series with the
    same expiry weekday, or same-day re-entry) would silently collapse the passive
    leg, so it is rejected with a ValueError — the caller must then build the
    ``CycleSeries`` directly with a positionally-aligned ``passive_pnl``.
    """
    ordered = sorted(trades, key=lambda t: t.entry_date)
    pnl = [float(t.net_pnl) for t in ordered]
    margin = [float(t.max_loss) for t in ordered]
    iv_source = [getattr(t, "iv_source", "") or "" for t in ordered]

    passive_pnl: list[float] | None = None
    if passive_trades is not None:
        gated_dates = [t.entry_date for t in ordered]
        if len(set(gated_dates)) != len(gated_dates):
            raise ValueError(
                "series_from_trades: duplicate entry_date in trades — by-date passive "
                "alignment is ambiguous (multi-index / same-day re-entry). Build the "
                "CycleSeries directly with a positionally-aligned passive_pnl."
            )
        passive_dates = [t.entry_date for t in passive_trades]
        if len(set(passive_dates)) != len(passive_dates):
            raise ValueError(
                "series_from_trades: duplicate entry_date in passive_trades — by-date "
                "alignment is ambiguous. Build the CycleSeries directly with a "
                "positionally-aligned passive_pnl."
            )
        by_date = {t.entry_date: float(t.net_pnl) for t in passive_trades}
        passive_pnl = [by_date.get(t.entry_date, 0.0) for t in ordered]

    return CycleSeries(
        pnl=pnl,
        margin=margin,
        short_vol_factor=short_vol_factor,
        passive_pnl=passive_pnl,
        regime=regime,
        iv_source=iv_source,
        capital=capital,
        n_configs_tried=n_configs_tried,
    )
