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
  aws s3 sync s3://dhan-trading-data-155304839154/kronos/training-data/ ~/nse_data/
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
from pathlib import Path
from datetime import date

import boto3
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("prepare")

# ── Config ────────────────────────────────────────────────────────────────────

S3_BUCKET = "dhan-trading-data-155304839154"
S3_PREFIX  = "kronos/training-data"

# Strict date-based splits — never change these
TRAIN_START = "2021-06-01"
TRAIN_END   = "2024-12-31"
VAL_START   = "2025-01-01"
VAL_END     = "2025-12-31"
TEST_START  = "2026-01-01"   # touch ONCE for final eval

CONTEXT_LEN = 512   # must match score_from_db() and finetune.py
PRED_LEN    = 30    # must match score_from_db() and finetune.py
MIN_BARS    = CONTEXT_LEN + PRED_LEN   # minimum bars to form one window

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


def load_parquet_local(data_dir: Path) -> list[tuple[str, pd.DataFrame]]:
    """Load all .parquet files from a local directory."""
    files = sorted(data_dir.glob("*.parquet"))
    log.info("Found %d Parquet files in %s", len(files), data_dir)
    result = []
    for f in files:
        security_id = f.stem
        df = pd.read_parquet(f)
        result.append((security_id, df))
    return result


def load_parquet_s3() -> list[tuple[str, pd.DataFrame]]:
    """Load all .parquet files directly from S3."""
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_PREFIX + "/"):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".parquet"):
                keys.append(obj["Key"])

    log.info("Found %d Parquet files on S3", len(keys))
    result = []
    for key in tqdm(keys, desc="Downloading from S3"):
        security_id = Path(key).stem
        resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
        buf = resp["Body"].read()
        import io
        df = pd.read_parquet(io.BytesIO(buf))
        result.append((security_id, df))
    return result


# ── Core processing ───────────────────────────────────────────────────────────

def process_instrument(
    security_id: str,
    df: pd.DataFrame,
    out_dir: Path,
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

    df = add_amount(df)
    df = add_time_features(df)

    # Ensure numeric types
    for col in FEAT_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=FEAT_COLS)

    splits = split_by_date(df)
    stats  = {"security_id": security_id, "splits": {}}

    for split_name, split_df in splits.items():
        if len(split_df) < MIN_BARS:
            stats["splits"][split_name] = {"bars": len(split_df), "windows": 0}
            continue

        x     = split_df[FEAT_COLS].values.astype(np.float32)   # (T, 6)
        stamp = split_df[TIME_COLS].values.astype(np.float32)    # (T, 5)

        split_out = out_dir / split_name
        split_out.mkdir(parents=True, exist_ok=True)

        np.save(split_out / f"{security_id}_x.npy",     x)
        np.save(split_out / f"{security_id}_stamp.npy", stamp)

        n_windows = max(0, (len(split_df) - CONTEXT_LEN - PRED_LEN) // PRED_LEN + 1)
        stats["splits"][split_name] = {"bars": len(split_df), "windows": n_windows}

    return stats


# ── Dataset info ──────────────────────────────────────────────────────────────

def write_manifest(out_dir: Path, all_stats: list[dict]):
    total_train_windows = sum(
        s["splits"].get("train", {}).get("windows", 0) for s in all_stats
    )
    total_val_windows = sum(
        s["splits"].get("val", {}).get("windows", 0) for s in all_stats
    )
    manifest = {
        "prepared_at":        pd.Timestamp.now(tz="UTC").isoformat(),
        "context_length":     CONTEXT_LEN,
        "pred_length":        PRED_LEN,
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
    ap.add_argument("--limit", type=int, default=None,
                    help="Process only N instruments (for quick testing)")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)

    # Load
    if args.s3:
        instruments = load_parquet_s3()
    else:
        instruments = load_parquet_local(args.data)

    if args.limit:
        instruments = instruments[:args.limit]
        log.info("Limiting to %d instruments (--limit)", args.limit)

    log.info("Processing %d instruments → %s", len(instruments), args.out)

    all_stats = []
    skipped   = 0

    for security_id, df in tqdm(instruments, desc="Processing"):
        stats = process_instrument(security_id, df, args.out)
        if stats is None:
            skipped += 1
            continue
        all_stats.append(stats)

    write_manifest(args.out, all_stats)

    log.info("Done. %d instruments processed, %d skipped.", len(all_stats), skipped)
    log.info("")
    log.info("Next step — fine-tune on GPU instance:")
    log.info("  python finetune.py \\")
    log.info("    --model NeoQuasar/Kronos-base \\")
    log.info("    --data_path %s/train/ \\", args.out)
    log.info("    --val_path  %s/val/ \\", args.out)
    log.info("    --context_length %d \\", CONTEXT_LEN)
    log.info("    --prediction_length %d \\", PRED_LEN)
    log.info("    --output_dir ~/kronos-nse-v1/")


if __name__ == "__main__":
    main()
