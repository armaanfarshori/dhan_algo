"""
Kronos calibration loop — turns persisted gate decisions into evidence.

Two stages:

  fill    For every BUY/SELL signal old enough to have an observable outcome,
          compute the realized return over the forecast horizon (default 30
          minutes — what score_from_db actually predicts) from the bars table
          and write it back into features_snapshot. Idempotent: rows that
          already carry realized_return are skipped.

  report  The decision artifact:
          • Gate value (orb_gate rows): do ALLOW-verdict breakouts outperform
            BLOCK-verdict ones? This is THE re-arm criterion — if ALLOWed
            trades aren't better than BLOCKed ones, the gate adds nothing.
          • Model accuracy by confidence bucket + threshold sweep, fresh vs
            stale data separated (decisions scored on stale bars are noise
            by construction and excluded from the recommendation).
          • A recommended min_confidence, or "insufficient evidence".

Re-arming stays a human decision: set KRONOS_SHADOW_MODE=false + restart
dhan-trader once the report supports it.

CLI:
    python -m ml.calibration fill   [--days 14] [--horizon 30]
    python -m ml.calibration report [--days 30] [--json out.json]
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("dhan.ml.calibration")

HORIZON_MIN_DEFAULT = 30      # matches kronos_pred_len (30 × 1m bars)
ENTRY_BAR_GRACE_MIN = 5       # decision price = last bar within this window
RECOMMEND_MIN_N = 30          # don't recommend a threshold on fewer samples
RECOMMEND_MIN_ACC = 0.55      # ...or below this directional accuracy


# ── Pure helpers (unit-tested, no IO) ─────────────────────────────────────────

def directional_hit(side: str, realized_return: float) -> Optional[bool]:
    """Did the move agree with the side? None for HOLD/unknown sides."""
    if side == "BUY":
        return realized_return > 0
    if side == "SELL":
        return realized_return < 0
    return None


def threshold_sweep(rows: list[dict], thresholds=None) -> list[dict]:
    """Accuracy of 'trust the model when confidence ≥ c' for each c.
    rows need: confidence, hit (bool)."""
    thresholds = thresholds or [round(c * 0.05, 2) for c in range(0, 19)]
    out = []
    for c in thresholds:
        sel = [r for r in rows if r["confidence"] >= c]
        n = len(sel)
        hits = sum(1 for r in sel if r["hit"])
        out.append({"threshold": c, "n": n,
                    "accuracy": round(hits / n, 3) if n else None})
    return out


def recommend_threshold(sweep: list[dict], min_n: int = RECOMMEND_MIN_N,
                        min_acc: float = RECOMMEND_MIN_ACC) -> Optional[float]:
    """Lowest threshold with enough samples and acceptable accuracy.
    Lowest (not highest-accuracy) so the gate keeps the most signal volume."""
    for row in sweep:                      # thresholds ascend
        if row["n"] >= min_n and row["accuracy"] is not None and row["accuracy"] >= min_acc:
            return row["threshold"]
    return None


def gate_value_summary(rows: list[dict]) -> dict:
    """ALLOW vs BLOCK outcome comparison for orb_gate rows.
    rows need: verdict, requested_direction, realized_return."""
    groups: dict[str, list[float]] = {"ALLOW": [], "BLOCK": []}
    for r in rows:
        v = r.get("verdict")
        if v not in groups:
            continue
        # Return signed in the BREAKOUT's direction — the thing the gate judges
        ret = r["realized_return"] if r.get("requested_direction") == "BUY" else -r["realized_return"]
        groups[v].append(ret)

    def stats(rets):
        n = len(rets)
        if not n:
            return {"n": 0, "hit_rate": None, "avg_return_bps": None}
        return {"n": n,
                "hit_rate": round(sum(1 for x in rets if x > 0) / n, 3),
                "avg_return_bps": round(sum(rets) / n * 10_000, 2)}

    allow, block = stats(groups["ALLOW"]), stats(groups["BLOCK"])
    verdict = None
    if allow["n"] >= RECOMMEND_MIN_N and block["n"] >= RECOMMEND_MIN_N:
        verdict = (allow["avg_return_bps"] or 0) > (block["avg_return_bps"] or 0)
    return {"allow": allow, "block": block, "gate_adds_value": verdict}


def bucketize(rows: list[dict], width: float = 0.1) -> list[dict]:
    """Per-confidence-bucket accuracy. rows need: confidence, hit."""
    buckets: dict[float, list[bool]] = {}
    for r in rows:
        b = min(int(r["confidence"] / width) * width, 1.0 - width)
        buckets.setdefault(round(b, 2), []).append(r["hit"])
    out = []
    for b in sorted(buckets):
        hits = buckets[b]
        out.append({"bucket": f"{b:.2f}–{b + width:.2f}", "n": len(hits),
                    "accuracy": round(sum(hits) / len(hits), 3)})
    return out


# ── DB stage 1: fill outcomes ─────────────────────────────────────────────────

def fill_outcomes(days: int = 14, horizon_min: int = HORIZON_MIN_DEFAULT,
                  limit: int = 1000) -> int:
    """Compute realized returns for signals lacking one. Returns rows filled."""
    from sqlalchemy import text
    from db import get_session

    cutoff_new = datetime.now(timezone.utc) - timedelta(minutes=horizon_min)
    cutoff_old = datetime.now(timezone.utc) - timedelta(days=days)

    with get_session() as s:
        pending = s.execute(text("""
            SELECT id, security_id, side, ts FROM signals
            WHERE side IN ('BUY','SELL')
              AND ts BETWEEN :old AND :new
              AND (features_snapshot IS NULL
                   OR NOT (features_snapshot ? 'realized_return'))
            ORDER BY ts DESC LIMIT :lim
        """), {"old": cutoff_old, "new": cutoff_new, "lim": limit}).fetchall()

    filled = 0
    for sig_id, sid, side, ts in pending:
        outcome = _compute_outcome(sid, ts, horizon_min)
        if outcome is None:
            continue
        outcome["hit"] = directional_hit(side, outcome["realized_return"])
        with get_session() as s:
            s.execute(text("""
                UPDATE signals
                SET features_snapshot = COALESCE(features_snapshot, '{}'::jsonb)
                                        || CAST(:o AS jsonb)
                WHERE id = :id
            """), {"o": json.dumps(outcome), "id": sig_id})
        filled += 1

    logger.info("Calibration fill: %d/%d outcomes computed", filled, len(pending))
    return filled


def _compute_outcome(security_id: str, ts, horizon_min: int) -> Optional[dict]:
    """Realized return from decision time to decision+horizon, from 1m bars."""
    from sqlalchemy import text
    from db import get_session
    with get_session() as s:
        entry = s.execute(text("""
            SELECT close, time FROM bars
            WHERE security_id = :sid AND timeframe = '1m'
              AND time <= :ts AND time > :ts - INTERVAL ':grace minutes'
            ORDER BY time DESC LIMIT 1
        """.replace("':grace minutes'", f"'{ENTRY_BAR_GRACE_MIN} minutes'")),
            {"sid": security_id, "ts": ts}).fetchone()
        if not entry:
            return None
        horizon_ts = ts + timedelta(minutes=horizon_min)
        exit_ = s.execute(text("""
            SELECT close, time FROM bars
            WHERE security_id = :sid AND timeframe = '1m'
              AND time > :ts AND time <= :hts
            ORDER BY time DESC LIMIT 1
        """), {"sid": security_id, "ts": ts, "hts": horizon_ts}).fetchone()
        if not exit_:
            return None   # no bars after the decision yet (or session ended)

    entry_px, exit_px = float(entry[0]), float(exit_[0])
    if entry_px <= 0:
        return None
    return {
        "realized_return": round((exit_px - entry_px) / entry_px, 6),
        "outcome_entry_px": entry_px,
        "outcome_exit_px": exit_px,
        "outcome_exit_ts": str(exit_[1]),
        "outcome_horizon_min": horizon_min,
        "outcome_filled_at": datetime.now(timezone.utc).isoformat(),
    }


# ── DB stage 2: report ────────────────────────────────────────────────────────

def load_calibration_rows(days: int = 30) -> list[dict]:
    from sqlalchemy import text
    from db import get_session
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with get_session() as s:
        rows = s.execute(text("""
            SELECT security_id, side, confidence, strategy, ts, features_snapshot
            FROM signals
            WHERE ts >= :cutoff
              AND features_snapshot ? 'realized_return'
        """), {"cutoff": cutoff}).fetchall()
    out = []
    for sid, side, conf, strategy, ts, feat in rows:
        f = feat if isinstance(feat, dict) else json.loads(feat or "{}")
        out.append({
            "security_id": sid, "side": side,
            "confidence": float(conf or 0), "strategy": strategy, "ts": str(ts),
            "realized_return": float(f["realized_return"]),
            "hit": bool(f.get("hit")) if f.get("hit") is not None else None,
            "verdict": f.get("verdict"),
            "requested_direction": f.get("requested_direction"),
            "stale": bool(f.get("stale", True)),   # rows without the flag = old/stale era
        })
    return out


def build_report(days: int = 30) -> dict:
    rows = load_calibration_rows(days)
    directional = [r for r in rows if r["hit"] is not None]
    fresh = [r for r in directional if not r["stale"]]
    stale = [r for r in directional if r["stale"]]
    gate_rows = [r for r in rows if r["strategy"] == "orb_gate"
                 and r["requested_direction"] in ("BUY", "SELL")]
    gate_fresh = [r for r in gate_rows if not r["stale"]]

    sweep = threshold_sweep(fresh)
    rec = recommend_threshold(sweep)
    gate = gate_value_summary(gate_fresh)

    def acc(rs):
        return round(sum(1 for r in rs if r["hit"]) / len(rs), 3) if rs else None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "rows_with_outcomes": len(rows),
        "model_accuracy": {
            "fresh": {"n": len(fresh), "accuracy": acc(fresh)},
            "stale_excluded": {"n": len(stale), "accuracy": acc(stale)},
        },
        "confidence_buckets_fresh": bucketize(fresh),
        "threshold_sweep_fresh": [r for r in sweep if r["n"] > 0],
        "gate_value": gate,
        "recommended_min_confidence": rec,
        "recommendation": _verdict_text(rec, gate, len(fresh)),
    }


def _verdict_text(rec: Optional[float], gate: dict, n_fresh: int) -> str:
    if n_fresh < RECOMMEND_MIN_N:
        return (f"INSUFFICIENT EVIDENCE — only {n_fresh} fresh-data outcomes "
                f"(need ≥{RECOMMEND_MIN_N}). Keep the gate in shadow mode.")
    if gate.get("gate_adds_value") is False:
        return ("DO NOT ARM — ALLOWed breakouts did not outperform BLOCKed ones. "
                "The gate adds no value at any threshold yet; revisit after fine-tuning.")
    if rec is None:
        return ("DO NOT ARM — no confidence threshold reached "
                f"{RECOMMEND_MIN_ACC:.0%} accuracy with enough samples. Keep shadow mode.")
    return (f"EVIDENCE SUPPORTS ARMING at KRONOS_MIN_CONFIDENCE={rec} — review the "
            f"sweep, then set KRONOS_SHADOW_MODE=false and restart dhan-trader.")


def print_report(rep: dict):
    print("\n" + "=" * 64)
    print(f"  KRONOS CALIBRATION — last {rep['window_days']} days")
    print("=" * 64)
    ma = rep["model_accuracy"]
    print(f"  outcomes: {rep['rows_with_outcomes']}   "
          f"fresh: {ma['fresh']['n']} (acc {ma['fresh']['accuracy']})   "
          f"stale excluded: {ma['stale_excluded']['n']}")
    print("-" * 64)
    print("  confidence buckets (fresh data only):")
    for b in rep["confidence_buckets_fresh"]:
        print(f"    {b['bucket']}   n={b['n']:<5} acc={b['accuracy']}")
    print("-" * 64)
    gv = rep["gate_value"]
    print(f"  gate value  ALLOW: n={gv['allow']['n']} hit={gv['allow']['hit_rate']} "
          f"avg={gv['allow']['avg_return_bps']}bps")
    print(f"              BLOCK: n={gv['block']['n']} hit={gv['block']['hit_rate']} "
          f"avg={gv['block']['avg_return_bps']}bps")
    print("-" * 64)
    print(f"  ► {rep['recommendation']}")
    print("=" * 64 + "\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
                        datefmt="%H:%M:%S")
    p = argparse.ArgumentParser(description="Kronos gate calibration")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fill", help="compute realized outcomes for pending signals")
    f.add_argument("--days", type=int, default=14)
    f.add_argument("--horizon", type=int, default=HORIZON_MIN_DEFAULT)
    r = sub.add_parser("report", help="print the calibration report")
    r.add_argument("--days", type=int, default=30)
    r.add_argument("--json", help="also write the report to this file")
    args = p.parse_args()

    from config import get_config
    from db import init_db
    init_db(get_config().db_url)

    if args.cmd == "fill":
        n = fill_outcomes(days=args.days, horizon_min=args.horizon)
        print(f"filled {n} outcomes")
    else:
        rep = build_report(days=args.days)
        print_report(rep)
        if args.json:
            with open(args.json, "w") as fh:
                json.dump(rep, fh, indent=2)


if __name__ == "__main__":
    main()
