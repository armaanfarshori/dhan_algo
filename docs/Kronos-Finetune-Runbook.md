# Kronos Fine-Tune Runbook (GPU spot)

End-to-end steps to fine-tune Kronos-base on the NSE corpus and wire the
checkpoint into the live gate + M3 Run 3. Runs on a **throwaway GPU spot**, OFF
the live agent. Prereq: the rebuilt M2.5 corpus is on `s3://<bucket>/kronos/training-data/`.

> Standardized on **Kronos-base** (config `kronos_model`). A/B the **5-min** (live
> granularity) vs **1-min** (legacy/OOD) datasets — build both, compare in M3 shadow.

## 0. Costs / safety
- g4dn.xlarge spot ≈ $0.20–0.40/hr; a few hours total. **Terminate when done.**
- No secrets needed beyond AWS creds for S3 (instance profile or `secrets-sync.sh pull`).
- This never touches the live trader or `dhan_trading`.

## 1. Launch GPU spot + env
```
# g4dn.xlarge spot, Deep Learning AMI (PyTorch). Then on the box:
git clone https://github.com/armaanfarshori/dhan_algo && cd dhan_algo
pip install -r requirements.txt            # incl. torch, boto3, pyarrow, tqdm, einops, safetensors
export S3_BUCKET=<data-bucket>             # or: aws configure / instance profile
```

## 2. Sync the corpus from S3
```
aws s3 sync s3://$S3_BUCKET/kronos/training-data/ ~/nse_data/    # ~1,674 parquet
```

## 3. Build datasets (both granularities for the A/B)
```
# 5-min (matches live serving: ctx 480, pred 6) — PRIMARY
python -m scripts.prepare_kronos_dataset --s3 --timeframe 5min --out ~/nse_5min/
# 1-min (legacy/OOD: ctx 512, pred 30) — A/B comparison
python -m scripts.prepare_kronos_dataset --s3 --timeframe 1min --out ~/nse_1min/
```
Each writes `train/ val/ test/` numpy + a `manifest.json` (records timeframe + ctx/pred).
Date split is strict: train ≤2024 / val 2025 / test 2026 (never randomized).

## 4. Fine-tune (per granularity) + upload checkpoint to S3
```
# 5-min variant
python -m scripts.finetune --model NeoQuasar/Kronos-base \
  --data_path ~/nse_5min/train/ --val_path ~/nse_5min/val/ \
  --context_length 480 --prediction_length 6 \
  --output_dir ~/kronos-nse-5min-v1/ --upload-s3 --s3-name nse-5min-v1

# 1-min variant (A/B)
python -m scripts.finetune --model NeoQuasar/Kronos-base \
  --data_path ~/nse_1min/train/ --val_path ~/nse_1min/val/ \
  --context_length 512 --prediction_length 30 \
  --output_dir ~/kronos-nse-1min-v1/ --upload-s3 --s3-name nse-1min-v1
```
`--upload-s3` syncs `best/` → `s3://$S3_BUCKET/kronos/checkpoints/<name>/` (the exact
`KRONOS_CHECKPOINT` value it prints). Tokenizer is frozen; only the predictor trains.

## 5. Wire into the live gate (M3 Run 3)
- The live `KronosSignalEngine` reads `cfg.kronos_checkpoint`; if `s3://`, it syncs to
  `~/.cache/dhan_kronos/` once and loads via `from_pretrained` (fail-open to zero-shot).
- For M3 Run 3 (off-hours, not the live trader):
  ```
  KRONOS_CHECKPOINT=s3://$S3_BUCKET/kronos/checkpoints/nse-5min-v1/ \
  python -m research.backtest --from 2021-06-01 --to 2026-06-13 --split-date 2026-01-01 \
    --gate kronos --n 10 --json results/run3.json
  ```
- To promote to LIVE: set `KRONOS_CHECKPOINT` in the agent `.env` + restart
  `dhan-trader` (off market hours) — ⚠️ Kronos-base on the t4g.large (8GB) agent
  lazy-loads on first signal; verify the load succeeds (RAM) before relying on it.

## 6. Decide (promotion)
Compare M3 Run 3 (fine-tuned) vs Run 2 (zero-shot): promote only on **OOS Sharpe
uplift ≥ +0.30** and no worse drawdown (`results/M3-RESULTS-TEMPLATE.md` §2.5).
Pick 5-min vs 1-min by the same OOS metric. Then **terminate the GPU spot.**
