"""F&O forward paper-trade log — real-chain iron-condor entries resolved at expiry.

Each EOD, record_paper_entry() builds one gate-filtered iron-condor per weekly cycle
from the REAL option chain in option_chain_snapshot (actual LTPs), inserts into
fno_paper_trades (ON CONFLICT DO NOTHING so the cycle is entered only once), and
stores entry context.  resolve_paper_trades() marks matured OPEN rows RESOLVED at
expiry, computing gross P&L via resolve_condor and net P&L after costs.

Cost accounting at resolution
------------------------------
NIFTY index options are cash-settled at expiry — there is no physical delivery.
Long ITM options at expiry receive cash equal to their intrinsic value; they are
NOT "exercised" in the equity sense.  However, SEBI/NSE still levies an STT on
the intrinsic value of ITM options at expiry (the "exercise STT" that fno_costs
calls OPTION_EXERCISE_STT_PCT).  We model this by calling condor_costs with the
expiry-intrinsic of the long legs as exercise_intrinsic.  Short legs expire
worthless (no premium to close), so exit costs consist only of the exercise STT
on any ITM long legs.  Brokerage on the exit legs is 0 (no market order placed).

This is an approximation — the real settlement comes from the NSE Final Settlement
Price (FSP), not the closing index level, and some brokers waive exercise STT on
cash-settled indices.  The approach is conservative and well-documented here.

DB imports are lazy (inside each function) so the module-level pure functions
remain DB-free and unit-testable without a database.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from db import get_session  # noqa: PLC0415 — module-level so tests can patch core.fno_paper.get_session
from sqlalchemy import text  # noqa: PLC0415

logger = logging.getLogger("dhan.fno_paper")

_IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> datetime:
    return datetime.now(_IST)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nearest_strike(strikes_dict: dict[int, Any], target: int) -> int:
    """Return the key in strikes_dict nearest to target (min absolute distance)."""
    return min(strikes_dict.keys(), key=lambda k: abs(k - target))


# ---------------------------------------------------------------------------
# 1. record_paper_entry
# ---------------------------------------------------------------------------

def record_paper_entry(
    symbol: str = "NIFTY",
    *,
    nifty_id: str = "13",
    k: Optional[float] = None,
    move_mult: float = 1.5,
    wing_strikes: int = 2,
    step: int = 50,
    lot: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Record one iron-condor paper-trade entry from the real option chain.

    Inserts into fno_paper_trades using ON CONFLICT DO NOTHING so there is
    exactly one entry per (symbol, expiry_date) weekly cycle.

    Parameters
    ----------
    symbol:       Underlying ticker, default ``"NIFTY"``.
    nifty_id:     security_id in index_bars for the underlying index, default ``"13"``.
    k:            Vol-gate threshold (default: DEFAULT_K from ml.fno_vol_gate).
    move_mult:    Short-strike placement multiplier (default 1.5 = 1.5× implied move).
    wing_strikes: Width of long wings in strike grid steps (default 2).
    step:         Strike grid spacing, default 50 (NIFTY).
    lot:          Lot size; defaults to NIFTY_LOT from research.backtest.fno_costs.
    now:          Override for the current IST time (for testing).

    Returns
    -------
    dict with at least ``recorded`` (bool) and context/reason fields.
    """
    # Lazy imports — keep heavier deps (Kronos, pandas) DB-free at import time
    from core.fno_backfill import nifty_atm_strike  # noqa: PLC0415
    from core.fno_derived import implied_move  # noqa: PLC0415
    from ml.fno_vol_gate import DEFAULT_K, SELL_PREMIUM, gate_decision  # noqa: PLC0415
    from research.backtest.fno_condor import build_condor  # noqa: PLC0415
    from research.backtest.fno_costs import NIFTY_LOT, condor_costs  # noqa: PLC0415

    if k is None:
        k = DEFAULT_K
    if lot is None:
        lot = NIFTY_LOT

    # Current IST date
    now_dt = (now or _now_ist()).astimezone(_IST)
    today: date = now_dt.date()

    with get_session() as session:
        # ── 1. Find nearest future expiry in option_chain_snapshot ──────────
        row = session.execute(
            text(
                "SELECT min(expiry_date) FROM option_chain_snapshot "
                "WHERE underlying_scrip = :scrip AND expiry_date >= :today"
            ),
            {"scrip": 13, "today": today},
        ).fetchone()

        expiry: Optional[date] = row[0] if row else None
        if expiry is None:
            logger.info("record_paper_entry: no future expiry in option_chain_snapshot for %s", symbol)
            return {"recorded": False, "reason": "no future expiry in snapshot"}

        # ── 2. Latest snapshot_time for that expiry ──────────────────────────
        snap_row = session.execute(
            text(
                "SELECT max(snapshot_time) FROM option_chain_snapshot "
                "WHERE underlying_scrip = :scrip AND expiry_date = :expiry"
            ),
            {"scrip": 13, "expiry": expiry},
        ).fetchone()

        snapshot_time = snap_row[0] if snap_row else None
        if snapshot_time is None:
            return {"recorded": False, "reason": "no snapshot_time found"}

        # ── 3. Load all rows for (expiry, snapshot_time) ────────────────────
        chain_rows = session.execute(
            text(
                "SELECT strike, option_type, ltp, iv, spot "
                "FROM option_chain_snapshot "
                "WHERE underlying_scrip = :scrip "
                "  AND expiry_date = :expiry "
                "  AND snapshot_time = :snap_time"
            ),
            {"scrip": 13, "expiry": expiry, "snap_time": snapshot_time},
        ).fetchall()

    # Build ce/pe dicts: {int(strike): (ltp, iv_fraction)}
    # iv in option_chain_snapshot is stored as raw percent (per parse_option_chain docstring)
    ce: dict[int, tuple[float, Optional[float]]] = {}
    pe: dict[int, tuple[float, Optional[float]]] = {}
    spot_val: Optional[float] = None

    for row in chain_rows:
        strike_raw, opt_type, ltp, iv_raw, spot_raw = row[0], row[1], row[2], row[3], row[4]
        if spot_val is None and spot_raw is not None:
            spot_val = float(spot_raw)
        strike_int = int(round(float(strike_raw)))
        ltp_f = float(ltp) if ltp is not None else 0.0
        iv_f = float(iv_raw) / 100.0 if iv_raw is not None and float(iv_raw) > 0 else None
        if opt_type == "CE":
            ce[strike_int] = (ltp_f, iv_f)
        elif opt_type == "PE":
            pe[strike_int] = (ltp_f, iv_f)

    if spot_val is None:
        return {"recorded": False, "reason": "no spot in snapshot rows"}

    # ── 4. ATM strike and straddle IV ───────────────────────────────────────
    atm = nifty_atm_strike(spot_val, step)

    # Find ATM call/put — nearest available strike
    atm_ce = ce.get(atm)
    atm_pe = pe.get(atm)

    if atm_ce is None or atm_pe is None or atm_ce[1] is None or atm_pe[1] is None:
        logger.info(
            "record_paper_entry: no ATM iv at strike %d for %s %s", atm, symbol, expiry
        )
        return {"recorded": False, "reason": "no ATM iv"}

    straddle_iv = (atm_ce[1] + atm_pe[1]) / 2.0  # already fractions

    # ── 5. Realized vol from index_bars ─────────────────────────────────────
    with get_session() as session:
        rvol_row = session.execute(
            text(
                "SELECT realized_vol_20d FROM index_bars "
                "WHERE security_id = :nid ORDER BY time DESC LIMIT 1"
            ),
            {"nid": nifty_id},
        ).fetchone()

    realized_vol_20d: Optional[float] = float(rvol_row[0]) if (rvol_row and rvol_row[0] is not None) else None
    if realized_vol_20d is None:
        return {"recorded": False, "reason": "no realized vol"}

    # ── 6. Gate decision ─────────────────────────────────────────────────────
    decision = gate_decision(realized_vol_20d, straddle_iv, k=k)
    if decision != SELL_PREMIUM:
        logger.info(
            "record_paper_entry: gate=%s for %s %s (rv=%.4f, iv=%.4f, k=%.2f) — no entry",
            decision, symbol, expiry, realized_vol_20d, straddle_iv, k,
        )
        return {"recorded": False, "decision": decision}

    # ── 7. Build condor strikes ──────────────────────────────────────────────
    dte = (expiry - today).days
    em = implied_move(spot_val, straddle_iv, dte)
    if em is None or em <= 0:
        return {"recorded": False, "reason": "implied_move is zero or None"}

    strikes = build_condor(spot_val, em, wing_strikes=wing_strikes, step=step, move_mult=move_mult)

    # ── 8. Look up REAL premiums (nearest available strike if exact missing) ─
    spk = strikes["short_put_k"]
    sck = strikes["short_call_k"]
    lpk = strikes["long_put_k"]
    lck = strikes["long_call_k"]

    def _nearest_pe(target: int) -> tuple[int, float]:
        k_near = _nearest_strike(pe, target) if pe else target
        return k_near, pe[k_near][0] if pe else 0.0

    def _nearest_ce(target: int) -> tuple[int, float]:
        k_near = _nearest_strike(ce, target) if ce else target
        return k_near, ce[k_near][0] if ce else 0.0

    spk_actual, sp_prem = _nearest_pe(spk)
    sck_actual, sc_prem = _nearest_ce(sck)
    lpk_actual, lp_prem = _nearest_pe(lpk)
    lck_actual, lc_prem = _nearest_ce(lck)

    credit = (sp_prem + sc_prem) - (lp_prem + lc_prem)
    wing_width = spk_actual - lpk_actual  # == lck_actual - sck_actual (approx)
    max_loss = max(0.0, wing_width - credit) * lot

    # ── 9. Entry costs ───────────────────────────────────────────────────────
    entry_legs = [
        (sp_prem, lot, "SELL"),
        (sc_prem, lot, "SELL"),
        (lp_prem, lot, "BUY"),
        (lc_prem, lot, "BUY"),
    ]
    entry_costs = condor_costs(entry_legs).total

    # ── 10. Raw entry context ─────────────────────────────────────────────────
    raw_context = {
        "symbol": symbol,
        "today": today.isoformat(),
        "expiry": expiry.isoformat(),
        "snapshot_time": snapshot_time.isoformat() if hasattr(snapshot_time, "isoformat") else str(snapshot_time),
        "atm": atm,
        "straddle_iv": straddle_iv,
        "realized_vol_20d": realized_vol_20d,
        "k": k,
        "dte": dte,
        "em": em,
        "strikes": {k2: v for k2, v in strikes.items()},
        "actual_strikes": {
            "short_put_k": spk_actual,
            "short_call_k": sck_actual,
            "long_put_k": lpk_actual,
            "long_call_k": lck_actual,
        },
        "premiums": {
            "short_put": sp_prem,
            "short_call": sc_prem,
            "long_put": lp_prem,
            "long_call": lc_prem,
        },
    }

    # ── 11. INSERT (ON CONFLICT DO NOTHING) ──────────────────────────────────
    inserted = False
    with get_session() as session:
        result = session.execute(
            text("""
                INSERT INTO fno_paper_trades (
                    entry_date, expiry_date, symbol, lot,
                    spot_entry, straddle_iv, realized_vol_20d, gate_decision, k,
                    short_put_k, short_call_k, long_put_k, long_call_k,
                    short_put_prem, short_call_prem, long_put_prem, long_call_prem,
                    credit, max_loss, entry_costs, status, raw
                ) VALUES (
                    :entry_date, :expiry_date, :symbol, :lot,
                    :spot_entry, :straddle_iv, :realized_vol_20d, :gate_decision, :k,
                    :short_put_k, :short_call_k, :long_put_k, :long_call_k,
                    :short_put_prem, :short_call_prem, :long_put_prem, :long_call_prem,
                    :credit, :max_loss, :entry_costs, 'OPEN', :raw::jsonb
                )
                ON CONFLICT ON CONSTRAINT uq_fno_paper_symbol_expiry DO NOTHING
            """),
            {
                "entry_date": today,
                "expiry_date": expiry,
                "symbol": symbol,
                "lot": lot,
                "spot_entry": spot_val,
                "straddle_iv": straddle_iv,
                "realized_vol_20d": realized_vol_20d,
                "gate_decision": decision,
                "k": k,
                "short_put_k": spk_actual,
                "short_call_k": sck_actual,
                "long_put_k": lpk_actual,
                "long_call_k": lck_actual,
                "short_put_prem": sp_prem,
                "short_call_prem": sc_prem,
                "long_put_prem": lp_prem,
                "long_call_prem": lc_prem,
                "credit": credit,
                "max_loss": max_loss,
                "entry_costs": entry_costs,
                "raw": json.dumps(raw_context),
            },
        )
        # rowcount == 1 if INSERT happened; 0 if ON CONFLICT skipped it
        inserted = result.rowcount == 1

    logger.info(
        "record_paper_entry: %s symbol=%s expiry=%s credit=%.2f max_loss=%.2f",
        "INSERTED" if inserted else "SKIPPED (already exists)",
        symbol, expiry, credit, max_loss,
    )
    return {
        "recorded": inserted,
        "decision": SELL_PREMIUM,
        "expiry": expiry,
        "credit": credit,
        "max_loss": max_loss,
    }


# ---------------------------------------------------------------------------
# 2. resolve_paper_trades
# ---------------------------------------------------------------------------

def resolve_paper_trades(
    symbol: str = "NIFTY",
    *,
    nifty_id: str = "13",
    now: Optional[datetime] = None,
) -> int:
    """Resolve all matured OPEN iron-condor paper trades at expiry.

    For each OPEN row whose expiry_date < today:
      1. Fetch the index close on expiry_date from index_bars as the settlement proxy.
         (NSE Final Settlement Price (FSP) would be more accurate; this is a known
         approximation — see module docstring.)
      2. Compute gross_pnl via resolve_condor (per-lot).
      3. Compute exit_costs: at held-to-expiry cash settlement the long legs receive
         intrinsic in cash with no market order needed.  However STT is still levied
         on the intrinsic value of any ITM long legs (exercise STT).  We therefore
         call condor_costs with exercise_intrinsic set to the total ITM intrinsic of
         the LONG legs (only long legs face exercise STT; short legs expire worthless
         or are assigned, with STT charged on the entry SELL already).  No brokerage
         is modelled on exit (cash settlement, no closing order).
      4. net_pnl = gross_pnl - entry_costs - exit_costs.
      5. UPDATE row: status='RESOLVED', expiry_spot, gross_pnl, exit_costs, net_pnl,
         win=(net_pnl>0), resolved_at=now.

    Parameters
    ----------
    symbol:   Ticker filter (currently used for logging; fno_paper_trades has a
              symbol column but the primary filter is status='OPEN' + expiry_date).
    nifty_id: security_id in index_bars for the settlement proxy.
    now:      Override for current IST datetime (for testing).

    Returns
    -------
    int — number of rows resolved in this call.
    """
    from research.backtest.fno_condor import resolve_condor  # noqa: PLC0415

    now_dt = (now or _now_ist()).astimezone(_IST)
    today: date = now_dt.date()
    now_utc = now_dt.astimezone(timezone.utc)

    # ── 1. Fetch all OPEN rows past expiry ────────────────────────────────────
    with get_session() as session:
        open_rows = session.execute(
            text(
                "SELECT id, symbol, lot, expiry_date, "
                "       short_put_k, short_call_k, long_put_k, long_call_k, "
                "       credit, entry_costs "
                "FROM fno_paper_trades "
                "WHERE status = 'OPEN' AND expiry_date < :today AND symbol = :sym"
            ),
            {"today": today, "sym": symbol},
        ).fetchall()

    resolved_count = 0

    for row in open_rows:
        (
            trade_id, trade_symbol, trade_lot, expiry_date,
            spk, sck, lpk, lck,
            credit_raw, entry_costs_raw,
        ) = row

        trade_lot = int(trade_lot)
        expiry_date_d: date = expiry_date if isinstance(expiry_date, date) else expiry_date.date()
        credit_f = float(credit_raw)
        entry_costs_f = float(entry_costs_raw) if entry_costs_raw is not None else 0.0
        spk_i = int(float(spk))
        sck_i = int(float(sck))
        lpk_i = int(float(lpk))
        lck_i = int(float(lck))

        strikes = {
            "short_put_k": spk_i,
            "short_call_k": sck_i,
            "long_put_k": lpk_i,
            "long_call_k": lck_i,
        }

        # ── 2. Settlement proxy: index_bars close on expiry_date ─────────────
        with get_session() as session:
            settle_row = session.execute(
                text(
                    "SELECT close FROM index_bars "
                    "WHERE security_id = :nid "
                    "  AND (time AT TIME ZONE 'UTC')::date = :exp_date "
                    "ORDER BY time DESC LIMIT 1"
                ),
                {"nid": nifty_id, "exp_date": expiry_date_d},
            ).fetchone()

        if settle_row is None or settle_row[0] is None:
            logger.debug(
                "resolve_paper_trades: no close for %s on %s — deferring id=%d",
                trade_symbol, expiry_date_d, trade_id,
            )
            continue

        expiry_spot = float(settle_row[0])

        # ── 3. Gross P&L ─────────────────────────────────────────────────────
        gross_result = resolve_condor(strikes, credit_f, expiry_spot, lot=trade_lot)
        gross_pnl = gross_result["gross_pnl"]

        # ── 4. Exit costs (exercise STT on ITM long legs at settlement) ───────
        # Long put: ITM if expiry_spot < lpk
        lp_intrinsic = max(lpk_i - expiry_spot, 0.0) * trade_lot
        # Long call: ITM if expiry_spot > lck
        lc_intrinsic = max(expiry_spot - lck_i, 0.0) * trade_lot
        exercise_intrinsic = lp_intrinsic + lc_intrinsic

        # Pass 0-premium legs with 0 quantity so condor_costs only charges the
        # exercise STT — no brokerage or other fees on cash-settled exit.
        # We model this by calling condor_costs with a single dummy BUY leg
        # at zero premium (no brokerage = ₹20; only exercise_intrinsic STT matters).
        # Simpler: compute exercise STT directly.
        from research.backtest.fno_costs import OPTION_EXERCISE_STT_PCT  # noqa: PLC0415
        exit_costs = OPTION_EXERCISE_STT_PCT * exercise_intrinsic

        # ── 5. Net P&L and win flag ──────────────────────────────────────────
        net_pnl = gross_pnl - entry_costs_f - exit_costs
        win = net_pnl > 0

        # ── 6. UPDATE row ─────────────────────────────────────────────────────
        with get_session() as session:
            session.execute(
                text("""
                    UPDATE fno_paper_trades SET
                        status       = 'RESOLVED',
                        expiry_spot  = :expiry_spot,
                        gross_pnl    = :gross_pnl,
                        exit_costs   = :exit_costs,
                        net_pnl      = :net_pnl,
                        win          = :win,
                        resolved_at  = :resolved_at
                    WHERE id = :trade_id
                """),
                {
                    "expiry_spot": expiry_spot,
                    "gross_pnl": gross_pnl,
                    "exit_costs": exit_costs,
                    "net_pnl": net_pnl,
                    "win": win,
                    "resolved_at": now_utc,
                    "trade_id": trade_id,
                },
            )

        resolved_count += 1
        logger.info(
            "resolve_paper_trades: RESOLVED id=%d %s expiry=%s spot=%.2f "
            "gross=%.2f exit_costs=%.2f net=%.2f win=%s",
            trade_id, trade_symbol, expiry_date_d, expiry_spot,
            gross_pnl, exit_costs, net_pnl, win,
        )

    return resolved_count


# ---------------------------------------------------------------------------
# 3. paper_summary
# ---------------------------------------------------------------------------

def paper_summary(symbol: str = "NIFTY") -> dict[str, Any]:
    """Return aggregate statistics over all fno_paper_trades rows for symbol.

    Returns
    -------
    dict with keys:
        n_open         — count of OPEN rows
        n_resolved     — count of RESOLVED rows
        win_rate       — fraction of resolved rows with win=True (0.0 if none)
        total_net_pnl  — sum of net_pnl over resolved rows (₹)
        total_max_loss — sum of max_loss over all rows (₹, open exposure)
    """
    with get_session() as session:
        agg = session.execute(
            text("""
                SELECT
                    COUNT(*) FILTER (WHERE status = 'OPEN')                      AS n_open,
                    COUNT(*) FILTER (WHERE status = 'RESOLVED')                  AS n_resolved,
                    COUNT(*) FILTER (WHERE status = 'RESOLVED' AND win = TRUE)   AS n_win,
                    SUM(net_pnl)   FILTER (WHERE status = 'RESOLVED')            AS total_net_pnl,
                    SUM(max_loss)                                                 AS total_max_loss
                FROM fno_paper_trades
                WHERE symbol = :sym
            """),
            {"sym": symbol},
        ).fetchone()

    if agg is None:
        return {
            "n_open": 0, "n_resolved": 0, "win_rate": 0.0,
            "total_net_pnl": 0.0, "total_max_loss": 0.0,
        }

    n_open = int(agg[0] or 0)
    n_resolved = int(agg[1] or 0)
    n_win = int(agg[2] or 0)
    total_net_pnl = float(agg[3] or 0.0)
    total_max_loss = float(agg[4] or 0.0)
    win_rate = n_win / n_resolved if n_resolved > 0 else 0.0

    return {
        "n_open": n_open,
        "n_resolved": n_resolved,
        "win_rate": win_rate,
        "total_net_pnl": total_net_pnl,
        "total_max_loss": total_max_loss,
    }
