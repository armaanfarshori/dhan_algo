"""F&O dashboard API handlers — five read-only GET endpoints.

All heavy work runs in the thread executor via the shared _db_query helper from
apps.api. DB access uses get_engine() + SQLAlchemy text() with bound parameters;
never f-string SQL interpolation. F&O tables are small — no TimescaleDB hypertable
tricks needed here. The COUNT(*)/ORDER BY-time ban applies only to the `bars` giant.

Unit convention: every vol/IV value returned is an annualised FRACTION.
  • index_bars.realized_vol_20d  — already a fraction  (no division)
  • index_bars.close (VIX, id 21) — percent → divide by 100
  • option_chain_snapshot.iv     — percent → divide by 100
  • option_atm_iv.straddle_iv    — already a fraction  (no division)
"""
from __future__ import annotations

import glob
import json
import logging
import os
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from aiohttp import web

logger = logging.getLogger("dhan.api")
_IST = ZoneInfo("Asia/Kolkata")

_NIFTY_ID = "13"
_VIX_ID = "21"
_TIMEFRAME = "1d"


def _safe_date(value: str) -> Optional[date]:
    """Parse a YYYY-MM-DD date string; return None on any malformed input."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


# ── 1. GET /api/fno/vrp ───────────────────────────────────────────────────────

async def fno_vrp_handler(_r: web.Request) -> web.Response:
    """Latest VRP snapshot: realized vol, VIX, ATM straddle IV, live gate decision.

    Cached 60 s. All vol values returned as annualised fractions.
    If the F&O tables are empty (early bootstrap) returns available:false with
    nulls and gate_decision STAND_ASIDE — never raises to the client.
    """
    from apps.api import _cache_get, _cache_set, _db_query

    cached = _cache_get("fno_vrp", 60)
    if cached:
        return web.json_response(cached)

    def _query():  # noqa: PLR0912 (complexity — deliberate; many nullable paths)
        from db import get_engine
        from ml.fno_vol_gate import DEFAULT_K, gate_decision
        from sqlalchemy import text

        with get_engine().connect() as conn:
            # Latest NIFTY realized vol (already a fraction)
            rv_row = conn.execute(text("""
                SELECT (time AT TIME ZONE 'Asia/Kolkata')::date AS d,
                       realized_vol_20d
                FROM index_bars
                WHERE security_id = :nid AND timeframe = :tf
                  AND realized_vol_20d IS NOT NULL
                ORDER BY time DESC
                LIMIT 1
            """), {"nid": _NIFTY_ID, "tf": _TIMEFRAME}).fetchone()

            # Latest India VIX close (percent — we divide by 100)
            vix_row = conn.execute(text("""
                SELECT (time AT TIME ZONE 'Asia/Kolkata')::date AS d,
                       close
                FROM index_bars
                WHERE security_id = :vid AND timeframe = :tf
                  AND close IS NOT NULL AND close > 0
                ORDER BY time DESC
                LIMIT 1
            """), {"vid": _VIX_ID, "tf": _TIMEFRAME}).fetchone()

            # Latest ATM straddle IV from option_atm_iv (already a fraction)
            atm_row = conn.execute(text("""
                SELECT (time AT TIME ZONE 'Asia/Kolkata')::date AS d,
                       straddle_iv, atm_strike, dte, spot_ref, implied_move,
                       expiry_date
                FROM option_atm_iv
                WHERE straddle_iv IS NOT NULL AND straddle_iv > 0 AND dte > 0
                ORDER BY time DESC
                LIMIT 1
            """)).fetchone()

            # Spot ref from latest option chain snapshot
            spot_row = conn.execute(text("""
                SELECT spot
                FROM option_chain_snapshot
                WHERE underlying_scrip = 13
                ORDER BY snapshot_time DESC
                LIMIT 1
            """)).fetchone()

        realized_vol = float(rv_row[1]) if rv_row and rv_row[1] is not None else None
        realized_as_of = str(rv_row[0]) if rv_row else None

        vix_raw = float(vix_row[1]) if vix_row and vix_row[1] is not None else None
        vix_as_of = str(vix_row[0]) if vix_row else None
        vix_frac = vix_raw / 100.0 if vix_raw is not None else None

        # Prefer fresh ATM straddle IV; fall back to VIX/100
        if atm_row and atm_row[1] is not None:
            implied_vol = float(atm_row[1])
            implied_source = "atm_straddle"
            implied_as_of = str(atm_row[0])
            atm_strike = float(atm_row[2]) if atm_row[2] is not None else None
            dte = int(atm_row[3]) if atm_row[3] is not None else None
            spot_ref_atm = float(atm_row[4]) if atm_row[4] is not None else None
            implied_move = float(atm_row[5]) if atm_row[5] is not None else None
        else:
            implied_vol = vix_frac
            implied_source = "india_vix"
            implied_as_of = vix_as_of
            atm_strike = None
            dte = None
            spot_ref_atm = None
            implied_move = None

        spot_ref = float(spot_row[0]) if spot_row and spot_row[0] is not None else spot_ref_atm

        available = realized_vol is not None and implied_vol is not None

        if available:
            decision = gate_decision(realized_vol, implied_vol, DEFAULT_K)
            spread = implied_vol - realized_vol
            vrp_ratio = realized_vol / implied_vol if implied_vol else None
        else:
            decision = "STAND_ASIDE"
            spread = None
            vrp_ratio = None

        return {
            "ok": True,
            "available": available,
            "realized_vol": realized_vol,
            "realized_as_of": realized_as_of,
            "implied_vol": implied_vol,
            "implied_source": implied_source,
            "implied_as_of": implied_as_of,
            "vix": vix_frac,
            "vix_as_of": vix_as_of,
            "spread": spread,
            "vrp_ratio": vrp_ratio,
            "gate_decision": decision,
            "k": DEFAULT_K,
            "spot_ref": spot_ref,
            "atm_strike": atm_strike,
            "dte": dte,
            "implied_move_pts": implied_move,
            "unit": "fraction",
        }

    try:
        result = await _db_query(_query)
        if result.get("available"):
            _cache_set("fno_vrp", result)  # don't cache the empty-bootstrap shell
        return web.json_response(result)
    except Exception:
        logger.exception("fno_vrp_handler failed")
        return web.json_response({
            "ok": False, "error": "internal error",
            "available": False, "realized_vol": None, "realized_as_of": None,
            "implied_vol": None, "implied_source": None, "implied_as_of": None,
            "vix": None, "vix_as_of": None, "spread": None, "vrp_ratio": None,
            "gate_decision": "STAND_ASIDE", "k": None, "spot_ref": None,
            "atm_strike": None, "dte": None, "implied_move_pts": None,
            "unit": "fraction",
        })


# ── 2. GET /api/fno/vrp/series ────────────────────────────────────────────────

async def fno_vrp_series_handler(request: web.Request) -> web.Response:
    """Historical realized vol + VIX series for the VRP chart.

    GET /api/fno/vrp/series?days=504  (clamp 30–1000, cache 300 s keyed by days).
    Joins index_bars NIFTY (realized_vol_20d, fraction) with India VIX
    (close/100, fraction) on the same IST date over the requested window.
    Empty tables → series:[].
    """
    from apps.api import _cache_get, _cache_set, _db_query

    try:
        days = int(request.rel_url.query.get("days", 504))
    except (ValueError, TypeError):
        days = 504
    days = max(30, min(days, 1000))

    cache_key = f"fno_vrp_series_{days}"
    cached = _cache_get(cache_key, 300)
    if cached:
        return web.json_response(cached)

    def _query():
        from db import get_engine
        from sqlalchemy import text

        with get_engine().connect() as conn:
            rows = conn.execute(text("""
                SELECT
                    (a.time AT TIME ZONE 'Asia/Kolkata')::date AS d,
                    a.realized_vol_20d,
                    b.close / 100.0 AS vix_frac
                FROM index_bars a
                JOIN index_bars b
                  ON (a.time AT TIME ZONE 'Asia/Kolkata')::date
                     = (b.time AT TIME ZONE 'Asia/Kolkata')::date
                 AND a.timeframe = b.timeframe
                WHERE a.security_id = :nid
                  AND b.security_id = :vid
                  AND a.timeframe   = :tf
                  AND a.time >= now() - (:days * interval '1 day')
                  AND b.close IS NOT NULL
                  AND b.close > 0
                ORDER BY d ASC
            """), {
                "nid": _NIFTY_ID,
                "vid": _VIX_ID,
                "tf": _TIMEFRAME,
                "days": days,
            }).fetchall()

        series = []
        for d, rv, vix_frac in rows:
            series.append({
                "date": str(d),
                "realized": float(rv) if rv is not None else None,
                "vix": float(vix_frac) if vix_frac is not None else None,
            })
        return {"ok": True, "unit": "fraction", "count": len(series), "series": series}

    try:
        result = await _db_query(_query)
        if result.get("count"):
            _cache_set(cache_key, result)  # don't cache an empty series
        return web.json_response(result)
    except Exception:
        logger.exception("fno_vrp_series_handler failed")
        return web.json_response({
            "ok": False, "error": "internal error",
            "unit": "fraction", "count": 0, "series": [],
        })


# ── 3. GET /api/fno/paper ─────────────────────────────────────────────────────

async def fno_paper_handler(request: web.Request) -> web.Response:
    """Paper-trade log: summary stats, equity curve, trade list.

    GET /api/fno/paper?limit=200  (clamp 1–500, cache 60 s).
    Summary and equity_curve are computed over RESOLVED trades only.
    Empty table → zeros + [].
    """
    from apps.api import _cache_get, _cache_set, _db_query

    try:
        limit = int(request.rel_url.query.get("limit", 200))
    except (ValueError, TypeError):
        limit = 200
    limit = max(1, min(limit, 500))

    cache_key = f"fno_paper_{limit}"
    cached = _cache_get(cache_key, 60)
    if cached:
        return web.json_response(cached)

    def _query():
        from db import get_engine
        from sqlalchemy import text

        with get_engine().connect() as conn:
            # Summary over RESOLVED trades only
            summary_row = conn.execute(text("""
                SELECT
                    COUNT(*) FILTER (WHERE status = 'RESOLVED')  AS resolved,
                    COUNT(*) FILTER (WHERE status = 'OPEN')      AS open_count,
                    COALESCE(SUM(net_pnl) FILTER (WHERE status = 'RESOLVED'), 0)  AS net_pnl,
                    COUNT(*) FILTER (WHERE status = 'RESOLVED' AND win = TRUE)    AS wins
                FROM fno_paper_trades
            """)).fetchone()

            # Equity curve: cumulative net_pnl over RESOLVED, ordered by resolved_at
            curve_rows = conn.execute(text("""
                SELECT
                    (resolved_at AT TIME ZONE 'Asia/Kolkata')::date AS d,
                    SUM(net_pnl) OVER (ORDER BY resolved_at ASC
                                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS cum_pnl
                FROM fno_paper_trades
                WHERE status = 'RESOLVED' AND resolved_at IS NOT NULL
                ORDER BY resolved_at ASC
            """)).fetchall()

            # Trade list — newest first
            trade_rows = conn.execute(text("""
                SELECT
                    id, status, entry_date, expiry_date, gate_decision,
                    straddle_iv, realized_vol_20d,
                    short_put_k, short_call_k, long_put_k, long_call_k,
                    credit, max_loss, net_pnl, win
                FROM fno_paper_trades
                ORDER BY entry_date DESC, id DESC
                LIMIT :lim
            """), {"lim": limit}).fetchall()

        resolved = int(summary_row[0] or 0)
        open_count = int(summary_row[1] or 0)
        net_pnl = float(summary_row[2] or 0)
        wins = int(summary_row[3] or 0)
        win_rate = (wins / resolved) if resolved > 0 else 0.0
        avg_pnl = (net_pnl / resolved) if resolved > 0 else 0.0

        equity_curve = [
            {"date": str(d), "cum_pnl": float(cum or 0)}
            for d, cum in curve_rows
        ]

        trades = []
        for row in trade_rows:
            (tid, status, entry_date, expiry_date, gate_decision,
             straddle_iv, rv20d,
             spk, sck, lpk, lck,
             credit, max_loss, net_pnl_t, win) = row
            trades.append({
                "id": tid,
                "status": status,
                "entry_date": str(entry_date) if entry_date else None,
                "expiry_date": str(expiry_date) if expiry_date else None,
                "gate_decision": gate_decision,
                "straddle_iv": float(straddle_iv) if straddle_iv is not None else None,
                "realized_vol_20d": float(rv20d) if rv20d is not None else None,
                "short_put_k": float(spk) if spk is not None else None,
                "short_call_k": float(sck) if sck is not None else None,
                "long_put_k": float(lpk) if lpk is not None else None,
                "long_call_k": float(lck) if lck is not None else None,
                "credit": float(credit) if credit is not None else None,
                "max_loss": float(max_loss) if max_loss is not None else None,
                "net_pnl": float(net_pnl_t) if net_pnl_t is not None else None,
                "win": win,
            })

        return {
            "ok": True,
            "summary": {
                "resolved": resolved,
                "open": open_count,
                "net_pnl": net_pnl,
                "win_rate": round(win_rate, 4),
                "wins": wins,
                "avg_pnl": round(avg_pnl, 2),
            },
            "equity_curve": equity_curve,
            "trades": trades,
        }

    try:
        result = await _db_query(_query)
        if result["summary"]["resolved"] or result["summary"]["open"]:
            _cache_set(cache_key, result)  # don't cache a zero-trade bootstrap result
        return web.json_response(result)
    except Exception:
        logger.exception("fno_paper_handler failed")
        return web.json_response({
            "ok": False, "error": "internal error",
            "summary": {
                "resolved": 0, "open": 0, "net_pnl": 0.0,
                "win_rate": 0.0, "wins": 0, "avg_pnl": 0.0,
            },
            "equity_curve": [],
            "trades": [],
        })


# ── 4. GET /api/fno/chain ─────────────────────────────────────────────────────

async def fno_chain_handler(request: web.Request) -> web.Response:
    """Latest NIFTY option chain ATM ± strikes, pivoted CE/PE.

    GET /api/fno/chain?strikes=5&expiry=YYYY-MM-DD
      strikes: 1–15 (default 5), optional expiry date filter.
    Cache 60 s. iv returned as fraction (raw column ÷ 100).
    Empty snapshot → available:false, rows:[].
    """
    from apps.api import _cache_get, _cache_set, _db_query

    try:
        n_strikes = int(request.rel_url.query.get("strikes", 5))
    except (ValueError, TypeError):
        n_strikes = 5
    n_strikes = max(1, min(n_strikes, 15))

    expiry_param = request.rel_url.query.get("expiry", "")
    expiry_date = _safe_date(expiry_param) if expiry_param else None

    cache_key = f"fno_chain_{n_strikes}_{expiry_date or 'auto'}"
    cached = _cache_get(cache_key, 60)
    if cached:
        return web.json_response(cached)

    def _query():
        from db import get_engine
        from sqlalchemy import text

        with get_engine().connect() as conn:
            # Latest snapshot time for NIFTY (underlying_scrip = 13)
            snap_row = conn.execute(text("""
                SELECT MAX(snapshot_time)
                FROM option_chain_snapshot
                WHERE underlying_scrip = 13
            """)).fetchone()

            latest_snap = snap_row[0] if snap_row else None
            if latest_snap is None:
                return None  # signal empty

            # Resolve expiry: use given date or nearest future expiry
            if expiry_date is not None:
                resolved_expiry = expiry_date
            else:
                exp_row = conn.execute(text("""
                    SELECT MIN(expiry_date)
                    FROM option_chain_snapshot
                    WHERE underlying_scrip = 13
                      AND snapshot_time = :snap
                      AND expiry_date >= (now() AT TIME ZONE 'Asia/Kolkata')::date
                """), {"snap": latest_snap}).fetchone()
                resolved_expiry = exp_row[0] if exp_row else None

            if resolved_expiry is None:
                return None

            # All expiries available at this snapshot
            expiry_rows = conn.execute(text("""
                SELECT DISTINCT expiry_date
                FROM option_chain_snapshot
                WHERE underlying_scrip = 13
                  AND snapshot_time = :snap
                ORDER BY expiry_date ASC
            """), {"snap": latest_snap}).fetchall()
            expiries = [str(r[0]) for r in expiry_rows]

            # Spot and ATM strike (median spot from the snapshot)
            spot_row = conn.execute(text("""
                SELECT spot
                FROM option_chain_snapshot
                WHERE underlying_scrip = 13
                  AND snapshot_time = :snap
                  AND expiry_date = :exp
                  AND spot IS NOT NULL
                LIMIT 1
            """), {"snap": latest_snap, "exp": resolved_expiry}).fetchone()

            spot = float(spot_row[0]) if spot_row and spot_row[0] is not None else None
            if spot is None:
                return None

            # ATM strike: strike closest to spot
            atm_row = conn.execute(text("""
                SELECT strike
                FROM option_chain_snapshot
                WHERE underlying_scrip = 13
                  AND snapshot_time = :snap
                  AND expiry_date   = :exp
                ORDER BY ABS(strike - :spot) ASC
                LIMIT 1
            """), {"snap": latest_snap, "exp": resolved_expiry, "spot": spot}).fetchone()

            atm_strike = float(atm_row[0]) if atm_row and atm_row[0] is not None else spot

            # Fetch ATM ± n_strikes rows for both CE and PE
            chain_rows = conn.execute(text("""
                SELECT strike, option_type, ltp, oi, iv, delta
                FROM option_chain_snapshot
                WHERE underlying_scrip = 13
                  AND snapshot_time = :snap
                  AND expiry_date   = :exp
                  AND ABS(strike - :atm) <= :width
                ORDER BY strike ASC
            """), {
                "snap": latest_snap,
                "exp": resolved_expiry,
                "atm": atm_strike,
                "width": atm_strike * 0.05 + n_strikes * 100.0,
                # generous width; we'll trim client-side to n_strikes
            }).fetchall()

        # Pivot by strike
        by_strike: dict = {}
        for strike, opt_type, ltp, oi, iv_raw, delta in chain_rows:
            k = float(strike)
            if k not in by_strike:
                by_strike[k] = {"ce": None, "pe": None}
            leg = {
                "ltp": float(ltp) if ltp is not None else None,
                "oi": int(oi) if oi is not None else None,
                "iv": float(iv_raw) / 100.0 if iv_raw is not None else None,
                "delta": float(delta) if delta is not None else None,
            }
            if opt_type == "CE":
                by_strike[k]["ce"] = leg
            elif opt_type == "PE":
                by_strike[k]["pe"] = leg

        # Sort strikes; pick ATM ± n_strikes
        all_strikes = sorted(by_strike.keys())
        try:
            atm_idx = min(
                range(len(all_strikes)),
                key=lambda i: abs(all_strikes[i] - atm_strike),
            )
        except ValueError:
            atm_idx = 0
        lo = max(0, atm_idx - n_strikes)
        hi = min(len(all_strikes), atm_idx + n_strikes + 1)
        selected = all_strikes[lo:hi]

        rows_out = [
            {"strike": k, "ce": by_strike[k]["ce"], "pe": by_strike[k]["pe"]}
            for k in selected
        ]

        # Staleness: snapshot older than 1440 min (1 trading day)
        now_utc = datetime.now(tz=ZoneInfo("UTC"))
        delta_min = (now_utc - latest_snap).total_seconds() / 60.0
        stale = delta_min > 1440.0

        return {
            "ok": True,
            "available": True,
            "snapshot_time": latest_snap.isoformat(),
            "age_min": round(delta_min, 1),
            "stale": stale,
            "expiry": str(resolved_expiry),
            "spot": spot,
            "atm_strike": atm_strike,
            "iv_unit": "fraction",
            "expiries": expiries,
            "rows": rows_out,
        }

    try:
        result = await _db_query(_query)
        if result is None:
            result = {
                "ok": True,
                "available": False,
                "snapshot_time": None,
                "age_min": None,
                "stale": False,
                "expiry": None,
                "spot": None,
                "atm_strike": None,
                "iv_unit": "fraction",
                "expiries": [],
                "rows": [],
            }
        if result.get("available"):
            _cache_set(cache_key, result)  # don't cache the empty-snapshot shell
        return web.json_response(result)
    except Exception:
        logger.exception("fno_chain_handler failed")
        return web.json_response({
            "ok": False, "error": "internal error",
            "available": False, "snapshot_time": None, "age_min": None,
            "stale": False, "expiry": None, "spot": None, "atm_strike": None,
            "iv_unit": "fraction", "expiries": [], "rows": [],
        })


# ── 5. GET /api/fno/backtest ──────────────────────────────────────────────────

async def fno_backtest_handler(_r: web.Request) -> web.Response:
    """Latest backtest results from research/backtest/results/*.json.

    Reads the newest file by mtime; no DB query needed. Cache 600 s.
    No file → available:false. Never raises to client.
    """
    from apps.api import _cache_get, _cache_set, _db_query

    cached = _cache_get("fno_backtest", 600)
    if cached:
        return web.json_response(cached)

    def _load():
        pattern = os.path.join(
            os.path.dirname(__file__),  # apps/routes/
            "..", "..",                 # repo root
            "research", "backtest", "results", "*.json",
        )
        matches = glob.glob(pattern)
        if not matches:
            return {"ok": True, "available": False}

        newest = max(matches, key=os.path.getmtime)
        with open(newest, encoding="utf-8") as fh:
            data = json.load(fh)

        return {
            "ok": True,
            "available": True,
            "as_of": data.get("as_of"),
            "n_trades": int(data.get("n_trades") or 0),
            "win_rate": float(data.get("win_rate") or 0),
            "profit_factor": float(data.get("profit_factor") or 0),
            "net_pnl": float(data.get("net_pnl") or 0),
            "move_mult": float(data.get("move_mult") or 0),
            "period": str(data.get("period") or ""),
            "vrp_note": str(data.get("vrp_note") or ""),
            "caveats": list(data.get("caveats") or []),
            "go": bool(data.get("go", False)),
            "go_reason": str(data.get("go_reason") or ""),
        }

    try:
        result = await _db_query(_load)
        _cache_set("fno_backtest", result)
        return web.json_response(result)
    except Exception:
        logger.exception("fno_backtest_handler failed")
        return web.json_response({"ok": False, "error": "internal error", "available": False})
