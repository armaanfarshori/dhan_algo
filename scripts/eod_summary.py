#!/usr/bin/env python3
"""
EOD Telegram summary — runs from plain cron after square-off (no LLM).

One message: day's trades + P&L, gate decisions, calibration verdict,
backfill progress. Replaces the Hermes EOD-review cron at zero cost.

Cron (agent): 30 11 * * 1-5  (17:00 IST weekdays)
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import text

from config import get_config
from core.notify import send
from db import get_session, init_db

init_db(get_config().db_url)

lines = [f"📋 EOD — {datetime.now(timezone.utc).astimezone().strftime('%d %b %Y')}"]

# ── Trades + P&L ───────────────────────────────────────────────────────────────
try:
    with get_session() as s:
        n_trades, pnl, wins = s.execute(text("""
            SELECT COUNT(*) FILTER (WHERE status='CLOSED'),
                   COALESCE(SUM(pnl) FILTER (WHERE status='CLOSED'), 0),
                   COUNT(*) FILTER (WHERE status='CLOSED' AND pnl > 0)
            FROM trades WHERE entry_ts::date = CURRENT_DATE
        """)).fetchone()
        open_pos = s.execute(text(
            "SELECT COUNT(*) FROM engine_positions WHERE qty != 0")).scalar()
    lines.append(f"Trades: {n_trades} closed ({wins} wins) · P&L ₹{float(pnl):+,.0f}"
                 + (f" · ⚠ {open_pos} STILL OPEN" if open_pos else ""))
except Exception as exc:
    lines.append(f"Trades: query failed ({exc})")

# ── Gate decisions today ───────────────────────────────────────────────────────
try:
    with get_session() as s:
        n_gate, n_allow = s.execute(text("""
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE features_snapshot::jsonb->>'verdict' = 'ALLOW')
            FROM signals WHERE strategy='orb_gate' AND ts::date = CURRENT_DATE
        """)).fetchone()
    if n_gate:
        lines.append(f"Kronos gate (shadow): {n_gate} decisions, would allow {n_allow}")
except Exception:
    pass

# ── Calibration verdict ────────────────────────────────────────────────────────
try:
    from ml.calibration import build_report, fill_outcomes
    fill_outcomes(days=3)
    rep = build_report(days=30)
    lines.append(f"Calibration: {rep['recommendation']}")
except Exception as exc:
    lines.append(f"Calibration: failed ({exc})")

# ── Backfill progress ──────────────────────────────────────────────────────────
try:
    import json
    ckpt = Path("/opt/dhan-trading/backfill_ckpt_NSE_EQ.json")
    if ckpt.exists():
        d = json.loads(ckpt.read_text())
        lines.append(f"Backfill: {d['index']}/{d['total']} "
                     f"({d['index'] / d['total'] * 100:.0f}%)")
except Exception:
    pass

send("\n".join(lines))
print("\n".join(lines))
