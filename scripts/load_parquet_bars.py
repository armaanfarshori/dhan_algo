"""Bulk-load the salvaged M2.5 parquet archive into the local `bars` hypertable.

The AWS salvage (2026-08-14) recovered per-security 1-minute parquet exports
(`~/dhan_data/s3-archive/kronos/training-data/`, one file per security_id,
columns: time[UTC] / security_id / open / high / low / close / volume / vwap).
This loader replays a date-bounded slice of that archive into `bars`
(timeframe '1m') via COPY — zero Dhan API calls, so it can run any time.

Design:
  • One worker process per slice of the file list (multiprocessing, --workers).
  • Per-file transaction: a file is COPYed in one txn and its security_id is
    appended to the checkpoint file only after commit — crash-resume is safe
    (a partially-copied file rolls back; a checkpointed one is skipped).
  • COPY into a TEMP table then INSERT ... ON CONFLICT DO NOTHING, so a re-run
    over an id that already has rows (e.g. live BarBuilder overlap) cannot die
    on the primary key.

Usage:
  .venv/bin/python scripts/load_parquet_bars.py \
      --src ~/dhan_data/s3-archive/kronos/training-data \
      --from 2024-08-15 [--to 2026-06-04] [--workers 6]
"""
from __future__ import annotations

import argparse
import io
import logging
import multiprocessing as mp
import os
import sys
import time as _time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("load_parquet_bars")

CHECKPOINT = Path("run/load_parquet_bars.done")

COLS = "time, security_id, timeframe, open, high, low, close, volume, vwap"


def _dsn() -> str:
    from config import get_config
    cfg = get_config()
    return (f"host={cfg.db_host} port={cfg.db_port} dbname={cfg.db_name} "
            f"user={cfg.db_user} password={cfg.db_password}")


def _load_one(path: Path, lo: datetime, hi: datetime, conn) -> int:
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    t = pq.read_table(path)
    mask = pc.and_(pc.greater_equal(t.column("time"), lo),
                   pc.less(t.column("time"), hi))
    t = t.filter(mask)
    if t.num_rows == 0:
        return 0

    buf = io.StringIO()
    cols = [t.column(c).to_pylist()
            for c in ("time", "security_id", "open", "high", "low",
                      "close", "volume", "vwap")]
    for ts, sid, o, h, lw, c, v, vw in zip(*cols):
        vw_s = "" if vw is None else f"{vw:.4f}"
        buf.write(f"{ts.isoformat()},{sid},1m,{o:.4f},{h:.4f},{lw:.4f},"
                  f"{c:.4f},{v},{vw_s}\n")
    buf.seek(0)

    with conn.cursor() as cur:
        # TEMP staging + ON CONFLICT DO NOTHING: an id that already has rows
        # (live BarBuilder overlap, or a re-run) must never abort the load.
        cur.execute("CREATE TEMP TABLE _stage (LIKE bars INCLUDING DEFAULTS) "
                    "ON COMMIT DROP")
        cur.copy_expert(
            f"COPY _stage ({COLS}) FROM STDIN WITH (FORMAT csv, NULL '')", buf)
        cur.execute(f"INSERT INTO bars ({COLS}) SELECT {COLS} FROM _stage "
                    "ON CONFLICT (security_id, timeframe, time) DO NOTHING")
    conn.commit()
    return t.num_rows


def _worker(idx: int, files: list, lo: datetime, hi: datetime,
            done_path: str) -> None:
    import psycopg2

    conn = psycopg2.connect(_dsn())
    n_rows = n_files = 0
    t0 = _time.time()
    for f in files:
        path = Path(f)
        try:
            n_rows += _load_one(path, lo, hi, conn)
            n_files += 1
        except Exception:
            log.exception("[w%d] FAILED %s — skipping", idx, path.name)
            conn.rollback()
            continue
        # Append-only checkpoint; O_APPEND writes of one short line are atomic.
        with open(done_path, "a") as fh:
            fh.write(path.stem + "\n")
        if n_files % 50 == 0:
            rate = n_rows / max(_time.time() - t0, 1e-9)
            log.info("[w%d] %d/%d files, %.1fM rows, %.0f rows/s",
                     idx, n_files, len(files), n_rows / 1e6, rate)
    conn.close()
    log.info("[w%d] DONE: %d files, %.1fM rows in %.0fs",
             idx, n_files, n_rows / 1e6, _time.time() - t0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--from", dest="lo", required=True,
                    help="inclusive UTC date lower bound, YYYY-MM-DD")
    ap.add_argument("--to", dest="hi", default="2100-01-01",
                    help="exclusive UTC date upper bound, YYYY-MM-DD")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    lo = datetime.fromisoformat(args.lo).replace(tzinfo=timezone.utc)
    hi = datetime.fromisoformat(args.hi).replace(tzinfo=timezone.utc)

    files = sorted(Path(args.src).expanduser().glob("*.parquet"))
    done: set[str] = set()
    if CHECKPOINT.exists():
        done = set(CHECKPOINT.read_text().split())
    todo = [f for f in files if f.stem not in done]
    log.info("%d parquet files, %d already loaded, %d to go",
             len(files), len(files) - len(todo), len(todo))
    if not todo:
        return 0
    CHECKPOINT.parent.mkdir(exist_ok=True)

    slices = [todo[i::args.workers] for i in range(args.workers)]
    procs = [mp.Process(target=_worker,
                        args=(i, [str(f) for f in sl], lo, hi, str(CHECKPOINT)))
             for i, sl in enumerate(slices) if sl]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    failed = [p for p in procs if p.exitcode != 0]
    if failed:
        log.error("%d workers exited non-zero", len(failed))
        return 1
    log.info("load complete")
    return 0


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent.parent)
    raise SystemExit(main())
