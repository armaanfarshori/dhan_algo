#!/usr/bin/env python3
"""
prepare_kronos_dataset.py — M2.5: Clean Parquet → Kronos training dataset

Reads per-instrument Parquet files (from S3 or local dir), applies the
strict date-based train/val/test split, adds the `amount` column Kronos
needs, and saves per-instrument numpy files for on-the-fly windowing
during fine-tuning.

Key data format (from kronos/kronos.py KronosPredictor):
  - 6 features per bar: open, high, low, close, volume, amount
    where amount = volume × mean(open, high, low, close)
  - 5 temporal features: minute, hour, weekday, day, month
  - Normalization: per-window z-score (done in finetune.py, NOT here)
  - No tokenizer fitting needed — KronosTokenizer is a pre-trained
    neural VQ network, not a statistical quantizer

Run on the GPU spot instance after syncing Parquet from S3:
  aws s3 sync s3://$S3_BUCKET/kronos/training-data/ ~/nse_data/
  python prepare_kronos_dataset.py --data ~/nse_data/ --out ~/nse_dataset/

Or pull directly from S3:
  python prepare_kronos_dataset.py --s3 --out ~/nse_dataset/

Date splits (NEVER randomise — time-series data):
  Train:    2021-06-01 → 2024-12-31
  Validate: 2025-01-01 → 2025-12-31
  Test:     2026-01-01 → today  ← touch ONCE for final eval
"""

import argparse
import json
import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pandas as pd
# boto3 + tqdm are imported lazily inside the functions that use them — this
# module is imported by tests (for TF_PRESETS / resample_5min / process_instrument)
# in a CI env that installs only the core deps (no boto3/tqdm).

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("prepare")

# ── Config ────────────────────────────────────────────────────────────────────

S3_BUCKET = os.environ.get("S3_BUCKET", "")  # set in .env on the agent
S3_PREFIX  = "kronos/training-data"

# Strict date-based splits — never change these
TRAIN_START = "2021-06-01"
TRAIN_END   = "2024-12-31"
VAL_START   = "2025-01-01"
VAL_END     = "2025-12-31"
TEST_START  = "2026-01-01"   # touch ONCE for final eval

# Length presets per scoring timeframe. These MUST match what the model is
# served with: config.py kronos_lookback (context) / kronos_pred_len (pred) for
# 5min, and the legacy 1min values. finetune.py must be invoked with the same
# --context_length/--prediction_length for the matching timeframe.
TF_PRESETS = {
    "1min": {"context": 512, "pred": 30},   # legacy — OOD for NSE (corpus is 5min+)
    "5min": {"context": 480, "pred": 6},     # matches live config (lookback 480, pred 6)
}

# Features expected by KronosPredictor (kronos/kronos.py)
PRICE_COLS = ["open", "high", "low", "close"]
FEAT_COLS  = ["open", "high", "low", "close", "volume", "amount"]
TIME_COLS  = ["minute", "hour", "weekday", "day", "month"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def add_amount(df: pd.DataFrame) -> pd.DataFrame:
    """Add `amount` column = volume × mean(open,high,low,close) (turnover)."""
    df = df.copy()
    df["amount"] = df["volume"] * df[PRICE_COLS].mean(axis=1)
    return df


def resample_5min(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 1-min bars to 5-min — matches core.kronos_signal._aggregate_bars:
    open=first, high=max, low=min, close=last, volume=sum. Empty bins (overnight
    gaps) are dropped, so no synthetic bar spans two sessions."""
    d = df.copy()
    d["time"] = pd.to_datetime(d["time"], utc=True)
    d = d.set_index("time").sort_index()
    agg = d.resample("5min").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna(subset=["open", "high", "low", "close"])
    return agg.reset_index()


def add_time_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add temporal features used by Kronos TemporalEmbedding."""
    ts = pd.to_datetime(df["time"], utc=True).dt.tz_convert("Asia/Kolkata")
    df = df.copy()
    df["minute"]  = ts.dt.minute
    df["hour"]    = ts.dt.hour
    df["weekday"] = ts.dt.weekday
    df["day"]     = ts.dt.day
    df["month"]   = ts.dt.month
    return df


def split_by_date(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """
    Apply strict date-based train/val/test split.
    Returns dict with keys 'train', 'val', 'test'.
    """
    df = df.copy()
    df["_date"] = pd.to_datetime(df["time"], utc=True).dt.date.astype(str)

    splits = {
        "train": df[(df["_date"] >= TRAIN_START) & (df["_date"] <= TRAIN_END)],
        "val":   df[(df["_date"] >= VAL_START)   & (df["_date"] <= VAL_END)],
        "test":  df[(df["_date"] >= TEST_START)],
    }
    return {k: v.drop(columns=["_date"]).reset_index(drop=True)
            for k, v in splits.items()}


def iter_parquet_local(data_dir: Path) -> Iterator[tuple[str, pd.DataFrame]]:
    """Yield (security_id, df) for each local .parquet file, one at a time.

    Each DataFrame is read lazily on demand and released by the caller before
    the next is read — only one is held in memory at any moment.
    """
    files = sorted(data_dir.glob("*.parquet"))
    log.info("Found %d Parquet files in %s", len(files), data_dir)
    for f in files:
        yield f.stem, pd.read_parquet(f)


def iter_parquet_s3() -> Iterator[tuple[str, pd.DataFrame]]:
    """Yield (security_id, df) for each S3 .parquet object, one at a time.

    Keys are listed up front (cheap), then each object is fetched, parsed, and
    yielded individually — only one DataFrame is held in memory at any moment.
    """
    import io

    import boto3
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_PREFIX + "/"):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".parquet"):
                keys.append(obj["Key"])

    log.info("Found %d Parquet files on S3", len(keys))
    for key in keys:
        resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
        buf = resp["Body"].read()
        yield Path(key).stem, pd.read_parquet(io.BytesIO(buf))


# ── Core processing ───────────────────────────────────────────────────────────

def process_instrument(
    security_id: str,
    df: pd.DataFrame,
    out_dir: Path,
    timeframe: str = "5min",
    context_len: int = 480,
    pred_len: int = 6,
) -> dict | None:
    """
    Process one instrument:
      - add amount + time features
      - split by date
      - save per-split numpy arrays
      - return stats dict for manifest

    Saved files per instrument per split:
      {out_dir}/{split}/{security_id}_x.npy       float32 (T, 6)  — OHLCV + amount
      {out_dir}/{split}/{security_id}_stamp.npy   float32 (T, 5)  — time features

    Windows are NOT pre-computed here. finetune.py generates them
    on-the-fly using a sliding window Dataset, saving memory.
    """
    if "time" not in df.columns:
        log.warning("SKIP %s — no 'time' column", security_id)
        return None

    if timeframe == "5min":
        df = resample_5min(df)

    df = add_amount(df)
    df = add_time_features(df)

    # Ensure numeric types
    for col in FEAT_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=FEAT_COLS)

    min_bars = context_len + pred_len
    splits = split_by_date(df)
    stats  = {"security_id": security_id, "splits": {}}

    for split_name, split_df in splits.items():
        if len(split_df) < min_bars:
            stats["splits"][split_name] = {"bars": len(split_df), "windows": 0}
            continue

        x     = split_df[FEAT_COLS].values.astype(np.float32)   # (T, 6)
        stamp = split_df[TIME_COLS].values.astype(np.float32)    # (T, 5)

        split_out = out_dir / split_name
        split_out.mkdir(parents=True, exist_ok=True)

        np.save(split_out / f"{security_id}_x.npy",     x)
        np.save(split_out / f"{security_id}_stamp.npy", stamp)

        n_windows = max(0, (len(split_df) - context_len - pred_len) // pred_len + 1)
        stats["splits"][split_name] = {"bars": len(split_df), "windows": n_windows}

    return stats


# ── Dataset info ──────────────────────────────────────────────────────────────

def write_manifest(out_dir: Path, all_stats: list[dict],
                   timeframe: str, context_len: int, pred_len: int):
    total_train_windows = sum(
        s["splits"].get("train", {}).get("windows", 0) for s in all_stats
    )
    total_val_windows = sum(
        s["splits"].get("val", {}).get("windows", 0) for s in all_stats
    )
    manifest = {
        "prepared_at":        pd.Timestamp.now(tz="UTC").isoformat(),
        "timeframe":          timeframe,
        "context_length":     context_len,
        "pred_length":        pred_len,
        "features":           FEAT_COLS,
        "time_features":      TIME_COLS,
        "splits": {
            "train": {"date_range": f"{TRAIN_START} → {TRAIN_END}", "total_windows": total_train_windows},
            "val":   {"date_range": f"{VAL_START} → {VAL_END}",     "total_windows": total_val_windows},
            "test":  {"date_range": f"{TEST_START} → today",         "total_windows": "not pre-counted"},
        },
        "instruments": all_stats,
    }
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    log.info("Manifest → %s", path)
    log.info("Train windows: %s  |  Val windows: %s",
             f"{total_train_windows:,}", f"{total_val_windows:,}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Prepare Kronos fine-tuning dataset from clean Parquet")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--data", type=Path, metavar="DIR",
                     help="Local directory containing per-instrument .parquet files")
    src.add_argument("--s3",  action="store_true",
                     help="Download directly from S3 (requires AWS credentials)")
    ap.add_argument("--out",  type=Path, required=True, metavar="DIR",
                    help="Output directory for processed numpy files")
    ap.add_argument("--timeframe", choices=list(TF_PRESETS), default="5min",
                    help="Bar granularity to train on. 5min (default) aggregates "
                         "the 1m Parquet to match live serving; 1min = legacy/OOD. "
                         "Build both for the A/B shadow (separate --out dirs).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process only N instruments (for quick testing)")
    args = ap.parse_args()

    preset      = TF_PRESETS[args.timeframe]
    context_len = preset["context"]
    pred_len    = preset["pred"]
    log.info("Timeframe=%s → context_len=%d pred_len=%d", args.timeframe, context_len, pred_len)

    args.out.mkdir(parents=True, exist_ok=True)

    # Stream instruments one at a time — each DataFrame is read, processed, and
    # released before the next, so memory stays bounded regardless of universe
    # size (1,674 files / ~630M rows would OOM a load-everything approach).
    if args.s3:
        instruments = iter_parquet_s3()
    else:
        instruments = iter_parquet_local(args.data)

    if args.limit:
        # islice caps the stream BEFORE the (N+1)th file is read/fetched, so
        # --limit still means "stop after N instruments" with no wasted I/O.
        import itertools
        instruments = itertools.islice(instruments, args.limit)
        log.info("Limiting to %d instruments (--limit)", args.limit)

    log.info("Processing instruments → %s", args.out)

    all_stats = []
    skipped   = 0

    from tqdm import tqdm
    for security_id, df in tqdm(instruments, desc="Processing"):
        stats = process_instrument(security_id, df, args.out,
                                   timeframe=args.timeframe,
                                   context_len=context_len, pred_len=pred_len)
        # Release this instrument's DataFrame before reading the next one.
        del df
        if stats is None:
            skipped += 1
            continue
        all_stats.append(stats)

    write_manifest(args.out, all_stats, args.timeframe, context_len, pred_len)

    log.info("Done. %d instruments processed, %d skipped.", len(all_stats), skipped)
    log.info("")
    log.info("Next step — fine-tune on GPU instance:")
    log.info("  python finetune.py \\")
    log.info("    --model NeoQuasar/Kronos-base \\")
    log.info("    --data_path %s/train/ \\", args.out)
    log.info("    --val_path  %s/val/ \\", args.out)
    log.info("    --context_length %d \\", context_len)
    log.info("    --prediction_length %d \\", pred_len)
    log.info("    --upload-s3 --s3-name nse-%s-v1 \\", args.timeframe)
    log.info("    --output_dir ~/kronos-nse-%s-v1/", args.timeframe)


if __name__ == "__main__":
    main()
