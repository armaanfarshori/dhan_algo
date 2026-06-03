#!/usr/bin/env python3
"""
Kronos Forecast — called by Hermes kronos_forecast skill.

Dynamically selects top-N most volatile NSE equities from the bars table,
runs KronosSignalEngine on each, and writes scored signals to the DB.

Usage:
    python3 forecast.py               # top 20 by ATR%, write to DB
    python3 forecast.py --n 30        # top 30
    python3 forecast.py --dry-run     # print scores, skip DB write
    python3 forecast.py --live        # rank by today's price move (market hours)
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/opt/dhan-trading")
from dotenv import load_dotenv
load_dotenv("/opt/dhan-trading/.env")

from db import init_db, get_session
from config import get_config
from core.nse_screener import screen
from core.kronos_signal import KronosSignalEngine
from sqlalchemy import text

_SEGMENT = "NSE_EQ"


def _init_db():
    cfg = get_config()
    from urllib.parse import quote_plus
    url = (
        f"postgresql+psycopg2://{quote_plus(cfg.db_user)}:{quote_plus(cfg.db_password)}"
        f"@{cfg.db_host}:{cfg.db_port}/{cfg.db_name}"
    )
    init_db(url)


def _write_signal(session, sid: str, result: dict):
    import json
    session.execute(text("""
        INSERT INTO signals
            (ts, security_id, side, score, confidence, strategy, features_snapshot)
        VALUES
            (:ts, :sid, :side, :score, :conf, 'kronos', :features)
        ON CONFLICT DO NOTHING
    """), {
        "ts":       datetime.now(timezone.utc),
        "sid":      sid,
        "side":     result["side"],
        "score":    result["score"],
        "conf":     result["confidence"],
        "features": json.dumps({
            "forecast_return": result.get("forecasted_return"),
            "current_price":   result.get("current_price"),
            "horizon_price":   result.get("horizon_price"),
        }),
    })


async def run(args):
    _init_db()

    # Step 1: pick top-N by volatility
    print(f"\n{'─'*60}")
    print(f"  Kronos Forecast  |  {datetime.now().strftime('%Y-%m-%d %H:%M IST')}")
    print(f"{'─'*60}")
    print(f"Selecting top {args.n} volatile NSE equities...")

    client = None
    if args.live:
        from core.client import DhanClient
        cfg = get_config()
        client = DhanClient(cfg.dhan_client_id, cfg.dhan_access_token)
        await client.__aenter__()

    try:
        candidates = await screen(n=args.n, segment=_SEGMENT, live=args.live, client=client)
    finally:
        if client:
            await client.__aexit__(None, None, None)

    if not candidates:
        print("No candidates found — check that bars table has data.")
        return

    print(f"\nSelected {len(candidates)} securities:")
    for i, c in enumerate(candidates, 1):
        print(f"  {i:2d}. {c['security_id']:>8}  {c['reason']}")

    # Step 2: load Kronos model
    print(f"\nLoading Kronos model...")
    engine = KronosSignalEngine()
    await engine.load()

    # Step 3: score each security
    print(f"\nScoring with Kronos ({len(candidates)} securities):\n")

    results = []
    with get_session() as session:
        for c in candidates:
            sid = c["security_id"]
            try:
                result = await engine.score_from_db(
                    security_id=sid,
                    exchange_segment=_SEGMENT,
                )
                side   = result["side"]
                score  = result["score"]
                conf   = result["confidence"]
                ret    = result.get("forecasted_return", 0)

                icon = {"BUY": "📈", "SELL": "📉", "HOLD": "➖"}.get(side, "❓")
                print(f"  {icon} {sid:>8}  {side:<4}  score={score:.4f}  conf={conf:.2f}  Δ{ret*100:+.3f}%  [{c['reason']}]")

                results.append((sid, result))

                if not args.dry_run:
                    _write_signal(session, sid, result)

            except Exception as exc:
                print(f"  ❌ {sid:>8}  ERROR: {exc}")

    # Step 4: summary
    buys  = [r for _, r in results if r["side"] == "BUY"]
    sells = [r for _, r in results if r["side"] == "SELL"]
    holds = [r for _, r in results if r["side"] == "HOLD"]

    print(f"\n{'─'*60}")
    print(f"  Results: {len(buys)} BUY  |  {len(sells)} SELL  |  {len(holds)} HOLD")
    if not args.dry_run:
        print(f"  {len(results)} signals written to signals table.")
    else:
        print("  Dry-run — no DB writes.")

    # Top BUY/SELL candidates for the briefing
    if buys:
        top_buy = sorted(buys, key=lambda r: r["confidence"], reverse=True)[0]
        print(f"\n  Strongest BUY : security {[sid for sid, r in results if r is top_buy][0]}  "
              f"conf={top_buy['confidence']:.2f}  Δ{top_buy.get('forecasted_return',0)*100:+.3f}%")
    if sells:
        top_sell = sorted(sells, key=lambda r: r["confidence"], reverse=True)[0]
        print(f"  Strongest SELL: security {[sid for sid, r in results if r is top_sell][0]}  "
              f"conf={top_sell['confidence']:.2f}  Δ{top_sell.get('forecasted_return',0)*100:+.3f}%")
    print(f"{'─'*60}\n")


def main():
    parser = argparse.ArgumentParser(description="Kronos forecast on top volatile NSE equities")
    parser.add_argument("--n",       type=int, default=20, help="Number of securities to score (default: 20)")
    parser.add_argument("--dry-run", action="store_true",  help="Score but don't write to DB")
    parser.add_argument("--live",    action="store_true",  help="Rank by today's live move (market hours)")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
