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
import math
from dataclasses import dataclass
from datetime import date
from typing import Optional, Sequence

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
        ``index_bars`` (security_id=nifty_id, default ``"13"``) on the same
        trading date and timeframe.

        * ``realized_vol`` = ``index_bars.realized_vol_20d`` (annualised
          fraction, computed by core/fno_derived from NIFTY 50 SPOT INDEX
          closes; NOT futures bars — futures_bars is unpopulated).
        * ``implied_vol``  = ``option_atm_iv.straddle_iv`` (annualised
          fraction already stored as such by core/fno_backfill — do NOT
          divide by 100, unlike the VIX path).

        ``DISTINCT ON`` de-duplication picks the nearest positive-dte expiry
        per trading date so each date yields exactly one pair.  After the DB
        round-trip, any pair where ``realized <= 0`` or ``implied <= 0`` is
        silently dropped in Python.

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
        Used by both ``source="vix"`` and ``source="atm"`` (realized-vol base
        always comes from the spot index).
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
            --
            -- realized_vol_20d comes from index_bars (NIFTY 50 SPOT INDEX, security_id
            -- :nifty_id).  futures_bars is never used here — it is unpopulated and would
            -- always return zero rows.  straddle_iv is already stored as an annualised
            -- fraction by core/fno_backfill; do NOT divide by 100.
            SELECT
                ib.realized_vol_20d  AS realized_vol,
                oi.straddle_iv       AS implied_vol
            FROM (
                SELECT DISTINCT ON ((time AT TIME ZONE 'UTC')::date)
                    (time AT TIME ZONE 'UTC')::date  AS trade_date,
                    straddle_iv
                FROM option_atm_iv
                WHERE symbol      = :sym
                  AND straddle_iv IS NOT NULL
                  AND straddle_iv > 0
                  AND dte         > 0
                ORDER BY (time AT TIME ZONE 'UTC')::date, dte ASC
            ) oi
            JOIN index_bars ib
              ON ib.security_id = :nifty_id
             AND ib.timeframe   = :tf
             AND (ib.time AT TIME ZONE 'UTC')::date = oi.trade_date
            WHERE ib.realized_vol_20d IS NOT NULL
            ORDER BY oi.trade_date
        """)
        params = {"sym": symbol, "nifty_id": nifty_id, "tf": timeframe}
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


# ═══════════════════════════════════════════════════════════════════════════════
#  GATE-V2 — Yang-Zhang RV + VRP-percentile + backwardation/event veto
# ═══════════════════════════════════════════════════════════════════════════════
#
# GATE-V2 layers four independent, AND-composed refinements ON TOP of the v1
# `gate_decision` VRP check. The v1 gate stays the spine (and the public,
# unchanged contract); GATE-V2 is a strictly-more-conservative wrapper:
#
#     v1 says SELL_PREMIUM            (necessary)
#   AND VRP-spread in a rich percentile of its trailing window
#   AND term structure is NOT in backwardation (front IV ≤ next IV)
#   AND today is NOT an event/expiry stand-aside day
#       → SELL_PREMIUM, else STAND_ASIDE.
#
# Every component is a PURE function and FAIL-OPEN: any None / degenerate /
# insufficient-data input degrades to STAND_ASIDE (for the gate) or a neutral /
# non-vetoing result (for the percentile + realized-vol helpers) and NEVER
# raises. This mirrors the v1 contract and the equity gate's fail-open rule
# (a missing signal never silently routes into an aggressive trade).
#
# INDEX-AGNOSTIC: nothing here hardcodes NIFTY. All thresholds/windows live in a
# `GateV2Config` dataclass and the realized-vol calendar-basis rebasing reuses
# research.backtest.fno_condor.realized_vol_to_calendar_basis (PR #74) so the RV
# is apples-to-apples with the calendar-annualised implied vol regardless of
# underlying. Per-index overrides ride on GateV2Config, never on constants.
#
# UNITS: all vols are annualised fractions (0.12 = 12% p.a.), same as v1.

# Selectable realized-vol estimator sources.
RV_SOURCE_CLOSE_CLOSE = "close_close"  # √252 close-to-close (core.fno_derived parity)
RV_SOURCE_YANG_ZHANG = "yang_zhang"    # OHLC Yang-Zhang (lower-variance estimator)

# Default trailing window + minimum sample count for the VRP-percentile gate.
_VRP_PCTL_WINDOW: int = 252        # ~1 trading year of trailing VRP observations
_VRP_PCTL_MIN_OBS: int = 60        # need ≥60 obs before the percentile is meaningful
# Sell premium only when the *current* VRP spread sits at/above this percentile
# of its trailing window (richer = more positive IV-RV). 0.50 = top half.
_VRP_RICH_PERCENTILE: float = 0.50

# Annualisation conventions (kept local so this section is self-contained; they
# match core.fno_derived / fno_condor exactly).
_TRADING_DAYS: int = 252
_CALENDAR_DAYS: int = 365


@dataclass(frozen=True)
class GateV2Config:
    """All GATE-V2 thresholds / windows — per-index overridable, no NIFTY constants.

    Pass a customised instance per underlying (the orchestrator owns the registry
    of per-index configs); the defaults reproduce the NIFTY Phase-0 behaviour.

    Attributes
    ----------
    k:
        v1 VRP threshold passed straight to ``gate_decision`` (default
        :data:`DEFAULT_K`). GATE-V2 inherits the engine's calibrated ``k`` — it
        does not invent its own.
    rv_source:
        Realized-vol estimator used when GATE-V2 computes RV from OHLC history
        (:data:`RV_SOURCE_CLOSE_CLOSE` or :data:`RV_SOURCE_YANG_ZHANG`). Only
        used by :func:`realized_vol` / callers that pass OHLC bars; when the
        caller already has an ``rv`` scalar this is ignored.
    rv_window:
        Trailing window (in bars) for the realized-vol estimator.
    vrp_window:
        Trailing window (in observations) for the VRP-percentile gate.
    vrp_min_obs:
        Minimum trailing observations before the percentile gate engages. Below
        this the percentile sub-gate is UNKNOWN → fail-open (does NOT veto; the
        v1 gate alone then decides, matching "missing signal never blocks").
    vrp_rich_percentile:
        Current VRP spread must sit at/above this percentile of the trailing
        window to qualify as "rich" (default 0.50 = top half). Calibratable.
    require_percentile:
        When True (default) the percentile sub-gate, once it has enough obs,
        must pass for SELL_PREMIUM. When False it is advisory only (logged,
        never vetoes) — useful while history is still accruing.
    """

    k: float = DEFAULT_K
    rv_source: str = RV_SOURCE_CLOSE_CLOSE
    rv_window: int = 20
    vrp_window: int = _VRP_PCTL_WINDOW
    vrp_min_obs: int = _VRP_PCTL_MIN_OBS
    vrp_rich_percentile: float = _VRP_RICH_PERCENTILE
    require_percentile: bool = True


# ── 1. Realized-vol estimators (close-close + Yang-Zhang), calendar-basis aware ──

def _rebase_to_calendar(
    vol_trading: Optional[float],
    trading_days: int = _TRADING_DAYS,
    calendar_days: int = _CALENDAR_DAYS,
) -> Optional[float]:
    """Rebase a √(trading-day)-annualised vol onto the calendar (365) basis.

    Thin local mirror of
    ``research.backtest.fno_condor.realized_vol_to_calendar_basis`` so a realized
    vol annualised with √252 is directly comparable to a calendar-annualised
    implied vol (India VIX / ATM straddle IV). ``None`` passes through. Never
    raises. The canonical implementation lives in fno_condor (PR #74); this copy
    keeps ml/ import-free of research/ at module load while staying numerically
    identical (√(trading/calendar)).
    """
    if vol_trading is None:
        return None
    try:
        return float(vol_trading) * math.sqrt(trading_days / calendar_days)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def close_close_realized_vol(
    closes: Sequence[float],
    window: int = 20,
    trading_days: int = _TRADING_DAYS,
) -> Optional[float]:
    """Trailing close-to-close annualised realized vol over the LAST ``window``
    log-returns (√``trading_days`` annualised; sample stdev, ddof=1).

    Returns ``None`` (fail-open) when there are fewer than ``window``+1 usable
    closes, when any close in the window is non-positive/None, or on any error.
    Numerically consistent with ``core.fno_derived.realized_vol_series`` for the
    most-recent bar (same log-return + √252 + ddof=1 convention).
    """
    try:
        if window < 2 or closes is None:
            return None
        vals = [c for c in closes]
        if len(vals) < window + 1:
            return None
        tail = vals[-(window + 1):]
        rets: list[float] = []
        for prev, cur in zip(tail, tail[1:]):
            if prev is None or cur is None or prev <= 0 or cur <= 0:
                return None  # data gap inside the window → fail-open
            rets.append(math.log(cur / prev))
        if len(rets) < 2:
            return None
        mean_r = sum(rets) / len(rets)
        var = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var) * math.sqrt(trading_days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("close_close_realized_vol: error (%s) → None", exc)
        return None


def yang_zhang_realized_vol(
    bars: Sequence[tuple[float, float, float, float]],
    window: int = 20,
    trading_days: int = _TRADING_DAYS,
) -> Optional[float]:
    """Yang-Zhang OHLC realized-vol estimator (annualised fraction).

    Yang-Zhang (2000) is the minimum-variance, drift-independent, gap-handling
    realized-vol estimator. It combines three variances over the LAST ``window``
    bars (each ``bar`` = ``(open, high, low, close)``)::

        σ²_YZ = σ²_overnight + k·σ²_open_close + (1−k)·σ²_RS

    where overnight = var(ln O_t / C_{t-1}), open-close = var(ln C_t / O_t),
    RS = mean Rogers-Satchell per-bar term, and
    ``k = 0.34 / (1.34 + (n+1)/(n−1))`` (n = number of bars used).

    Less noisy than close-close (uses the full OHLC range), which is exactly why
    it is offered as a selectable RV source for the gate.

    Returns ``None`` (fail-open) when there are fewer than ``window``+1 bars (an
    overnight term needs the prior close), when any OHLC value is
    non-positive/None/misordered, or on any error. NEVER raises.
    """
    try:
        if window < 2 or bars is None:
            return None
        rows = [b for b in bars]
        if len(rows) < window + 1:
            return None
        tail = rows[-(window + 1):]   # +1 for the prior close (overnight term)

        # Validate + unpack OHLC; reject degenerate/misordered bars (fail-open).
        ohlc: list[tuple[float, float, float, float]] = []
        for b in tail:
            if b is None or len(b) < 4:
                return None
            o, h, low, c = float(b[0]), float(b[1]), float(b[2]), float(b[3])
            if o <= 0 or h <= 0 or low <= 0 or c <= 0:
                return None
            if h < low or h < o or h < c or low > o or low > c:
                return None  # impossible bar → data error → fail-open
            ohlc.append((o, h, low, c))

        n = len(ohlc) - 1  # number of bars contributing (need prior close)
        if n < 2:
            return None

        overnight: list[float] = []   # ln(O_t / C_{t-1})
        open_close: list[float] = []  # ln(C_t / O_t)
        rs: list[float] = []          # Rogers-Satchell per bar
        for i in range(1, len(ohlc)):
            o, h, low, c = ohlc[i]
            prev_c = ohlc[i - 1][3]
            overnight.append(math.log(o / prev_c))
            open_close.append(math.log(c / o))
            # Rogers-Satchell: ln(H/C)·ln(H/O) + ln(L/C)·ln(L/O)
            rs.append(
                math.log(h / c) * math.log(h / o)
                + math.log(low / c) * math.log(low / o)
            )

        def _var(xs: list[float]) -> float:
            m = sum(xs) / len(xs)
            return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)

        var_overnight = _var(overnight)
        var_open_close = _var(open_close)
        var_rs = sum(rs) / len(rs)

        k = 0.34 / (1.34 + (n + 1) / (n - 1))
        sigma2 = var_overnight + k * var_open_close + (1.0 - k) * var_rs
        if sigma2 < 0:
            sigma2 = 0.0  # numerical guard — variance can dip slightly negative
        return math.sqrt(sigma2) * math.sqrt(trading_days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("yang_zhang_realized_vol: error (%s) → None", exc)
        return None


def realized_vol(
    closes: Optional[Sequence[float]] = None,
    ohlc_bars: Optional[Sequence[tuple[float, float, float, float]]] = None,
    cfg: GateV2Config = GateV2Config(),
) -> Optional[float]:
    """Compute trailing realized vol with the estimator selected by ``cfg.rv_source``.

    * ``RV_SOURCE_YANG_ZHANG`` → :func:`yang_zhang_realized_vol` on ``ohlc_bars``.
    * ``RV_SOURCE_CLOSE_CLOSE`` (default) → :func:`close_close_realized_vol` on
      ``closes`` (or the close column of ``ohlc_bars`` if ``closes`` is None).

    Returns ``None`` (fail-open) when the required input is missing for the chosen
    source, or on any error. Both estimators annualise with √252 (trading basis);
    rebase to calendar with :func:`_rebase_to_calendar` before comparing to
    implied vol (the gate functions do this internally).
    """
    try:
        if cfg.rv_source == RV_SOURCE_YANG_ZHANG:
            if ohlc_bars is None:
                return None
            return yang_zhang_realized_vol(ohlc_bars, window=cfg.rv_window)
        # close-close (default)
        src = closes
        if src is None and ohlc_bars is not None:
            src = [b[3] for b in ohlc_bars if b is not None and len(b) >= 4]
        if src is None:
            return None
        return close_close_realized_vol(src, window=cfg.rv_window)
    except Exception as exc:  # noqa: BLE001
        logger.warning("realized_vol: error (%s) → None", exc)
        return None


# ── 2. VRP-percentile gate ──────────────────────────────────────────────────────

def vrp_spread(realized_vol_in: Optional[float], implied_vol: Optional[float]) -> Optional[float]:
    """IV − RV spread (vol points). Positive = implied richer than realized.

    ``None`` on any None / non-positive implied vol / negative realized vol.
    Never raises. (Spread, not ratio: the percentile gate ranks spreads so the
    sign convention "more positive = richer" is intuitive.)
    """
    try:
        if realized_vol_in is None or implied_vol is None:
            return None
        rv = float(realized_vol_in)
        iv = float(implied_vol)
        if iv <= 0 or rv < 0:
            return None
        return iv - rv
    except (TypeError, ValueError):
        return None


def percentile_rank(value: Optional[float], window: Sequence[Optional[float]]) -> Optional[float]:
    """Percentile rank (0..1) of ``value`` within ``window`` (fraction strictly below).

    Filters None/non-finite entries from ``window`` first. Returns ``None`` when
    ``value`` is None/non-finite or the cleaned window is empty. Never raises.
    PIT discipline is the CALLER's responsibility — pass only trailing
    observations ``≤`` the decision date (do not include the future).
    """
    try:
        if value is None:
            return None
        v = float(value)
        if not math.isfinite(v):
            return None
        vals = [
            float(x) for x in window
            if x is not None and math.isfinite(float(x))
        ]
        if not vals:
            return None
        below = sum(1 for x in vals if x < v)
        return below / len(vals)
    except (TypeError, ValueError):
        return None


def vrp_percentile_gate(
    current_spread: Optional[float],
    trailing_spreads: Sequence[Optional[float]],
    cfg: GateV2Config = GateV2Config(),
) -> tuple[bool, Optional[float]]:
    """Is the current VRP spread RICH (high percentile) vs its trailing window?

    Returns ``(rich, pct)``:
      * ``rich`` True when ``pct >= cfg.vrp_rich_percentile`` (premium is rich
        relative to its own recent history → sell-premium favoured).
      * ``pct`` is the percentile rank (0..1), or ``None`` when there are fewer
        than ``cfg.vrp_min_obs`` usable trailing spreads or the current spread
        is None.

    FAIL-OPEN: insufficient history / None spread → ``(True, None)`` — the
    sub-gate does NOT veto (a missing signal never blocks; the v1 gate still
    decides). When ``cfg.require_percentile`` is False the boolean is always
    ``True`` (advisory mode). Never raises.
    """
    try:
        if not cfg.require_percentile:
            return True, percentile_rank(current_spread, trailing_spreads)
        usable = [
            x for x in trailing_spreads
            if x is not None and math.isfinite(float(x))
        ]
        if current_spread is None or len(usable) < cfg.vrp_min_obs:
            return True, None  # fail-open: not enough info to veto
        pct = percentile_rank(current_spread, usable)
        if pct is None:
            return True, None
        return (pct >= cfg.vrp_rich_percentile), pct
    except Exception as exc:  # noqa: BLE001
        logger.warning("vrp_percentile_gate: error (%s) → fail-open allow", exc)
        return True, None


# ── 3. Regime / backwardation veto (term structure) ─────────────────────────────

def is_backwardation(
    front_iv: Optional[float],
    next_iv: Optional[float],
    min_ratio: float = 1.0,
) -> bool:
    """Front-expiry IV in backwardation vs the next expiry → stress regime.

    Backwardation (front IV > next IV by more than ``min_ratio``) signals acute
    near-term stress (event/crash pricing): premium-selling is dangerous there.
    Returns True ONLY when both IVs are usable positives AND
    ``front_iv > min_ratio * next_iv``.

    FAIL-OPEN: any None / non-positive IV → ``False`` (do NOT veto on missing
    term-structure data; the gate then relies on the other checks). Never raises.
    """
    try:
        if front_iv is None or next_iv is None:
            return False
        f = float(front_iv)
        n = float(next_iv)
        if f <= 0 or n <= 0:
            return False
        return f > min_ratio * n
    except (TypeError, ValueError):
        return False


# ── 4. Event veto (expiry day + configurable event-date list) ───────────────────

def is_event_day(
    cycle_date: Optional[date],
    expiry_date: Optional[date] = None,
    event_dates: Optional[Sequence[date]] = None,
) -> bool:
    """Stand-aside day check: expiry day OR a listed event date (RBI/FOMC/results).

    Returns True when ``cycle_date`` equals ``expiry_date`` (entering on expiry
    day is gamma-heavy / settlement-risky) or appears in ``event_dates``
    (configurable hook; empty/None default → no event vetoes). Date-only
    comparison (datetimes are normalised to ``.date()``).

    FAIL-OPEN: ``cycle_date`` None → ``False`` (cannot identify an event → do not
    veto). Never raises.
    """
    try:
        if cycle_date is None:
            return False
        # `date` has no .hour; datetime does. Normalise datetimes to date only.
        cd = cycle_date.date() if hasattr(cycle_date, "hour") else cycle_date
        if expiry_date is not None:
            ed = expiry_date.date() if hasattr(expiry_date, "hour") else expiry_date
            if cd == ed:
                return True
        if event_dates:
            for e in event_dates:
                if e is None:
                    continue
                ee = e.date() if hasattr(e, "hour") else e
                if cd == ee:
                    return True
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("is_event_day: error (%s) → no veto", exc)
        return False


# ── GATE-V2 composed decision ───────────────────────────────────────────────────

@dataclass(frozen=True)
class GateV2Result:
    """Full GATE-V2 verdict + the per-component diagnostics (for logging/calibration)."""

    decision: str               # SELL_PREMIUM | BUY_PREMIUM | STAND_ASIDE
    v1_decision: str            # the underlying gate_decision verdict
    vrp_rich: bool              # VRP-percentile sub-gate result (True = rich/allow)
    vrp_percentile: Optional[float]  # percentile rank of current spread (None if N/A)
    current_spread: Optional[float]  # iv - rv (calendar-rebased rv)
    backwardation_veto: bool    # term-structure stress veto fired
    event_veto: bool            # expiry/event-date veto fired
    reason: str = ""            # human-readable explanation


def gate_v2_decision(
    realized_vol_in: Optional[float],
    implied_vol: Optional[float],
    *,
    trailing_spreads: Optional[Sequence[Optional[float]]] = None,
    front_iv: Optional[float] = None,
    next_iv: Optional[float] = None,
    cycle_date: Optional[date] = None,
    expiry_date: Optional[date] = None,
    event_dates: Optional[Sequence[date]] = None,
    rv_is_trading_basis: bool = True,
    cfg: GateV2Config = GateV2Config(),
) -> GateV2Result:
    """AND-composed GATE-V2 decision (Yang-Zhang/close RV + VRP-percentile +
    backwardation veto + event veto), layered on top of v1 ``gate_decision``.

    The v1 VRP gate is the spine: GATE-V2 can only ever make it MORE
    conservative (turn a v1 SELL_PREMIUM into STAND_ASIDE). A v1 BUY_PREMIUM /
    STAND_ASIDE passes through unchanged (the refinements only refine the
    *sell-premium* decision — long-vol/aside is already conservative).

    Composition (SELL_PREMIUM requires ALL):
      1. v1 ``gate_decision`` == SELL_PREMIUM.
      2. VRP spread sits in a rich percentile of its trailing window
         (:func:`vrp_percentile_gate`; fail-open when history is thin).
      3. NOT backwardation (:func:`is_backwardation`; fail-open on missing IVs).
      4. NOT an event/expiry day (:func:`is_event_day`; empty default = no veto).

    Parameters
    ----------
    realized_vol_in, implied_vol:
        Annualised fractions. If ``rv_is_trading_basis`` (default) the realized
        vol is rebased onto the calendar (365) basis before comparison + spread,
        so it is apples-to-apples with the calendar-annualised implied vol
        (reuses PR #74's convention). Pass ``rv_is_trading_basis=False`` if the
        caller already rebased.
    trailing_spreads:
        Trailing (PIT, ≤ decision-date) IV−RV spreads for the percentile gate.
        ``None``/short → percentile sub-gate fails open (does not veto).
    front_iv, next_iv:
        Front- vs next-expiry ATM IV for the term-structure veto. Prefer real
        ATM IV (resolve_iv_source / option_atm_iv); VIX is the fallback front IV.
    cycle_date, expiry_date, event_dates:
        Event-veto inputs (expiry day + configurable RBI/FOMC/results list).
    cfg:
        :class:`GateV2Config` — per-index thresholds/windows (index-agnostic).

    Returns
    -------
    GateV2Result
        Full verdict + diagnostics. FAIL-OPEN: any degenerate input degrades to
        STAND_ASIDE (never an aggressive trade) and NEVER raises.
    """
    try:
        # Rebase RV to calendar basis so spread + v1 gate compare like-for-like.
        rv_cal = _rebase_to_calendar(realized_vol_in) if rv_is_trading_basis else realized_vol_in

        v1 = gate_decision(rv_cal, implied_vol, k=cfg.k)

        spread = vrp_spread(rv_cal, implied_vol)
        rich, pct = vrp_percentile_gate(spread, trailing_spreads or [], cfg=cfg)
        backwardation = is_backwardation(front_iv, next_iv)
        event = is_event_day(cycle_date, expiry_date, event_dates)

        # Non-sell verdicts pass through unchanged (already conservative).
        if v1 != SELL_PREMIUM:
            return GateV2Result(
                decision=v1, v1_decision=v1, vrp_rich=rich, vrp_percentile=pct,
                current_spread=spread, backwardation_veto=backwardation,
                event_veto=event,
                reason=f"v1={v1} (not SELL); passes through unchanged",
            )

        vetoes: list[str] = []
        if not rich:
            vetoes.append(
                f"VRP not rich (pct={pct:.2f} < {cfg.vrp_rich_percentile:.2f})"
                if pct is not None else "VRP not rich"
            )
        if backwardation:
            vetoes.append(f"backwardation (front_iv={front_iv} > next_iv={next_iv})")
        if event:
            vetoes.append("event/expiry day")

        if vetoes:
            return GateV2Result(
                decision=STAND_ASIDE, v1_decision=v1, vrp_rich=rich,
                vrp_percentile=pct, current_spread=spread,
                backwardation_veto=backwardation, event_veto=event,
                reason="v1=SELL but GATE-V2 vetoed: " + "; ".join(vetoes),
            )

        return GateV2Result(
            decision=SELL_PREMIUM, v1_decision=v1, vrp_rich=rich,
            vrp_percentile=pct, current_spread=spread,
            backwardation_veto=False, event_veto=False,
            reason=(
                "SELL_PREMIUM — v1 SELL + VRP rich"
                + (f" (pct={pct:.2f})" if pct is not None else " (pct N/A, fail-open)")
                + " + no backwardation + no event"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("gate_v2_decision: unexpected error (%s) → STAND_ASIDE", exc)
        return GateV2Result(
            decision=STAND_ASIDE, v1_decision=STAND_ASIDE, vrp_rich=False,
            vrp_percentile=None, current_spread=None, backwardation_veto=False,
            event_veto=False, reason=f"fail-open: {exc}",
        )
