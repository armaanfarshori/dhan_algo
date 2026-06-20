"""EQUITY (stock) F&O data collector — universe-wide forward collection + backfill.

Phase-0 of the stock-derivatives data foundation. Two drivers, both over the
``core.fno_equity_universe`` (the ~180-190 NSE single-stock F&O underlyings):

  • ``backfill_equity_futures``    — backfill stock-futures (NSE_FNO / FUTSTK) OHLCV+OI
        into ``futures_bars``, one continuous near-month series per underlying. Reuses
        ``fno_backfill.backfill_futures_bars(instrument="FUTSTK")`` — token via
        ``resolve_access_token``, off-hours-guarded, partial-failure-tolerant.

  • ``collect_equity_chains``      — daily forward snapshot of each stock's option chain
        into ``option_chain_snapshot`` (underlying_scrip = the stock's
        UNDERLYING_SECURITY_ID, underlying_seg = "NSE_FNO") + ATM IV into
        ``option_atm_iv``. Reuses ``fno_backfill.snapshot_option_chain``. Respects the
        Dhan optionchain ~1 req / 3 s throttle and tolerates per-stock failure (one stock
        failing must NOT abort the rest — mirrors ``fno_collector.run_eod_collection``).

Dhan has no historical option-chain / IV, so the chain collection is FORWARD-ONLY: it
accrues daily snapshots from the day it starts running. The futures bars DO have real
history via the charts endpoint.

No new schema: ``option_chain_snapshot`` already keys on (snapshot_time,
underlying_scrip, expiry_date, strike, option_type) and ``futures_bars`` on
(symbol, timeframe, time) — stock rows coexist with NIFTY rows.

Run live from the trusted machine, off-hours (the underlying fno_backfill functions
enforce the off-hours guard)::

    python -m core.fno_equity_collector --chains                 # all universe, today
    python -m core.fno_equity_collector --chains --top-n 50      # top-N subset
    python -m core.fno_equity_collector --futures --from 2024-06-01 --to 2026-06-18
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime
from typing import Any, Optional

import core.fno_backfill as fb
from core.fno_equity_universe import load_universe

logger = logging.getLogger("dhan.fno_equity_collector")

# Dhan rate-limits the optionchain endpoint to ~1 request / 3 s.
OPTIONCHAIN_THROTTLE_S = 3.0


def _select_universe(top_n: Optional[int]) -> list[dict[str, Any]]:
    """Load the generated universe; optionally restrict to the first ``top_n`` rows.

    NOTE: the universe JSON is symbol-sorted, NOT liquidity-ranked, so ``top_n`` is
    a deterministic subset for staged rollout — wire a real ADV/OI ranking here once
    we have the data to rank by. The arg is named ``top_n`` to match the spec
    (top-N-by-liquidity subset).
    """
    universe = load_universe()
    if top_n is not None and top_n > 0:
        return universe[:top_n]
    return universe


# ── stock-futures backfill driver ────────────────────────────────────────────────
async def backfill_equity_futures(
    client: Any,
    from_date: str,
    to_date: str,
    *,
    top_n: Optional[int] = None,
    timeframe: str = "1d",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Backfill stock-futures bars (FUTSTK) for the universe into futures_bars.

    Each underlying's near-month FUTSTK ``future_security_id`` is fetched and written
    under ``symbol = "<UNDERLYING>-FUT"`` (a continuous near-month series — see the
    PK-collision warning in fno_backfill.backfill_futures_bars). One underlying failing
    is logged into ``errors`` and the rest continue.

    Returns ``{"symbols": n_attempted, "ok": n_ok, "rows": total_rows, "errors": [...]}``.
    """
    universe = _select_universe(top_n)
    errors: list[str] = []
    total_rows = 0
    ok = 0
    logger.info("equity-futures backfill: %d underlyings (%s→%s)", len(universe), from_date, to_date)
    for u in universe:
        sym = u["underlying_symbol"]
        sec_id = u.get("future_security_id")
        if not sec_id:
            errors.append(f"{sym}: no future_security_id")
            continue
        try:
            n = await fb.backfill_futures_bars(
                client,
                f"{sym}-FUT",
                str(sec_id),
                from_date,
                to_date,
                timeframe=timeframe,
                instrument="FUTSTK",
                expiry_date=u.get("near_expiry"),
                now=now,
            )
            total_rows += n
            ok += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("equity-futures backfill failed for %s: %s — continuing", sym, exc)
            errors.append(f"{sym}: {exc}")

    summary = {"symbols": len(universe), "ok": ok, "rows": total_rows, "errors": errors}
    logger.info("equity-futures backfill complete: %s", {k: v for k, v in summary.items() if k != "errors"})
    return summary


# ── stock option-chain forward collector ─────────────────────────────────────────
async def collect_equity_chains(
    client: Any,
    *,
    top_n: Optional[int] = None,
    n_expiries: int = 1,
    throttle_s: float = OPTIONCHAIN_THROTTLE_S,
    sleep: Any = asyncio.sleep,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Snapshot each universe stock's option chain into option_chain_snapshot (+ ATM
    IV into option_atm_iv). Nearest ``n_expiries`` future expiries per stock.

    Per-stock isolation: a stock whose expiry-list or chain fetch raises is logged into
    ``errors`` and the loop continues (mirrors fno_collector.run_eod_collection). The
    Dhan optionchain throttle (~1 req / 3 s) is honoured with ``sleep(throttle_s)``
    BETWEEN chain calls (injectable for tests).

    Returns ``{"stocks": n, "ok": n_ok, "chain_rows": total, "atm": total_atm,
    "errors": [...]}``.
    """
    universe = _select_universe(top_n)
    today: date = (now or fb._now_ist()).astimezone(fb._IST).date()
    errors: list[str] = []
    total_chain_rows = 0
    total_atm = 0
    ok = 0
    first_call = True

    logger.info("equity option-chain collection: %d stocks (n_expiries=%d)", len(universe), n_expiries)
    for u in universe:
        sym = u["underlying_symbol"]
        scrip = u.get("underlying_security_id")
        if not scrip:
            errors.append(f"{sym}: no underlying_security_id")
            continue
        try:
            scrip_int = int(scrip)
            # Throttle BEFORE the expiry-list call so it counts as a request slot.
            # This ensures no two Dhan optionchain requests fire within throttle_s.
            if not first_call and throttle_s > 0:
                await sleep(throttle_s)
            first_call = False
            # Resolve the nearest future expiries for THIS stock (NSE_FNO segment).
            try:
                raw_expiries = await client.get_fno_expiry_list(scrip_int, "NSE_FNO")
            except Exception as exc:  # noqa: BLE001
                logger.warning("equity expiry-list failed for %s: %s — continuing", sym, exc)
                errors.append(f"{sym}: expiry-list: {exc}")
                continue
            data = (
                raw_expiries.get("data", raw_expiries)
                if isinstance(raw_expiries, dict)
                else raw_expiries
            )
            parsed = sorted({d for d in (fb._parse_date(x) for x in (data or [])) if d is not None})
            future = [d for d in parsed if d >= today][:n_expiries]
            if not future:
                logger.info("equity chain: no future expiries for %s — skipping", sym)
                continue
            stock_ok = True
            for expiry in future:
                # Honour the optionchain throttle BETWEEN chain calls.
                if throttle_s > 0:
                    await sleep(throttle_s)
                try:
                    result = await fb.snapshot_option_chain(
                        client,
                        sym,
                        underlying_scrip=scrip_int,
                        underlying_seg="NSE_FNO",
                        expiry_date=expiry,
                        now=now,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "equity chain-fetch failed for %s expiry=%s: %s — continuing",
                        sym, expiry, exc,
                    )
                    errors.append(f"{sym}: chain: {exc}")
                    stock_ok = False
                    continue
                total_chain_rows += result.get("chain_rows", 0)
                total_atm += result.get("atm", 0)
            if stock_ok:
                ok += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("equity chain failed for %s: %s — continuing", sym, exc)
            errors.append(f"{sym}: {exc}")

    summary = {
        "stocks": len(universe),
        "ok": ok,
        "chain_rows": total_chain_rows,
        "atm": total_atm,
        "errors": errors,
    }
    logger.info(
        "equity option-chain collection complete: %s",
        {k: v for k, v in summary.items() if k != "errors"},
    )
    return summary


# ── CLI ──────────────────────────────────────────────────────────────────────────
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="EQUITY (stock) F&O collector — universe-wide chains + futures backfill (off-hours)."
    )
    p.add_argument("--chains", action="store_true", help="forward-snapshot stock option chains")
    p.add_argument("--futures", action="store_true", help="backfill stock-futures bars")
    p.add_argument("--top-n", type=int, default=None, help="restrict to the first N universe symbols")
    p.add_argument("--n-expiries", type=int, default=1, help="future expiries per stock (chains)")
    p.add_argument("--from", dest="from_date", help="futures backfill start YYYY-MM-DD")
    p.add_argument("--to", dest="to_date", help="futures backfill end YYYY-MM-DD")
    return p


async def _amain(args: argparse.Namespace) -> None:
    from config import get_config
    from core.client import DhanClient
    from db import init_db

    cfg = get_config()
    init_db(cfg.db_url)
    token = await fb.resolve_access_token()
    async with DhanClient(cfg.dhan_client_id, token) as client:
        if args.futures:
            if not args.from_date or not args.to_date:
                raise SystemExit("--futures requires --from and --to")
            res = await backfill_equity_futures(
                client, args.from_date, args.to_date, top_n=args.top_n
            )
            logger.info("equity-futures result: %s", {k: v for k, v in res.items() if k != "errors"})
        if args.chains:
            res = await collect_equity_chains(
                client, top_n=args.top_n, n_expiries=args.n_expiries
            )
            logger.info("equity-chains result: %s", {k: v for k, v in res.items() if k != "errors"})


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = _build_arg_parser().parse_args()
    if not (args.chains or args.futures):
        _build_arg_parser().print_help()
        return
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
