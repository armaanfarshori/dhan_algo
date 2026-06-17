# M3 Decision Study — Run Plan (recommended params + thresholds to lock)

This proposes the concrete window, split, and run parameters for the M3 three-way
study so it can launch cleanly once (a) the rebuilt `dhan_clean` corpus is on S3
and (b) the fine-tuned Kronos checkpoint exists. **Lock Section 2 of
`results/M3-RESULTS-TEMPLATE.md` (countersigned) BEFORE viewing any result** —
copy it to `results/M3-YYYY-MM-DD.md` and fill Section 1 from here.

> Survivorship reminder: the universe is the current `clean_universe` (delisted
> names absent) → every result is an **optimistic upper bound**, not a forecast.

## Window & split (the critical choice)

| Field | Value | Why |
|---|---|---|
| Full window | **2021-06-01 → 2026-06-13** | full cleaned history |
| In-sample (IS) | **2021-06-01 → 2025-12-31** | tuning / fine-tune train+val |
| Out-of-sample (OOS) | **2026-01-01 → 2026-06-13** | held-out; never seen in tuning/fine-tune |

**Why OOS = 2026:** `prepare_kronos_dataset` date-splits train ≤2024 / val 2025 /
**test 2026**. For Run 3 (fine-tuned Kronos) to be honest, the OOS must be data the
fine-tune never touched → **2026**. Using the same OOS for Runs 1 & 2 keeps the
three-way comparison apples-to-apples. (OOS is ~5.5 months / ~110 sessions — watch
the OOS-trade-count gate in §2.2; bump `--n` if it's short of 50.)

## Run parameters (must match across all 3 runs)

| Param | Value | Note |
|---|---|---|
| `--equity` | 500,000 | paper_balance default |
| `--slippage-bps` | 2 (base) + **stress re-run at 4–5** | §3.5 stress |
| `--min-volume` | 50,000 | = live screener (CLI default) |
| `--n` | **10** | top-10 ATR%/day; more names → more trades for OOS validity, still ≤ max_open_positions(10) |
| `--split-date` | 2026-01-01 | one run emits IS+OOS panels |
| risk/caps | from `config.py` (risk 0.5%, max_open 10, daily-loss 2%, etc.) | embedded in result provenance |

## Commands

```
# Run 1 — ORB standalone
python -m research.backtest --from 2021-06-01 --to 2026-06-13 --split-date 2026-01-01 \
  --gate none --n 10 --equity 500000 --slippage-bps 2 --json results/run1.json

# Run 2 — ORB + Kronos zero-shot
python -m research.backtest --from 2021-06-01 --to 2026-06-13 --split-date 2026-01-01 \
  --gate kronos --n 10 --equity 500000 --slippage-bps 2 --json results/run2.json

# Run 3 — ORB + Kronos fine-tuned (after the GPU run uploads the checkpoint)
KRONOS_CHECKPOINT=s3://<bucket>/kronos/checkpoints/nse-5min-v1/ \
python -m research.backtest --from 2021-06-01 --to 2026-06-13 --split-date 2026-01-01 \
  --gate kronos --n 10 --equity 500000 --slippage-bps 2 --json results/run3.json

# Slippage stress — re-run the best variant at 4 bps (§3.5)
python -m research.backtest ... --slippage-bps 4 --json results/run_best_4bps.json
```

> Heavy: ~110 OOS + ~1,100 IS sessions × top-10 names. Run on the agent **off-hours**
> (never restart `dhan-trader` mid-session) or a separate worker. The Kronos-gated
> runs are CPU-heavy (inference per breakout) — budget hours; seed is fixed
> (`--kronos-seed 0`) so they're reproducible.

## Thresholds to lock (Section 2 defaults — review your risk appetite)

The template's proposed defaults are sound for a first live gate:
Sharpe (OOS) ≥ **1.0**, max DD ≤ **15%**, profit factor ≥ **1.3**, OOS net > 0;
trades ≥ 150 (full) / ≥ 50 (OOS); OOS÷IS Sharpe ≥ **0.70**; cost-retention ≥ 0.50;
top-5-day ≤ 50%; months-positive ≥ 60%; gate uplift ≥ +0.30; calibration ≥ 55%.

**Decision logic:** any FAIL on a *primary* or *statistical-validity* gate ⇒
**NO-GO**. FAIL only on a *gate-value* gate ⇒ ship **white-box ORB alone**, Kronos
stays shadow. See `Live-Readiness-Checklist.md`.
