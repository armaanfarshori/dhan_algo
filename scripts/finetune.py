#!/usr/bin/env python3
"""
finetune.py — Fine-tune NeoQuasar/Kronos-base on NSE 1m bar data

Run on g4dn.xlarge spot GPU instance after prepare_kronos_dataset.py:
  python finetune.py \\
    --model NeoQuasar/Kronos-base \\
    --data_path ~/nse_dataset/train/ \\
    --val_path  ~/nse_dataset/val/ \\
    --context_length 512 \\
    --prediction_length 30 \\
    --output_dir ~/kronos-nse-v1/

After training:
  aws s3 sync ~/kronos-nse-v1/ s3://$S3_BUCKET/kronos/checkpoints/nse-v1/
  # Then on agent EC2:
  # KRONOS_CHECKPOINT=s3://$S3_BUCKET/kronos/checkpoints/nse-v1/
  # Restart platform — KronosSignalEngine lazy-loads the new checkpoint on first signal
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("finetune")


# ── Dataset ───────────────────────────────────────────────────────────────────

class KronosWindowDataset(Dataset):
    """
    Sliding-window dataset over per-instrument numpy files.

    Each sample is a (context_len + pred_len) window.
    Normalization: per-window z-score (matches KronosPredictor.predict()).
    """

    def __init__(self, data_dir: Path, context_len: int, pred_len: int,
                 stride: int | None = None, clip: float = 5.0):
        self.context_len = context_len
        self.pred_len    = pred_len
        self.window_len  = context_len + pred_len
        self.stride      = stride or pred_len   # default: non-overlapping prediction
        self.clip        = clip

        self.windows: list[tuple[np.ndarray, np.ndarray, int, int]] = []
        # Each entry: (x_array, stamp_array, start_idx, end_idx)

        x_files = sorted(Path(data_dir).glob("*_x.npy"))
        log.info("Loading %d instruments from %s", len(x_files), data_dir)

        total_windows = 0
        for xf in x_files:
            sf = xf.with_name(xf.name.replace("_x.npy", "_stamp.npy"))
            if not sf.exists():
                continue
            x     = np.load(xf, mmap_mode="r")   # memory-mapped, lazy
            stamp = np.load(sf, mmap_mode="r")
            T = len(x)
            if T < self.window_len:
                continue
            for start in range(0, T - self.window_len + 1, self.stride):
                self.windows.append((x, stamp, start, start + self.window_len))
                total_windows += 1

        log.info("Total windows: %s", f"{total_windows:,}")

    def __len__(self) -> int:
        return len(self.windows)

    def __getitem__(self, idx: int):
        x_arr, stamp_arr, start, end = self.windows[idx]

        x     = x_arr[start:end].copy().astype(np.float32)     # (window_len, 6)
        stamp = stamp_arr[start:end].copy().astype(np.float32)  # (window_len, 5)

        # Z-score using CONTEXT-ONLY stats, then apply to the whole window. This
        # matches KronosPredictor.predict (kronos/kronos.py), which at inference can
        # only see the context: it computes mean/std over the lookback and predicts
        # in that frame. Normalising over the full window (incl. the target bars)
        # would leak future information — a train/inference distribution mismatch.
        x_ctx  = x[:self.context_len]
        x_mean = x_ctx.mean(axis=0, keepdims=True)
        x_std  = x_ctx.std(axis=0,  keepdims=True)
        x_norm = (x - x_mean) / (x_std + 1e-5)
        x_norm = np.clip(x_norm, -self.clip, self.clip)

        x_ctx   = x_norm[:self.context_len]        # (ctx, 6)
        x_tgt   = x_norm[self.context_len:]        # (pred, 6)
        s_ctx   = stamp[:self.context_len]          # (ctx, 5)
        s_tgt   = stamp[self.context_len:]          # (pred, 5)

        return {
            "x":       torch.from_numpy(x_ctx),
            "x_stamp": torch.from_numpy(s_ctx),
            "y":       torch.from_numpy(x_tgt),
            "y_stamp": torch.from_numpy(s_tgt),
        }


# ── Loss ─────────────────────────────────────────────────────────────────────

def compute_loss(tokenizer, model, batch, device):
    """
    Next-token cross-entropy over the full window — mirrors the OFFICIAL Kronos
    trainer (shiyu-coder/Kronos `finetune/train_predictor.py`):

        s1_ids, s2_ids = tokenizer.encode(window, half=True)
        token_in  = tokens[:, :-1]      token_out = tokens[:, 1:]   # next-token
        s1_logits, s2_logits = model(token_in_s1, token_in_s2, stamp_in)
        loss = model.head.compute_loss(s1_logits, s2_logits, out_s1, out_s2)

    Three things this deliberately does NOT do (the previous version did, wrongly):
      • No `use_teacher_forcing=True`. The default forward path conditions the s2
        head on the model's OWN sampled s1 — exactly what `auto_regressive_inference`
        does at serving time. Teacher forcing here would create a train/inference
        mismatch on the s2 (sibling) head.
      • No `s1_targets=s1_input`. That conditioned s2 on the *current* s1 token,
        but s2 must be conditioned on the s1 of the position it is predicting
        (off-by-one). The official recipe sidesteps this entirely (no TF).
      • No "last pred_len positions only" slice. Loss is next-token over the WHOLE
        window (how the base model was pre-trained), and via the head's own
        averaged CE — not a manual summed CE.

    The input window is already per-window z-scored + clipped in
    KronosWindowDataset (matching KronosPredictor.predict), so no normalisation here.
    """
    x       = batch["x"].to(device)       # (B, ctx, 6)
    y       = batch["y"].to(device)       # (B, pred, 6)
    x_stamp = batch["x_stamp"].to(device) # (B, ctx, 5)
    y_stamp = batch["y_stamp"].to(device) # (B, pred, 5)

    # Tokenise the full context+target window (the model predicts next-token across it)
    full_x     = torch.cat([x, y], dim=1)             # (B, ctx+pred, 6)
    full_stamp = torch.cat([x_stamp, y_stamp], dim=1) # (B, ctx+pred, 5)

    with torch.no_grad():
        s1_ids, s2_ids = tokenizer.encode(full_x, half=True)  # each (B, T)

    token_in  = [s1_ids[:, :-1], s2_ids[:, :-1]]
    token_out = [s1_ids[:, 1:],  s2_ids[:, 1:]]
    stamp_in  = full_stamp[:, :-1]

    # Default forward (use_teacher_forcing=False): s2 head conditions on the model's
    # own sampled s1 — train/inference consistent.
    s1_logits, s2_logits = model(token_in[0], token_in[1], stamp=stamp_in)

    loss, _s1_loss, _s2_loss = model.head.compute_loss(
        s1_logits, s2_logits, token_out[0], token_out[1])
    return loss


# ── Training loop ─────────────────────────────────────────────────────────────

def train(args):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    log.info("Device: %s", device)

    # Load pre-trained Kronos tokenizer + model
    log.info("Loading %s from HuggingFace…", args.model)
    from kronos.kronos import KronosTokenizer, Kronos

    # Tokenizer and model are SEPARATE HuggingFace repos (the tokenizer is a
    # pretrained VQ network with its own config); loading the tokenizer from the
    # MODEL repo fails — KronosTokenizer.__init__ needs args the model config
    # doesn't carry. Mirror the live loader (core/kronos_signal.py).
    tokenizer = KronosTokenizer.from_pretrained(args.tokenizer)
    model     = Kronos.from_pretrained(args.model)

    tokenizer = tokenizer.to(device).eval()   # tokenizer stays frozen
    model     = model.to(device).train()

    # Freeze tokenizer — fine-tune predictor only
    for p in tokenizer.parameters():
        p.requires_grad_(False)

    log.info("Tokenizer parameters: frozen")
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    log.info("Trainable model parameters: %s", f"{trainable:,}")

    # Datasets
    train_ds = KronosWindowDataset(args.data_path, args.context_length,
                                   args.prediction_length, stride=args.stride)
    val_ds   = KronosWindowDataset(args.val_path,  args.context_length,
                                   args.prediction_length, stride=args.prediction_length)

    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True,
                              num_workers=4, pin_memory=True)
    val_loader   = DataLoader(val_ds,   batch_size=args.batch_size, shuffle=False,
                              num_workers=2, pin_memory=True)

    # Optimizer + scheduler
    optimizer = optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)
    steps_per_epoch = len(train_loader)
    total_steps     = steps_per_epoch * args.epochs
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=total_steps,
                                                      eta_min=args.lr * 0.1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    best_val_loss = float("inf")
    history = []

    for epoch in range(1, args.epochs + 1):
        # ── Train ─────────────────────────────────────────────────
        model.train()
        train_loss = 0.0
        t0 = time.time()

        for step, batch in enumerate(tqdm(train_loader, desc=f"Epoch {epoch}/{args.epochs}")):
            optimizer.zero_grad()
            loss = compute_loss(tokenizer, model, batch, device)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            scheduler.step()
            train_loss += loss.item()

            if step % 200 == 0 and step > 0:
                avg = train_loss / (step + 1)
                lr  = scheduler.get_last_lr()[0]
                log.info("  step %d/%d  loss=%.4f  lr=%.2e", step, steps_per_epoch, avg, lr)

        train_loss /= len(train_loader)

        # ── Validate ───────────────────────────────────────────────
        model.eval()
        val_loss = 0.0
        with torch.no_grad():
            for batch in tqdm(val_loader, desc="Validating"):
                val_loss += compute_loss(tokenizer, model, batch, device).item()
        val_loss /= len(val_loader)

        elapsed = time.time() - t0
        log.info("Epoch %d/%d  train_loss=%.4f  val_loss=%.4f  time=%.0fs",
                 epoch, args.epochs, train_loss, val_loss, elapsed)

        history.append({"epoch": epoch, "train_loss": train_loss, "val_loss": val_loss})

        # Save best checkpoint
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            log.info("  ✓ New best val_loss=%.4f — saving checkpoint", val_loss)
            model.save_pretrained(output_dir / "best")
            tokenizer.save_pretrained(output_dir / "best")

        # Save latest checkpoint every epoch
        model.save_pretrained(output_dir / "latest")
        tokenizer.save_pretrained(output_dir / "latest")

    # Save training history + config
    config = {
        "base_model":        args.model,
        "context_length":    args.context_length,
        "prediction_length": args.prediction_length,
        "epochs":            args.epochs,
        "batch_size":        args.batch_size,
        "lr":                args.lr,
        "best_val_loss":     best_val_loss,
        "history":           history,
    }
    (output_dir / "training_config.json").write_text(json.dumps(config, indent=2))

    log.info("Training complete. Best val_loss=%.4f", best_val_loss)
    log.info("Checkpoint saved to: %s/best/", output_dir)

    s3_uri = None
    if args.upload_s3:
        s3_uri = _upload_checkpoint_s3(output_dir / "best", args.s3_name)

    log.info("")
    if s3_uri:
        log.info("Checkpoint uploaded to %s", s3_uri)
        log.info("Then on agent EC2 — set in .env and restart platform:")
        log.info("  KRONOS_CHECKPOINT=%s", s3_uri)
    else:
        log.info("Upload to S3 (or re-run with --upload-s3):")
        log.info("  aws s3 sync %s/best/ s3://$S3_BUCKET/kronos/checkpoints/%s/",
                 output_dir, args.s3_name)
        log.info("Then on agent EC2 — set in .env and restart platform:")
        log.info("  KRONOS_CHECKPOINT=s3://$S3_BUCKET/kronos/checkpoints/%s/", args.s3_name)


def _upload_checkpoint_s3(local_dir: Path, name: str) -> str | None:
    """Sync a local checkpoint dir to s3://$S3_BUCKET/kronos/checkpoints/<name>/.
    Returns the s3:// URI on success, None if S3_BUCKET is unset/upload failed.
    The URI it returns is exactly what KRONOS_CHECKPOINT expects (the live
    KronosSignalEngine syncs it back down on first load)."""
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        log.warning("--upload-s3 set but S3_BUCKET is empty — skipping upload")
        return None
    try:
        import boto3
        s3 = boto3.client("s3")
        prefix = f"kronos/checkpoints/{name}"
        n = 0
        for f in sorted(Path(local_dir).rglob("*")):
            if f.is_file():
                rel = f.relative_to(local_dir).as_posix()
                s3.upload_file(str(f), bucket, f"{prefix}/{rel}")
                n += 1
        log.info("Uploaded %d checkpoint files to s3://%s/%s/", n, bucket, prefix)
        return f"s3://{bucket}/{prefix}/"
    except Exception as exc:
        log.error("Checkpoint S3 upload failed: %s", exc)
        return None


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Fine-tune Kronos-base on NSE data")
    ap.add_argument("--model",             default="NeoQuasar/Kronos-base")
    ap.add_argument("--tokenizer",         default="NeoQuasar/Kronos-Tokenizer-base",
                    help="Tokenizer HF repo — SEPARATE from --model (matches "
                         "config.kronos_tokenizer / core.kronos_signal)")
    ap.add_argument("--data_path",         type=Path, required=True,
                    help="Train split directory from prepare_kronos_dataset.py")
    ap.add_argument("--val_path",          type=Path, required=True,
                    help="Val split directory from prepare_kronos_dataset.py")
    ap.add_argument("--output_dir",        type=Path, default=Path("~/kronos-nse-v1").expanduser())
    ap.add_argument("--context_length",    type=int,  default=512)
    ap.add_argument("--prediction_length", type=int,  default=30)
    ap.add_argument("--stride",            type=int,  default=None,
                    help="Window stride (default: pred_length = non-overlapping predictions)")
    ap.add_argument("--epochs",            type=int,  default=3)
    ap.add_argument("--batch_size",        type=int,  default=32)
    ap.add_argument("--lr",                type=float, default=1e-4)
    # For the 5-min A/B variant, pass --context_length 480 --prediction_length 6
    # (matches config kronos_lookback/kronos_pred_len) against the 5min dataset.
    ap.add_argument("--upload-s3", action="store_true", dest="upload_s3",
                    help="Sync best/ to s3://$S3_BUCKET/kronos/checkpoints/<s3-name>/ after training")
    ap.add_argument("--s3-name", dest="s3_name", default="nse-v1",
                    help="Checkpoint name under kronos/checkpoints/ (e.g. nse-5min-v1)")
    args = ap.parse_args()

    train(args)


if __name__ == "__main__":
    main()
