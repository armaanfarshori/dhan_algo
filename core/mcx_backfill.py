"""MCX (commodities) Phase-0 data backfill — FUTCOM near-month futures bars.

Populates the ``mcx_bars`` table added in Alembic 012, mirroring the F&O
Phase-0 pattern (core/fno_backfill.py):

  • ``backfill_mcx_bars`` — MCX commodity-futures OHLCV + open interest from
        Dhan's historical charts endpoint (``charts/historical`` daily /
        ``charts/intraday`` intraday) using ``exchange_segment="MCX_COMM"`` and
        ``instrument="FUTCOM"``. Writes the NEAR-MONTH contract per symbol.

Hard invariants (CLAUDE.md + F&O handoff):
  • Historical reads only — this module never touches an order path.
  • All live Dhan fetches refuse to run during market hours via
    ``_assert_off_hours``. (MCX trades a much longer day than NSE — typically
    09:00–23:30 IST — but to keep this strictly a research/backfill path that
    can never interleave with live order flow, it reuses the conservative
    NSE 09:15–15:30 off-hours guard from core/fno_backfill. Run MCX backfills
    overnight, after the MCX session closes.)
  • Pure parse/extract helpers are deterministic and DB-free so they unit-test
    without creds or a database.
  • Token is sourced via core.fno_backfill.resolve_access_token (live cache →
    PIN/TOTP fallback), NEVER the static cfg.dhan_access_token (which expires
    and triggered DH-901 on long-running jobs).

CONTINUOUS / FRONT-MONTH HANDLING — OPEN QUESTION (do NOT solve here)
--------------------------------------------------------------------
This backfill writes the NEAR-MONTH contract for a symbol under its physical
``security_id`` (one explicit contract per run). Building a *continuous*
back-adjusted series across monthly rolls (roll dates, gap adjustment, volume/OI
roll triggers) is deliberately NOT solved — it is the MCX analogue of F&O
Open Q#2 (continuous index futures, docs/fno-handoff.md). The schema is
roll-agnostic: store raw per-contract under distinct ``security_id`` values, and
build a stitched series later as a derived layer. See TODO(mcx-continuous).

Run live from the trusted machine, off-hours, e.g.::

    python -m core.mcx_backfill --symbol CRUDEOILM --security-id <id> \
        --from 2025-01-01 --to 2026-06-18
    python -m core.mcx_backfill --symbol GOLDM --security-id <id> \
        --from 2025-01-01 --to 2026-06-18 --timeframe 5min --interval 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Reuse the F&O time/market-hours guard, token path, and history parser verbatim
# so MCX inherits the same hardened, unit-tested behaviour (no duplication drift).
from core.fno_backfill import (
    _assert_off_hours,
    _now_ist,
    is_market_hours,
    parse_futures_history,
    resolve_access_token,
)

logger = logging.getLogger("dhan.mcx_backfill")

# Re-export for callers/tests that reach for them on this module.
__all__ = [
    "backfill_mcx_bars",
    "parse_mcx_history",
    "resolve_access_token",
    "is_market_hours",
    "_assert_off_hours",
    "MCX_EXCHANGE_SEGMENT",
    "MCX_INSTRUMENT",
    "CHECKPOINT_DIR",
]

# Dhan segment / instrument codes for MCX commodity futures.
MCX_EXCHANGE_SEGMENT = "MCX_COMM"
MCX_INSTRUMENT = "FUTCOM"

# Checkpoint location — mirrors backfill.py's checkpointed-CLI convention so a
# multi-day/interrupted run can be re-driven idempotently (upserts are ON CONFLICT).
CHECKPOINT_DIR = Path(__file__).parent.parent / ".cache" / "mcx_backfill"


# ── pure helpers (deterministic, DB-free) ───────────────────────────────────────
def parse_mcx_history(
    raw: dict[str, Any],
    symbol: str,
    security_id: str,
    timeframe: str = "1d",
    expiry_date: Optional[date] = None,
) -> list[dict[str, Any]]:
    """Parse a Dhan ``charts/historical|intraday`` response into mcx_bars rows.

    MCX FUTCOM history has the same parallel-array shape as NSE FUTIDX, so we
    delegate the OHLCV+OI extraction to the F&O ``parse_futures_history`` and
    then attach ``security_id`` (mcx_bars is keyed on the physical contract id,
    like index_bars). Suspended/illiquid / malformed payload → []."""
    base = parse_futures_history(raw, symbol, timeframe, expiry_date)
    for r in base:
        r["security_id"] = security_id
    return base


# ── checkpointing ───────────────────────────────────────────────────────────────
def _checkpoint_path(security_id: str, timeframe: str) -> Path:
    return CHECKPOINT_DIR / f"{security_id}_{timeframe}.json"


def read_checkpoint(security_id: str, timeframe: str) -> Optional[dict[str, Any]]:
    """Return the last-completed checkpoint dict for (security_id, timeframe),
    or None if no checkpoint exists / is unreadable. DB-free, side-effect-free."""
    p = _checkpoint_path(security_id, timeframe)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        logger.warning("mcx_backfill: unreadable checkpoint %s — ignoring", p)
        return None


def write_checkpoint(security_id: str, timeframe: str, payload: dict[str, Any]) -> None:
    """Persist a checkpoint for (security_id, timeframe). Best-effort; failures
    are logged, never raised (a backfill must not die on a checkpoint write)."""
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    p = _checkpoint_path(security_id, timeframe)
    try:
        p.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("mcx_backfill: failed to write checkpoint %s: %s", p, exc)


# ── DB upsert (mirror core/fno_backfill._upsert_index_bars) ──────────────────────
def _upsert_mcx_bars(rows: list[dict[str, Any]]) -> int:
    """Upsert rows into mcx_bars. Conflict key: (security_id, timeframe, time)."""
    if not rows:
        return 0
    from psycopg2.extras import execute_values

    from db import get_session

    sql = (
        "INSERT INTO mcx_bars "
        "(time, security_id, symbol, expiry_date, timeframe, open, high, low, close, "
        "volume, open_interest) VALUES %s "
        "ON CONFLICT (security_id, timeframe, time) DO UPDATE SET "
        "symbol=EXCLUDED.symbol, expiry_date=EXCLUDED.expiry_date, "
        "open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close, "
        "volume=EXCLUDED.volume, open_interest=EXCLUDED.open_interest"
    )
    tuples = [
        (
            r["time"], r["security_id"], r["symbol"], r.get("expiry_date"),
            r["timeframe"], r["open"], r["high"], r["low"], r["close"],
            r["volume"], r["open_interest"],
        )
        for r in rows
    ]
    with get_session() as session:
        cur = session.connection().connection.cursor()
        execute_values(cur, sql, tuples, page_size=1000)
        cur.close()
    return len(tuples)


# ── orchestration (live Dhan fetches — off-hours only) ───────────────────────────
async def backfill_mcx_bars(
    client: Any,
    symbol: str,
    security_id: str,
    from_date: str,
    to_date: str,
    *,
    timeframe: str = "1d",
    interval: Optional[str] = None,
    expiry_date: Optional[date] = None,
    now: Optional[datetime] = None,
) -> int:
    """Fetch MCX_COMM/FUTCOM OHLCV+OI for ONE near-month contract and upsert into
    mcx_bars. Off-hours only.

    ``timeframe`` is the logical label stored on the row ("1d", "5min", …);
    ``interval`` (e.g. "5") is the Dhan intraday minute interval — when given,
    the intraday endpoint is used, otherwise the daily endpoint.

    WARNING: each ``security_id`` is ONE physical contract. Do NOT write two
    different expiries under the same id; continuous/back-adjusted stitching is
    an unsolved derived-layer question (TODO(mcx-continuous); F&O Open Q#2
    analogue). The schema is unchanged.
    """
    _assert_off_hours("backfill_mcx_bars", now)
    if interval is not None:
        raw = await client.get_intraday_historical(
            security_id=security_id,
            exchange_segment=MCX_EXCHANGE_SEGMENT,
            instrument=MCX_INSTRUMENT,
            interval=str(interval),
            from_date=from_date,
            to_date=to_date,
        )
    else:
        raw = await client.get_daily_historical(
            security_id=security_id,
            exchange_segment=MCX_EXCHANGE_SEGMENT,
            instrument=MCX_INSTRUMENT,
            from_date=from_date,
            to_date=to_date,
        )
    rows = parse_mcx_history(raw, symbol, security_id, timeframe, expiry_date)
    n = _upsert_mcx_bars(rows)
    logger.info(
        "mcx_bars: upserted %d rows for %s/%s (%s→%s, tf=%s)",
        n, symbol, security_id, from_date, to_date, timeframe,
    )
    # Checkpoint the completed window so an interrupted multi-symbol run resumes.
    write_checkpoint(
        security_id, timeframe,
        {
            "symbol": symbol,
            "security_id": security_id,
            "from_date": from_date,
            "to_date": to_date,
            "timeframe": timeframe,
            "rows": n,
            "completed_at": (now or _now_ist()).astimezone(timezone.utc).isoformat(),
        },
    )
    return n


# ── CLI ──────────────────────────────────────────────────────────────────────────
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="MCX (commodities) Phase-0 data backfill — FUTCOM near-month (off-hours only)"
    )
    p.add_argument("--symbol", required=True, help="commodity series, e.g. CRUDEOILM / GOLDM / NATURALGAS")
    p.add_argument("--security-id", required=True, help="MCX_COMM near-month contract security id")
    p.add_argument("--from", dest="from_date", required=True, help="from date YYYY-MM-DD")
    p.add_argument("--to", dest="to_date", required=True, help="to date YYYY-MM-DD")
    p.add_argument("--timeframe", default="1d", help="row label (1d, 5min, 1min); default 1d")
    p.add_argument(
        "--interval", default=None,
        help="Dhan intraday minute interval (e.g. 5); omit for the daily endpoint",
    )
    p.add_argument("--expiry", default=None, help="physical contract expiry YYYY-MM-DD (optional)")
    return p


def _parse_cli_expiry(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise SystemExit(f"--expiry must be YYYY-MM-DD, got {s!r}")


async def _amain(args: argparse.Namespace) -> None:
    from config import get_config
    from core.client import DhanClient
    from db import init_db

    cfg = get_config()
    init_db(cfg.db_url)
    # Token via the manager (live cache → PIN/TOTP fallback), NOT the static
    # .env access token, which expires and triggers DH-901 on long-running jobs.
    access_token = await resolve_access_token()
    async with DhanClient(cfg.dhan_client_id, access_token) as client:
        await backfill_mcx_bars(
            client,
            args.symbol,
            args.security_id,
            args.from_date,
            args.to_date,
            timeframe=args.timeframe,
            interval=args.interval,
            expiry_date=_parse_cli_expiry(args.expiry),
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = _build_arg_parser().parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
