"""Realized cross-sectional correlation / dispersion of the stock-futures panel.

This module computes the **average pairwise realized correlation** ρ̄ and the
**realized dispersion** of the ~188 single-stock-futures universe (``futures_bars``
rows keyed ``"<SYM>-FUT"``).  It is the cheap, read-only feature engine behind the
go/no-go check on the *dispersion-gate* thesis (research task #11): does realized
cross-sectional correlation/dispersion of the stock futures predict the NIFTY
iron-condor's per-cycle outcome?  ``equity_corr_rank_ic.py`` consumes these series.

Estimator — the variance-ratio ρ̄ (O(N), robust)
------------------------------------------------
For an **equal-weight** basket of ``N`` names with per-name return variances
``σ²_i`` and basket-return variance ``σ²_basket`` (all over the same window),
the average pairwise correlation is recovered in closed form::

        N² · σ²_basket  −  Σ σ²_i
  ρ̄ = ───────────────────────────────
              (Σ σ_i)²  −  Σ σ²_i

This is exact for an equal-weight basket (basket variance =
``(1/N²)·[Σσ²_i + Σ_{i≠j} σ_iσ_jρ_ij]``) and only needs per-name variances plus
the basket variance — never the full N×N covariance matrix.  ρ̄ is clamped to
``[-1/(N-1), 1]`` (the algebraic floor for an equal-weight basket).

Conventions (mirrors ``core/fno_derived.py``)
--------------------------------------------
* Close-to-close **log** returns (``daily_log_returns`` reused from fno_derived).
* Sample variance / std with ``ddof=1``; annualisation by 252 (only matters for
  the dispersion frame's vol columns — ρ̄ is scale-free).
* **Never ``fillna(0)``** — a NaN return inside a window kills that name for that
  day; a day with fewer than ``min_names`` valid names yields ρ̄ = NaN.

Correctness scrubs (the #1 risk = futures stitch contamination)
---------------------------------------------------------------
``scrub_panel`` applies, per symbol column:
  * **roll-day NaN**: set the return to NaN on any bar whose ``expiry_date``
    differs from the prior bar's — a stitched contract roll injects a spurious
    jump that would masquerade as a correlated market move.
  * **non-positive close** → return NaN (handled by ``daily_log_returns``).
  * **|return| > 0.20** → NaN (residual bad-print / unscrubbed-roll guard).

The pure functions take a tidy ``returns_df`` (index=date, columns=symbol) so they
unit-test with zero DB.  ``load_returns`` / ``dispersion_frame`` wrap the DB.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np
import pandas as pd

from core.fno_derived import TRADING_DAYS, daily_log_returns

logger = logging.getLogger("dhan.research.equity_correlation")

DEFAULT_WINDOW = 63
DEFAULT_EWMA_LAMBDA = 0.94
DEFAULT_MIN_NAMES = 30
MAX_ABS_RETURN = 0.20  # |daily log return| above this is treated as a bad print


# ── pure helpers ──────────────────────────────────────────────────────────────
def closes_to_returns(closes_df: pd.DataFrame) -> pd.DataFrame:
    """date×symbol close panel → date×symbol log-return panel (NaN-safe).

    The first row is dropped (no prior close).  Non-positive / missing closes
    yield NaN returns via :func:`core.fno_derived.daily_log_returns`.
    """
    out = {}
    for col in closes_df.columns:
        series = closes_df[col].tolist()
        rets = daily_log_returns([None if pd.isna(x) else float(x) for x in series])
        out[col] = rets  # length len(series) - 1, aligned to closes_df.index[1:]
    return pd.DataFrame(out, index=closes_df.index[1:])


def scrub_returns(
    returns_df: pd.DataFrame,
    expiry_df: Optional[pd.DataFrame] = None,
    max_abs_return: float = MAX_ABS_RETURN,
) -> pd.DataFrame:
    """Scrub a date×symbol return panel in place-safe fashion (returns a copy).

    * roll-day: when ``expiry_df`` is supplied (date×symbol of each bar's
      ``expiry_date``), any bar whose expiry differs from the **prior** bar's
      expiry has its return set to NaN — this kills futures-stitch jumps.  The
      ``expiry_df`` is expected to be aligned to the *close* panel index
      (i.e. it includes the dropped first row); we re-align onto the return
      index here.
    * any ``|return| > max_abs_return`` → NaN.

    Never fills NaNs.
    """
    df = returns_df.copy()

    if expiry_df is not None:
        # expiry_df is indexed by the full close-panel dates; the return at
        # return-index date d corresponds to the move INTO bar d, i.e. the
        # transition expiry[d_prev] -> expiry[d].  A roll has expiry[d] != expiry[d_prev].
        for col in df.columns:
            if col not in expiry_df.columns:
                continue
            exp = expiry_df[col]
            changed = exp != exp.shift(1)
            # changed is indexed by the full close panel; align onto return index.
            roll_mask = changed.reindex(df.index).fillna(False).astype(bool)
            # The very first comparison (shift -> NaN) is a non-roll by construction;
            # fillna(False) above handles dates absent from expiry_df too.
            df.loc[roll_mask[roll_mask].index, col] = np.nan

    # Bad-print guard.
    df = df.mask(df.abs() > max_abs_return)
    return df


def _variance_ratio_corr(
    variances: np.ndarray, basket_variance: float
) -> Optional[float]:
    """Average pairwise correlation from per-name variances + basket variance.

    ``variances`` is the 1-D array of per-name return variances (length N, all
    finite, all > 0 caller-guaranteed for the active names).  Returns ρ̄ clamped
    to ``[-1/(N-1), 1]`` or ``None`` if degenerate.
    """
    n = variances.size
    if n < 2:
        return None
    sigma = np.sqrt(variances)
    sum_sigma = float(sigma.sum())
    sum_var = float(variances.sum())
    denom = sum_sigma * sum_sigma - sum_var
    if denom <= 0:
        return None
    numer = (n * n) * basket_variance - sum_var
    rho = numer / denom
    floor = -1.0 / (n - 1)
    return float(min(1.0, max(floor, rho)))


def _window_corr(
    window_block: pd.DataFrame, min_names: int, ddof: int = 1
) -> tuple[Optional[float], int]:
    """ρ̄ over one window block (rows=days in window, cols=symbols).

    A symbol is *valid* only if it has NO NaN return anywhere in the window
    (NaN-in-window kills the pair/day).  Need ≥ ``min_names`` valid names.
    Returns (rho_bar, n_valid_names).
    """
    valid = window_block.dropna(axis=1, how="any")
    n = valid.shape[1]
    if n < min_names:
        return None, n
    variances = valid.var(ddof=ddof, axis=0).to_numpy()
    if not np.all(np.isfinite(variances)) or np.any(variances <= 0):
        # Drop zero/degenerate-variance names then retry.
        good = np.isfinite(variances) & (variances > 0)
        valid = valid.loc[:, valid.columns[good]]
        n = valid.shape[1]
        if n < min_names:
            return None, n
        variances = valid.var(ddof=ddof, axis=0).to_numpy()
    basket = valid.mean(axis=1)  # equal-weight basket return series
    basket_var = float(basket.var(ddof=ddof))
    rho = _variance_ratio_corr(variances, basket_var)
    return rho, n


def realized_corr_series(
    returns_df: pd.DataFrame,
    window: int = DEFAULT_WINDOW,
    *,
    ewma_lambda: Optional[float] = None,
    min_names: int = DEFAULT_MIN_NAMES,
    ddof: int = 1,
) -> pd.Series:
    """Daily average pairwise realized correlation ρ̄ of an equal-weight basket.

    Two variants:
      * **rolling window** (default, ``ewma_lambda=None``): ρ̄ at date ``d`` uses
        the ``window`` returns ending at ``d`` (inclusive).  Result is NaN until
        a full window of dates exists.
      * **EWMA** (``ewma_lambda`` set, e.g. 0.94): exponentially-weighted
        variances / basket variance with decay λ.  ρ̄ at ``d`` uses all returns
        up to and including ``d`` with weight ``λ^k`` on the lag-k return.

    Either way a day's ρ̄ is NaN unless ≥ ``min_names`` names have a complete
    (NaN-free) contribution.  Output index == ``returns_df.index``.
    """
    idx = returns_df.index
    if ewma_lambda is not None:
        return _realized_corr_ewma(
            returns_df, ewma_lambda, min_names=min_names, ddof=ddof
        )

    out: list[float] = [math.nan] * len(idx)
    n = len(idx)
    for i in range(window - 1, n):
        block = returns_df.iloc[i - window + 1 : i + 1]
        rho, _ = _window_corr(block, min_names=min_names, ddof=ddof)
        out[i] = math.nan if rho is None else rho
    return pd.Series(out, index=idx, name="rho_bar")


def _realized_corr_ewma(
    returns_df: pd.DataFrame,
    lam: float,
    *,
    min_names: int,
    ddof: int,
) -> pd.Series:
    """EWMA variant of :func:`realized_corr_series`.

    For each date we form EWMA per-name variances and the EWMA basket variance
    over the equal-weight basket of names that are NaN-free up to that date, then
    apply the variance-ratio estimator.  Implemented per-date (clear, O(T·N));
    the universe is small (~188) so this is cheap enough for a research run.
    """
    if not (0.0 < lam < 1.0):
        raise ValueError(f"ewma_lambda must be in (0,1), got {lam}")
    idx = returns_df.index
    out: list[float] = [math.nan] * len(idx)
    for i in range(len(idx)):
        block = returns_df.iloc[: i + 1]
        valid = block.dropna(axis=1, how="any")
        if valid.shape[1] < min_names or valid.shape[0] < 2:
            continue
        # EWMA stats: pandas ewm with the same decay; take the last value.
        # var uses bias-correction equivalent to ddof=1 when bias=False.
        ewm = valid.ewm(alpha=1.0 - lam, adjust=True)
        variances = ewm.var(bias=(ddof == 0)).iloc[-1].to_numpy()
        good = np.isfinite(variances) & (variances > 0)
        if int(good.sum()) < min_names:
            continue
        cols = valid.columns[good]
        variances = variances[good]
        basket = valid[cols].mean(axis=1)
        basket_var = float(
            basket.ewm(alpha=1.0 - lam, adjust=True).var(bias=(ddof == 0)).iloc[-1]
        )
        rho = _variance_ratio_corr(variances, basket_var)
        out[i] = math.nan if rho is None else rho
    return pd.Series(out, index=idx, name="rho_bar_ewma")


def realized_dispersion_series(
    returns_df: pd.DataFrame,
    window: int = DEFAULT_WINDOW,
    *,
    min_names: int = DEFAULT_MIN_NAMES,
    trading_days: int = TRADING_DAYS,
    ddof: int = 1,
) -> pd.DataFrame:
    """Rolling realized dispersion: avg single-name vol vs basket/index vol.

    For each date (rolling ``window``, NaN-free names only), returns a frame with:
      * ``avg_single_var`` — mean per-name return variance (window).
      * ``basket_var``     — equal-weight basket return variance (window).
      * ``avg_single_vol`` — annualised √(avg_single_var).
      * ``basket_vol``     — annualised √(basket_var).
      * ``dispersion``     — ``avg_single_vol − basket_vol`` (≥ 0 when names
        decorrelate; → 0 as ρ̄ → 1).
      * ``n_names``        — valid names that day.
    """
    idx = returns_df.index
    ann = math.sqrt(trading_days)
    recs = []
    n = len(idx)
    for i in range(n):
        if i < window - 1:
            recs.append((math.nan,) * 5 + (0,))
            continue
        block = returns_df.iloc[i - window + 1 : i + 1]
        valid = block.dropna(axis=1, how="any")
        n_valid = valid.shape[1]
        if n_valid < min_names:
            recs.append((math.nan,) * 5 + (n_valid,))
            continue
        variances = valid.var(ddof=ddof, axis=0)
        avg_single_var = float(variances.mean())
        basket_var = float(valid.mean(axis=1).var(ddof=ddof))
        avg_single_vol = math.sqrt(max(avg_single_var, 0.0)) * ann
        basket_vol = math.sqrt(max(basket_var, 0.0)) * ann
        recs.append(
            (
                avg_single_var,
                basket_var,
                avg_single_vol,
                basket_vol,
                avg_single_vol - basket_vol,
                n_valid,
            )
        )
    return pd.DataFrame(
        recs,
        index=idx,
        columns=[
            "avg_single_var",
            "basket_var",
            "avg_single_vol",
            "basket_vol",
            "dispersion",
            "n_names",
        ],
    )


def corr_implied_from_vols(
    avg_single_vol: float, basket_vol: float, n_names: int
) -> Optional[float]:
    """Consistency-check correlation implied by the vol ratio.

    Under the equal-weight identity with all names sharing the same vol,
    ``basket_var ≈ avg_var · (1 + (N-1)ρ) / N`` ⇒
    ``ρ ≈ (N · (basket_vol/avg_vol)² − 1) / (N − 1)``.  This should track the
    variance-ratio ρ̄ closely; large divergence flags a data problem.  Clamped
    to ``[-1/(N-1), 1]``.
    """
    if n_names < 2 or avg_single_vol is None or avg_single_vol <= 0:
        return None
    if basket_vol is None or basket_vol < 0:
        return None
    ratio = (basket_vol / avg_single_vol) ** 2
    rho = (n_names * ratio - 1.0) / (n_names - 1)
    floor = -1.0 / (n_names - 1)
    return float(min(1.0, max(floor, rho)))


def dispersion_frame(
    returns_df: pd.DataFrame,
    window: int = DEFAULT_WINDOW,
    *,
    ewma_lambda: Optional[float] = DEFAULT_EWMA_LAMBDA,
    min_names: int = DEFAULT_MIN_NAMES,
    trading_days: int = TRADING_DAYS,
    ddof: int = 1,
) -> pd.DataFrame:
    """Combined per-date feature frame: ρ̄ (rolling + EWMA), dispersion, n_names,
    and the ``corr_implied`` vol-ratio consistency check.

    Columns: ``rho_bar``, ``rho_bar_ewma``, ``avg_single_vol``, ``basket_vol``,
    ``dispersion``, ``corr_implied``, ``n_names``.  Index == ``returns_df.index``.
    """
    rho = realized_corr_series(
        returns_df, window=window, min_names=min_names, ddof=ddof
    )
    disp = realized_dispersion_series(
        returns_df, window=window, min_names=min_names,
        trading_days=trading_days, ddof=ddof,
    )
    frame = pd.DataFrame(index=returns_df.index)
    frame["rho_bar"] = rho
    if ewma_lambda is not None:
        frame["rho_bar_ewma"] = realized_corr_series(
            returns_df, window=window, ewma_lambda=ewma_lambda,
            min_names=min_names, ddof=ddof,
        )
    else:
        frame["rho_bar_ewma"] = math.nan
    frame["avg_single_vol"] = disp["avg_single_vol"]
    frame["basket_vol"] = disp["basket_vol"]
    frame["dispersion"] = disp["dispersion"]
    frame["corr_implied"] = [
        corr_implied_from_vols(a, b, int(n))
        for a, b, n in zip(disp["avg_single_vol"], disp["basket_vol"], disp["n_names"])
    ]
    frame["n_names"] = disp["n_names"]
    return frame


# ── DB wrapper ─────────────────────────────────────────────────────────────────
def _backtest_db_url() -> Optional[str]:
    """Return config.backtest_db_url if available, else None (caller falls back)."""
    try:
        from config import get_config  # noqa: PLC0415

        return get_config().backtest_db_url
    except Exception:  # pragma: no cover - config optional in pure contexts
        return None


def load_returns(
    timeframe: str = "1d",
    start: Optional[str] = None,
    end: Optional[str] = None,
    *,
    suffix: str = "-FUT",
) -> dict[str, pd.DataFrame]:
    """Load + scrub the stock-futures return panel and NIFTY returns from the DB.

    Pivots ``futures_bars`` closes (symbols ending ``suffix``) to a date×symbol
    panel, builds the date×symbol ``expiry_date`` panel for the roll-day scrub,
    converts to scrubbed log returns, and also loads NIFTY index returns
    (security_id ``'13'`` in ``index_bars``).

    Returns a dict with keys:
      * ``"closes"``      — date×symbol close panel (raw).
      * ``"returns"``     — date×symbol **scrubbed** log returns.
      * ``"nifty_ret"``   — Series of NIFTY log returns (scrubbed, |ret|≤0.20).
      * ``"nifty_close"`` — Series of NIFTY closes.
    """
    from sqlalchemy import text  # noqa: PLC0415

    from db import get_session  # noqa: PLC0415

    where = ["timeframe = :tf", "symbol LIKE :suf"]
    params: dict = {"tf": timeframe, "suf": f"%{suffix}"}
    if start:
        where.append("(time AT TIME ZONE 'UTC')::date >= :start")
        params["start"] = start
    if end:
        where.append("(time AT TIME ZONE 'UTC')::date <= :end")
        params["end"] = end
    clause = " AND ".join(where)

    with get_session() as session:
        rows = session.execute(
            text(
                f"SELECT (time AT TIME ZONE 'UTC')::date AS d, symbol, "
                f"close, expiry_date FROM futures_bars WHERE {clause} ORDER BY 1, 2"
            ),
            params,
        ).fetchall()

        # NIFTY index closes (security_id '13') over the same window.
        n_where = ["security_id = '13'", "timeframe = :tf"]
        if start:
            n_where.append("(time AT TIME ZONE 'UTC')::date >= :start")
        if end:
            n_where.append("(time AT TIME ZONE 'UTC')::date <= :end")
        nifty_rows = session.execute(
            text(
                "SELECT (time AT TIME ZONE 'UTC')::date AS d, close "
                f"FROM index_bars WHERE {' AND '.join(n_where)} ORDER BY 1"
            ),
            params,
        ).fetchall()

    if not rows:
        logger.warning("load_returns: futures_bars returned 0 rows for tf=%s", timeframe)
        empty = pd.DataFrame()
        return {"closes": empty, "returns": empty,
                "nifty_ret": pd.Series(dtype=float), "nifty_close": pd.Series(dtype=float)}

    df = pd.DataFrame(rows, columns=["d", "symbol", "close", "expiry_date"])
    df["d"] = pd.to_datetime(df["d"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    closes = df.pivot_table(index="d", columns="symbol", values="close", aggfunc="last")
    expiry = df.pivot_table(
        index="d", columns="symbol", values="expiry_date", aggfunc="last"
    )
    closes = closes.sort_index()
    expiry = expiry.reindex(closes.index)[closes.columns]

    raw_returns = closes_to_returns(closes)
    returns = scrub_returns(raw_returns, expiry_df=expiry)

    nifty_close = pd.Series(dtype=float)
    nifty_ret = pd.Series(dtype=float)
    if nifty_rows:
        ndf = pd.DataFrame(nifty_rows, columns=["d", "close"])
        ndf["d"] = pd.to_datetime(ndf["d"])
        nifty_close = pd.Series(
            pd.to_numeric(ndf["close"], errors="coerce").tolist(),
            index=ndf["d"], name="nifty_close",
        ).sort_index()
        nret = daily_log_returns(
            [None if pd.isna(x) else float(x) for x in nifty_close.tolist()]
        )
        nifty_ret = pd.Series(nret, index=nifty_close.index[1:], name="nifty_ret")
        nifty_ret = nifty_ret.mask(nifty_ret.abs() > MAX_ABS_RETURN)

    return {
        "closes": closes,
        "returns": returns,
        "nifty_ret": nifty_ret,
        "nifty_close": nifty_close,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────────
def _summarise(frame: pd.DataFrame) -> str:
    rho = frame["rho_bar"].dropna()
    nn = frame["n_names"]
    lines = [
        "=== equity realized-correlation summary ===",
        f"date range : {frame.index.min()} → {frame.index.max()}  ({len(frame)} days)",
        f"n_names    : min={int(nn.min())} median={int(nn.median())} max={int(nn.max())}",
    ]
    if not rho.empty:
        pct = rho.quantile([0.05, 0.25, 0.5, 0.75, 0.95])
        lines.append(
            "rho_bar    : "
            f"p05={pct.loc[0.05]:.3f} p25={pct.loc[0.25]:.3f} "
            f"p50={pct.loc[0.5]:.3f} p75={pct.loc[0.75]:.3f} p95={pct.loc[0.95]:.3f} "
            f"(n={len(rho)})"
        )
        disp = frame["dispersion"].dropna()
        if not disp.empty:
            lines.append(
                f"dispersion : mean={disp.mean():.4f} "
                f"p50={disp.median():.4f} max={disp.max():.4f}"
            )
    else:
        lines.append("rho_bar    : (all NaN — check min_names / data coverage)")
    return "\n".join(lines)


def main() -> None:  # pragma: no cover - thin CLI
    import argparse

    parser = argparse.ArgumentParser(
        description="Realized cross-sectional correlation/dispersion of stock futures."
    )
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    parser.add_argument("--ewma-lambda", type=float, default=DEFAULT_EWMA_LAMBDA)
    parser.add_argument("--min-names", type=int, default=DEFAULT_MIN_NAMES)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--out", default=None, help="optional parquet output path")
    args = parser.parse_args()

    from config import get_config
    from db import init_db

    cfg = get_config()
    init_db(getattr(cfg, "backtest_db_url", None) or cfg.db_url)

    data = load_returns(timeframe=args.timeframe, start=args.start, end=args.end)
    returns = data["returns"]
    if returns.empty:
        print("No futures_bars returns loaded — nothing to do.")
        return

    frame = dispersion_frame(
        returns, window=args.window, ewma_lambda=args.ewma_lambda,
        min_names=args.min_names,
    )
    print(_summarise(frame))
    if args.out:
        import os

        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        frame.to_parquet(args.out)
        print(f"\nwrote {len(frame)} rows → {args.out}")


if __name__ == "__main__":  # pragma: no cover
    main()
