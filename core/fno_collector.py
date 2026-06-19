"""F&O Phase-0 EOD collector — daily post-close cron entrypoint.

Keeps the F&O dataset current by orchestrating four operations in sequence:
  1. Append today's NIFTY 50 and India VIX index bars (idempotent upsert).
  2. Refresh the expiry calendar.
  3. Snapshot the live option chain for the nearest N expiries — the only way
     to accrue real per-expiry ATM IV (Dhan has no historical option IV).
  4. Recompute realized vol over the freshly upserted index bars (sync, after
     the async collection is complete).

Run post-close (cron, trusted machine only). The sub-functions in
core/fno_backfill already enforce the off-hours guard — this module does NOT
add a second guard so tests can inject a fake ``now``.

Usage::

    python -m core.fno_collector                            # defaults
    python -m core.fno_collector --symbol NIFTY \\
        --n-expiries 4 --lookback-days 10 --with-instruments
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, timedelta
from typing import Any, Optional

import core.fno_backfill as fb
from core.fno_derived import compute_index_realized_vol

logger = logging.getLogger("dhan.fno_collector")


async def run_eod_collection(
    client: Any,
    symbol: str = "NIFTY",
    *,
    nifty_id: str = "13",
    vix_id: str = "21",
    lookback_days: int = 10,
    n_expiries: int = 4,
    now: Optional[Any] = None,
) -> dict[str, Any]:
    """Orchestrate the EOD F&O data collection for one session.

    Parameters
    ----------
    client:
        An open ``DhanClient`` async-context-manager instance.
    symbol:
        Underlying name (default ``"NIFTY"``).
    nifty_id:
        IDX_I security id for the underlying index (default ``"13"`` for NIFTY 50).
    vix_id:
        IDX_I security id for India VIX (default ``"21"``).
    lookback_days:
        Number of calendar days of index bar history to request (idempotent
        upsert — only new rows are inserted).
    n_expiries:
        Number of *future* expiries to snapshot.  Nearest first.
    now:
        Override the current IST time (for tests).  ``None`` uses real wall time.
        Instrument sync is handled synchronously in ``main()`` before this
        async function is called.

    Returns
    -------
    dict with keys: ``index_bars``, ``vix_bars``, ``expiries``,
    ``snapshots``, ``chain_rows``, ``atm``.
    """
    today: date = (now or fb._now_ist()).astimezone(fb._IST).date()
    frm: str = (today - timedelta(days=lookback_days)).isoformat()
    to: str = today.isoformat()

    # 1. Index bars — NIFTY 50 spot + India VIX.
    logger.info("Collecting index bars: %s [%s] %s → %s", symbol, nifty_id, frm, to)
    idx = await fb.backfill_index_bars(client, nifty_id, symbol, frm, to, now=now)

    logger.info("Collecting VIX bars: INDIAVIX [%s] %s → %s", vix_id, frm, to)
    vix = await fb.backfill_index_bars(client, vix_id, "INDIAVIX", frm, to, now=now)

    # 2. Expiry calendar.
    logger.info("Refreshing expiry calendar for %s", symbol)
    exp = await fb.build_expiry_calendar(client, symbol, now=now)

    # 3. Option-chain snapshots — nearest n_expiries future expiries only.
    scrip = fb.SYMBOL_SCRIP[symbol]
    raw_expiries = await client.get_fno_expiry_list(scrip, "IDX_I")
    raw_data = (
        raw_expiries.get("data", raw_expiries)
        if isinstance(raw_expiries, dict)
        else raw_expiries
    )
    parsed_expiries = sorted(
        {d for d in (fb._parse_date(x) for x in (raw_data or [])) if d is not None}
    )
    future_expiries = [d for d in parsed_expiries if d >= today]
    target_expiries = future_expiries[:n_expiries]

    logger.info(
        "Snapshotting option chain for %d expiries: %s",
        len(target_expiries),
        [str(e) for e in target_expiries],
    )
    total_chain_rows = 0
    total_atm = 0
    snapshots = 0
    for expiry in target_expiries:
        try:
            result = await fb.snapshot_option_chain(
                client, symbol, expiry_date=expiry, now=now
            )
            total_chain_rows += result.get("chain_rows", 0)
            total_atm += result.get("atm", 0)
            snapshots += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "snapshot_option_chain failed for %s %s: %s — continuing",
                symbol,
                expiry,
                exc,
            )

    summary: dict[str, Any] = {
        "index_bars": idx,
        "vix_bars": vix,
        "expiries": exp,
        "snapshots": snapshots,
        "chain_rows": total_chain_rows,
        "atm": total_atm,
    }
    logger.info("EOD collection complete: %s", summary)
    return summary


def main() -> None:
    """CLI entrypoint — initialises DB, builds a DhanClient from the cached
    token, runs the async collection, then recomputes realized vol."""
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="F&O EOD collector — run post-close to keep the F&O dataset current."
    )
    parser.add_argument("--symbol", default="NIFTY", help="Underlying symbol (default NIFTY)")
    parser.add_argument(
        "--n-expiries", type=int, default=4,
        help="Number of future expiries to snapshot (default 4)",
    )
    parser.add_argument(
        "--lookback-days", type=int, default=10,
        help="Days of index bar history to request (default 10)",
    )
    parser.add_argument(
        "--with-instruments", action="store_true",
        help="Sync the FNO instruments master before collecting",
    )
    args = parser.parse_args()

    from config import get_config
    from core.client import DhanClient
    from core.token_manager import read_current_token
    from db import init_db

    cfg = get_config()
    init_db(cfg.db_url)

    if args.with_instruments:
        import core.fno_instruments as fno_instruments
        logger.info("Syncing FNO instruments master…")
        result = fno_instruments.sync_fno_instruments()
        logger.info("FNO instruments sync: %s", result)

    token = read_current_token()
    if token is None:
        raise SystemExit(
            "No valid cached token in dhan_token.json — start dhan-trader first "
            "or generate a token manually."
        )

    async def _run() -> dict[str, Any]:
        async with DhanClient(cfg.dhan_client_id, token) as client:
            return await run_eod_collection(
                client,
                args.symbol,
                n_expiries=args.n_expiries,
                lookback_days=args.lookback_days,
            )

    summary = asyncio.run(_run())
    logger.info("Collection summary: %s", summary)

    # Recompute realized vol after the bars are written (sync, no client needed).
    nifty_id = "13"
    logger.info("Recomputing realized vol for security_id=%s", nifty_id)
    rv_rows = compute_index_realized_vol(nifty_id)
    logger.info("realized_vol_20d: updated %d rows for security_id=%s", rv_rows, nifty_id)


if __name__ == "__main__":
    main()
