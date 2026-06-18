"""
F&O Volatility-Regime Gate — VRP (Volatility Risk Premium) proxy gate.

PURPOSE
-------
This module is the Phase-0 volatility-regime gate for F&O strategy decisions.
It is entirely separate from ml/kronos_gate.py (the live EQUITY ORB gate) and
must NEVER be confused with it:

  • ml/kronos_gate.py   — deep-learning directional gate for NSE_EQ ORB trades.
  • ml/fno_vol_gate.py  — rule-based VRP gate for F&O options premium
                          selling / buying decisions (THIS file).

VRP LOGIC
---------
Options imply a level of forward volatility (implied_vol = ATM straddle IV,
annualised fraction). The market consistently *overprices* volatility relative to
what subsequently realises — this spread is the Volatility Risk Premium (VRP).

A simple gate (BUY checked before SELL — see gate_decision docstring for why):

  1.  If predicted_realized_vol > implied_vol       →  BUY_PREMIUM
      (realized likely to exceed implied → long vol)
  2.  If predicted_realized_vol < k × implied_vol  →  SELL_PREMIUM
      (implied is elevated vs what we expect to realise → harvest the premium)
  3.  Otherwise                                     →  STAND_ASIDE
      (regime unclear — do nothing)

PROXY (Phase-0 persistence forecast)
-------------------------------------
A proper realized-vol forecast would require a NIFTY-trained time-series model.
The NSE F&O universe (NIFTY, BANKNIFTY continuous futures) is not yet in the
Kronos training corpus; Phase-0 uses PERSISTENCE as the simplest unbiased proxy:

    predicted_realized_vol ≈ trailing realized_vol_20d

This is the industry baseline: yesterday's realized vol is tomorrow's best naive
guess. It has positive expected edge (VRP is a documented risk premium) but will
miss vol-regime transitions.

PROXY HORIZON MISMATCH (main source of proxy error)
-----------------------------------------------------
realized_vol_20d is a 20-trading-day BACKWARD-looking realized vol. The ATM
straddle price, by contrast, embeds the market's expectation of volatility over
the FORWARD period until expiry — typically ~5–10 trading days for the nearest
weekly option. Using a 20-day backward vol as a proxy for a ~5–10-day forward
vol is a deliberate simplification: it is stable and well-understood, but it will
lag vol-regime changes and structurally over-smooth short-term spikes.

A 5–10 day trailing realized vol (or an EWMA with a short half-life) would better
match the straddle's pricing horizon and should be revisited once a NIFTY-trained
Kronos checkpoint replaces the persistence proxy (Open Question #4). Until then,
20-day persistence is the Phase-0 baseline.

OPEN QUESTION #4 — Kronos NIFTY fine-tune
------------------------------------------
Once the M2.5 clean-data build includes NIFTY continuous futures bars and the
fine-tune pipeline (feat/s3-pipeline-wiring) is extended to train on them, a
NIFTY-specific Kronos checkpoint can replace the persistence proxy here.
The interface of gate_decision() is unchanged — only the `realized_vol` argument
source changes from trailing_realized_vol_20d to a Kronos vol forecast.
Track this under Open Question #4 in the F&O milestone doc.

This module DOES NOT import Kronos — there is no NIFTY checkpoint yet; do not
wire a forecast that does not exist.

FAIL-OPEN
---------
Any None / degenerate input returns STAND_ASIDE. The gate never raises. This
matches the equity gate's fail-open contract (model errors never block trades).

UNITS
-----
Both realized_vol and implied_vol are annualised fractions (e.g. 0.12 = 12 %
annualised vol). This matches how realized_vol_20d is written by
core/fno_derived.compute_realized_vol() and how straddle_iv is stored in
option_atm_iv (stored as a fraction by core/fno_backfill).

HISTORICAL REALIZED-VOL BASE
-----------------------------
The realized-vol baseline in both ``samples_from_db`` paths comes from the
**NIFTY 50 SPOT INDEX** (``index_bars``, security_id ``"13"``), specifically the
``realized_vol_20d`` column populated by core/fno_derived.  This is consistent
with ``cycles_from_db``, which also reads the spot index.  The underlying series
is NOT futures bars.

HISTORICAL IMPLIED BASELINE (source="vix")
------------------------------------------
Until forward ATM-IV from option_atm_iv accrues sufficient history, the
``samples_from_db`` helper defaults to ``source="vix"``, which uses India VIX
(security_id="21" in index_bars) as the implied-vol proxy.

India VIX is quoted in percent (e.g. 13.5 = 13.5 % annualised); dividing by 100
gives an annualised fraction directly comparable to realized_vol_20d.  This is
the Phase-0 historical backtest path — it is NOT the live forward-IV path.

Once option_atm_iv rows accumulate, switch to ``source="atm"`` to use the live
ATM straddle IV instead.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("dhan.ml.fno_vol_gate")

# ── Constants ──────────────────────────────────────────────────────────────────

SELL_PREMIUM = "SELL_PREMIUM"
STAND_ASIDE = "STAND_ASIDE"
BUY_PREMIUM = "BUY_PREMIUM"

# Default k: implied vol must be at least 1/0.9 ≈ 111% of realized vol before
# we call SELL_PREMIUM. This gives a small cushion against noise.
DEFAULT_K: float = 0.9

# Bounds for calibrate_threshold — prevent degenerate k values.
_K_MIN: float = 0.5
_K_MAX: float = 1.5


# ── Pure gate function ────────────────────────────────────────────────────────

def gate_decision(
    realized_vol: Optional[float],
    implied_vol: Optional[float],
    k: float = DEFAULT_K,
) -> str:
    """Classify the volatility regime from a single (realized_vol, implied_vol) pair.

    Parameters
    ----------
    realized_vol:
        Predicted annualised realized vol (fraction). For Phase-0 this is the
        trailing realized_vol_20d of the NIFTY 50 SPOT INDEX (index_bars,
        persistence proxy — see module docstring). Must be non-negative.
    implied_vol:
        ATM straddle IV (annualised fraction) from option_atm_iv.straddle_iv.
        Must be strictly positive.
    k:
        Sell-premium threshold multiplier (default 0.9). SELL_PREMIUM is
        triggered when ``realized_vol < k * implied_vol``.

    Returns
    -------
    str
        One of SELL_PREMIUM, BUY_PREMIUM, STAND_ASIDE.
        Never raises — fail-open returns STAND_ASIDE on degenerate inputs.

    Notes
    -----
    BUY_PREMIUM is evaluated BEFORE SELL_PREMIUM.  When k > 1 (which
    calibrate_threshold can return — the clamp ceiling is 1.5) the two
    conditions overlap: a realized_vol that is simultaneously > implied_vol
    and < k * implied_vol would satisfy both.  In that region the correct
    signal is BUY_PREMIUM (we expect realized to exceed implied), so BUY
    must take precedence.  For the default k = 0.9 (≤ 1) there is no
    overlap and the ordering does not change observable behaviour.
    """
    try:
        if realized_vol is None or implied_vol is None or implied_vol <= 0 or realized_vol < 0:
            return STAND_ASIDE
        rv = float(realized_vol)
        iv = float(implied_vol)
        # Comparing annualised vols is equivalent to comparing implied vs predicted
        # MOVES: the spot·√(dte/365) scaling factor cancels in the ratio rv/iv.
        if rv > iv:
            return BUY_PREMIUM
        if rv < k * iv:
            return SELL_PREMIUM
        return STAND_ASIDE
    except Exception as exc:  # noqa: BLE001
        logger.warning("gate_decision: unexpected error (%s) — returning STAND_ASIDE", exc)
        return STAND_ASIDE


# ── VRP statistics ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VrpStats:
    """Aggregate VRP statistics over a sample set."""

    n: int
    """Number of usable (non-None, positive IV) samples."""

    pass_rate: float
    """Fraction of usable samples classified as SELL_PREMIUM at threshold k."""

    mean_edge: float
    """Mean of (implied_vol - realized_vol) over SELL_PREMIUM samples, in vol
    points (annualised fraction). Zero when there are no SELL_PREMIUM samples."""

    median_edge: float
    """Median of (implied_vol - realized_vol) over SELL_PREMIUM samples.
    Zero when there are no SELL_PREMIUM samples."""

    edge_win_rate: float
    """Fraction of SELL_PREMIUM samples where implied_vol > realized_vol (i.e.
    the premium capture has positive expected edge at that observation). Zero
    when there are no SELL_PREMIUM samples."""


def compute_vrp_stats(
    samples: list[tuple[Optional[float], Optional[float]]],
    k: float = DEFAULT_K,
) -> VrpStats:
    """Compute aggregate VRP statistics over a list of (realized_vol, implied_vol) pairs.

    Parameters
    ----------
    samples:
        List of (realized_vol, implied_vol) tuples. Pairs where either value is
        None or implied_vol <= 0 are silently dropped.
    k:
        Sell-premium threshold (same k as gate_decision).

    Returns
    -------
    VrpStats
        Zero/empty stats when the usable sample count is zero.
    """
    usable: list[tuple[float, float]] = []
    for rv, iv in samples:
        if rv is None or iv is None or iv <= 0:
            continue
        try:
            usable.append((float(rv), float(iv)))
        except (TypeError, ValueError):
            continue

    n = len(usable)
    if n == 0:
        return VrpStats(n=0, pass_rate=0.0, mean_edge=0.0,
                        median_edge=0.0, edge_win_rate=0.0)

    sell_edges: list[float] = []
    for rv, iv in usable:
        if gate_decision(rv, iv, k) == SELL_PREMIUM:
            sell_edges.append(iv - rv)

    pass_rate = len(sell_edges) / n

    if not sell_edges:
        return VrpStats(n=n, pass_rate=pass_rate, mean_edge=0.0,
                        median_edge=0.0, edge_win_rate=0.0)

    mean_edge = sum(sell_edges) / len(sell_edges)
    sorted_edges = sorted(sell_edges)
    m = len(sorted_edges)
    if m % 2 == 1:
        median_edge = sorted_edges[m // 2]
    else:
        median_edge = (sorted_edges[m // 2 - 1] + sorted_edges[m // 2]) / 2.0
    edge_win_rate = sum(1 for e in sell_edges if e > 0) / len(sell_edges)

    return VrpStats(
        n=n,
        pass_rate=round(pass_rate, 4),
        mean_edge=round(mean_edge, 6),
        median_edge=round(median_edge, 6),
        edge_win_rate=round(edge_win_rate, 4),
    )


# ── Threshold calibration ─────────────────────────────────────────────────────

def calibrate_threshold(
    samples: list[tuple[Optional[float], Optional[float]]],
    target_pass: float = 0.70,
) -> float:
    """Derive k so that approximately target_pass of usable samples satisfy
    ``realized_vol < k * implied_vol``, i.e. would be classified SELL_PREMIUM.

    Method: for each usable sample compute r = realized_vol / implied_vol; the
    target_pass quantile of r is the k at which exactly that fraction satisfies
    realized_vol < k * implied_vol. The result is clamped to [0.5, 1.5].

    Parameters
    ----------
    samples:
        List of (realized_vol, implied_vol) pairs. Pairs with None or non-positive
        implied_vol are dropped.
    target_pass:
        Desired SELL_PREMIUM pass rate (default 0.70 = 70 %).

    Returns
    -------
    float
        Calibrated k clamped to [_K_MIN, _K_MAX]. Returns DEFAULT_K on empty /
        degenerate input.
    """
    ratios: list[float] = []
    for rv, iv in samples:
        if rv is None or iv is None or iv <= 0 or rv <= 0:
            continue
        try:
            ratios.append(float(rv) / float(iv))
        except (TypeError, ValueError, ZeroDivisionError):
            continue

    if not ratios:
        return DEFAULT_K

    ratios.sort()
    n = len(ratios)

    # target_pass quantile: index = floor(target_pass * n), clamped to valid range.
    idx = min(int(target_pass * n), n - 1)
    k = ratios[idx]

    return float(max(_K_MIN, min(_K_MAX, k)))


# ── Optional DB helper ────────────────────────────────────────────────────────

def samples_from_db(
    source: str = "vix",
    *,
    nifty_id: str = "13",
    vix_id: str = "21",
    timeframe: str = "1d",
    symbol: str = "NIFTY",
) -> list[tuple[float, float]]:
    """Load (realized_vol, implied_vol) pairs from TimescaleDB.

    Two data sources are supported via the ``source`` parameter:

    ``source="vix"`` (DEFAULT — historical backtest path)
        Joins ``index_bars`` (security_id=nifty_id, alias ``a``) with
        ``index_bars`` (security_id=vix_id, alias ``b``) on the same calendar
        date and timeframe.

        * ``realized_vol`` = ``a.realized_vol_20d`` (annualised fraction,
          computed by core/fno_derived and stored in index_bars for the
          **NIFTY 50 SPOT INDEX**, security_id ``"13"`` — consistent with
          cycles_from_db; this is NOT futures bars).
        * ``implied_vol``  = ``b.close / 100.0`` — India VIX is an annualised
          volatility *quoted in percent* (e.g. 13.5 means 13.5 % p.a.);
          dividing by 100 converts it to an annualised fraction directly
          comparable to realized_vol_20d.  VIX/100 is the Phase-0 implied
          baseline.

        This is the Phase-0 historical path.  Rows where
        ``a.realized_vol_20d IS NULL`` or ``b.close IS NULL`` or ``b.close <= 0``
        are silently dropped by the SQL.  After the DB round-trip, any pair
        where ``realized <= 0`` or ``implied <= 0`` is also dropped in Python
        (defence-in-depth against mock data or future schema changes that bypass
        the SQL filter).

    ``source="atm"`` (live / forward path)
        Joins ``option_atm_iv`` (straddle IV for ``symbol``) with
        ``index_bars`` (realized_vol_20d for the NIFTY 50 SPOT INDEX,
        security_id ``"13"``) on the same trading date and timeframe.  This is
        the forward path used once option_atm_iv rows accumulate.  The existing
        DISTINCT ON de-duplication (nearest positive-dte expiry per date) is
        preserved.  After the DB round-trip, any pair where ``realized <= 0``
        or ``implied <= 0`` is silently dropped in Python.

    Rows missing either value are silently dropped in both paths.

    DB imports are lazy (``from sqlalchemy import text`` + ``from db import
    get_session``) so the pure functions above work without a DB connection —
    only this helper touches the DB.

    Parameters
    ----------
    source:
        Data source: ``"vix"`` (default, historical) or ``"atm"`` (live/forward).
        Any other value raises ``ValueError``.
    nifty_id:
        security_id of NIFTY 50 SPOT INDEX in index_bars (default ``"13"``).
        Only used when ``source="vix"``.
    vix_id:
        security_id of India VIX in index_bars (default ``"21"``).
        Only used when ``source="vix"``.
    timeframe:
        Bar timeframe to join on (default ``"1d"``).
    symbol:
        Futures / options symbol for the ``"atm"`` path (default ``"NIFTY"``).

    Returns
    -------
    list of (realized_vol, implied_vol) float tuples sorted ascending by date.
    Tuples where either value is <= 0 are excluded.
    """
    from sqlalchemy import text

    from db import get_session

    if source == "vix":
        sql = text("""
            -- Join NIFTY index_bars (realized_vol_20d) with India VIX index_bars
            -- (close / 100 = annualised vol fraction) on the same calendar date
            -- and timeframe.  India VIX is quoted in percent, so /100 normalises
            -- it to the same annualised-fraction units as realized_vol_20d.
            SELECT
                a.realized_vol_20d           AS realized_vol,
                b.close / 100.0              AS implied_vol
            FROM index_bars a
            JOIN index_bars b
              ON (a.time AT TIME ZONE 'UTC')::date
                 = (b.time AT TIME ZONE 'UTC')::date
             AND a.timeframe = b.timeframe
            WHERE a.security_id        = :nifty_id
              AND b.security_id        = :vix_id
              AND a.timeframe          = :tf
              AND a.realized_vol_20d   IS NOT NULL
              AND b.close              IS NOT NULL
              AND b.close              > 0
            ORDER BY (a.time AT TIME ZONE 'UTC')::date
        """)
        params: dict = {"nifty_id": nifty_id, "vix_id": vix_id, "tf": timeframe}
    elif source == "atm":
        sql = text("""
            -- option_atm_iv can have MULTIPLE rows per trading date (weekly + monthly
            -- expiries).  DISTINCT ON restricts to the nearest positive-dte expiry per
            -- date so each (realized_vol, implied_vol) pair is not duplicated.
            -- Date keys use AT TIME ZONE 'UTC' so the join is independent of DB server TZ.
            SELECT
                fb.realized_vol_20d  AS realized_vol,
                oi.straddle_iv       AS implied_vol
            FROM (
                SELECT DISTINCT ON ((time AT TIME ZONE 'UTC')::date)
                    symbol,
                    (time AT TIME ZONE 'UTC')::date  AS trade_date,
                    straddle_iv
                FROM option_atm_iv
                WHERE symbol      = :sym
                  AND straddle_iv IS NOT NULL
                ORDER BY (time AT TIME ZONE 'UTC')::date, dte ASC
            ) oi
            JOIN futures_bars fb
              ON fb.symbol    = oi.symbol
             AND fb.timeframe = :tf
             AND (fb.time AT TIME ZONE 'UTC')::date = oi.trade_date
            WHERE fb.realized_vol_20d IS NOT NULL
            ORDER BY oi.trade_date
        """)
        params = {"sym": symbol, "tf": timeframe}
    else:
        raise ValueError(f"samples_from_db: unknown source={source!r}; expected 'vix' or 'atm'")

    with get_session() as session:
        rows = session.execute(sql, params).fetchall()

    result: list[tuple[float, float]] = []
    for rv, iv in rows:
        try:
            rv_f = float(rv)
            iv_f = float(iv)
        except (TypeError, ValueError):
            continue
        # Python-side guard: skip degenerate pairs even if the SQL WHERE filter
        # is somehow bypassed (e.g. test mocks, future schema changes).
        if rv_f <= 0 or iv_f <= 0:
            continue
        result.append((rv_f, iv_f))
    return result
