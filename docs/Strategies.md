# Strategies, Gate, and Risk

*Last updated: 2026-06-16*

## Opening Range Breakout (`strategies/orb.py`)

The only active strategy. It is a **pure, synchronous class** — no IO, no client, no mode awareness — consumed identically by the live runner and the backtester.

### Session lifecycle

```
09:15–09:30  build the opening range (OR) from tick highs/lows
09:30        lock OR (high, low)
09:30–15:15  trade breakouts; manage exits
15:15        EOD square-off (unconditional — runs even if the OR is unknown)
```

### Entry rules

- **Long:** price breaks above OR high (first attempt per side per day)
- **Short:** price breaks below OR low
- Skip if the range is too narrow (`min_range_pct` of price) — a zero-width range carries no information
- One attempt per side per session (`_long_tried` / `_short_tried`)

### Exit rules

- **Target:** entry ± **1.5×** the opening range (`target_multiplier = 1.5` in `ORBParams`)
- **Stop:** opposite OR edge padded by `sl_buffer_pct` (0.2% default)
- **EOD square-off:** 15 minutes before close (`squareoff_before_close_min = 15`), always

### Mid-session restart behaviour

A trader process started after 09:30 cannot observe the opening range live. Two safeguards:

1. The EOD square-off is evaluated **before** the OR-locked gate, so a restored position can never be stranded past close.
2. At boot, `seed_opening_range()` reconstructs each security's true OR from REST intraday bars and marks any side that already broke out (while the process was down) as *tried* — the engine never chases a breakout hours late at a worse price.

### Position state

The strategy holds only a *view* of the position (`position`, `entry_price`), updated via `notify_fill()`/`notify_flat()` by the runner. The authoritative position lives in the DB-persisted Portfolio and is resynced into the strategy at boot.

---

## Watchlist screener (`core/nse_screener.py`)

At trader boot, the screener ranks NSE equities by average daily ATR% over the lookback window, with three filters that all exist because their absence cost real (paper) money:

| Filter | Default | Why |
|---|---|---|
| `min_avg_volume` | 50,000 shares/day | Illiquid names produce meaningless fills |
| `min_price` | ₹50 | ATR% naturally over-ranks penny stocks, where one tick ≈ several bps and simulated slippage flatters every fill |
| Scrip-master validation | EQUITY in segment | A cached watchlist once smuggled in a stock *index*, which traded both directions of a whipsaw for the worst loss of day 1 — and would have been rejected by the broker in live mode |

Securities holding open positions are always appended (orphan protection) and exempt from validation — whatever the engine holds, it must be able to exit.

---

## Kronos gate (`ml/kronos_gate.py`)

Kronos is an OHLCV foundation model (AAAI 2026): it tokenizes candles and forecasts the next bars. The platform uses it as a **directional gate** on ORB entries:

```
ORB wants BUY → KronosSignalEngine.score_from_db(security)
   → 480 × 5-min bars (aggregated from 1-min DB rows)
   → 6-bar forecast (6 × 5 min = 30-min horizon)
   → {side, score, confidence, forecasted_return, data_age_min}
shadow mode:    verdict + features persisted to signals; trade proceeds regardless
enforcing mode: confidence ≥ min_confidence (0.4) AND direction agrees → allow, else block
```

### Scorer v2 (5-min aggregation)

NSE equity bars are in the Kronos pre-training corpus **at 5-min granularity only** — feeding 1-min bars is out-of-distribution for the model. Scorer v2 (active since 2026-06-12) aggregates 1-min DB rows into 5-min buckets before inference, matching the paper's protocol (arXiv:2508.02739):

| Parameter | Value | Note |
|---|---|---|
| `kronos_timeframe` | `5min` | Aggregation bucket (OOD if `1min`) |
| `kronos_lookback` | 480 | Bars at scoring timeframe |
| `kronos_pred_len` | 6 | Forecast bars → 30-min horizon |
| `kronos_samples` (N) | 10 | Monte-Carlo rollouts |
| `kronos_temperature` (T) | 0.6 | Paper's price/return-forecasting value |
| `kronos_top_p` | 0.90 | Paper's value |

The scorer version string (`scorer_version()`) encodes these parameters so calibration data from different configs is never pooled.

### `_directional_confidence()` — the confidence formula

```python
rel_std    = std(pred_close) / (current_price + 1e-9)
confidence = 1.0 - clamp(rel_std * 10, 0, 1)
```

This maps the coefficient of variation of Monte-Carlo close-price forecasts into [0, 1]:

- `rel_std = 0.00` → confidence 1.0 (samples perfectly agree)
- `rel_std = 0.05` → confidence 0.5 (5% inter-sample price spread)
- `rel_std ≥ 0.10` → confidence 0.0 (clamped — maximally uncertain)

Observed live values for NSE equities in the ₹200–₹2000 range fall in the 0.85–0.99 region. The scale factor of 10 is an empirical constant; any change requires a `scorer_version` bump to protect in-flight calibration data.

### Design rules

- **Fail-open.** Any model error returns `allows=True` — an AI hiccup must never prevent the rule-based engine from managing risk.
- **Staleness is explicit.** Every verdict records `data_age_min`. Decisions scored on bars older than 15 minutes are flagged `stale` and excluded from calibration. (A live-feed bug silenced the WebSocket for the platform's entire pre-2026-06-12 session history — all those verdicts are stale by construction.)
- **Shadow until proven.** The gate blocks nothing until the calibration report says its blocks would have saved money.

---

## Calibration loop (`ml/calibration.py`)

The mechanism that decides whether the gate ever gets teeth.

### Stage 1: `fill`

```bash
python -m ml.calibration fill [--days 14] [--horizon 30]
```

For every BUY/SELL verdict old enough to have a 30-minute outcome, computes the realized forward return from the `bars` table and writes it back into the verdict's `features_snapshot` in `signals`. Idempotent (already-filled rows are skipped). Runs from cron after each session at 16:45 IST.

### Stage 2: `report`

```bash
python -m ml.calibration report [--days 30] [--json out.json]
```

On **fresh-data rows only** (decisions scored on live bars, not stale history):

- **Gate value:** do ALLOW-verdict breakouts outperform BLOCK-verdict ones? This is the primary re-arm criterion — if the gate's blocks don't save money, it has no value at any threshold.
- **Confidence-bucket accuracy:** directional hit rate per 0.1-width confidence bucket.
- **Threshold sweep:** accuracy and sample count at each confidence threshold from 0.00 to 0.90.
- **Recommendation:** explicit "ARM / DO NOT ARM" text, or "INSUFFICIENT EVIDENCE".

**Re-arm criterion: n ≥ 30 fresh outcomes with ≥ 55% directional accuracy at the recommended threshold, AND ALLOWed breakouts outperform BLOCKed ones.** Re-arming is a deliberate human act: set `KRONOS_SHADOW_MODE=false` + restart dhan-trader.

---

## Fine-tuning plan (after the clean data replica)

1. Build the clean training set (liquid names, corporate-action-adjusted, no circuit-breaker days) — `scripts/build_clean_db.py` + `scripts/prepare_kronos_dataset.py`
2. Spot GPU (g4dn.xlarge), fine-tune **Kronos-base** (`scripts/finetune.py`)
3. **Split by date, never randomly** (train ≤ 2024, validate 2025, test 2026 — test touched once)
4. Checkpoint → S3, terminate the GPU, set `KRONOS_CHECKPOINT` env var
5. Promote only if the three-way backtest (ORB vs +zero-shot vs +fine-tuned, identical costs) shows a meaningfully better Sharpe

---

## Risk engine (`engine/risk.py`)

All limits are **fractions of equity**, never absolute rupees — so paper rehearses the same risk geometry that live will use. Live mode additionally scales every fraction by `live_risk_scale` (0.5 by default — training wheels for M8 tiny-live).

### Risk parameters (defaults)

| Parameter | Default | Meaning |
|---|---|---|
| `risk_per_trade` | 0.5% | Fraction of equity at risk per trade (stop-to-entry distance) |
| `max_daily_loss_pct` | 2% | Halt + flatten for the day when today's P&L (realized + unrealized) breaches this |
| `weekly_loss_pct` | 5% | Halt until next Monday |
| `max_notional_pct` | 20% | Per-trade notional cap |
| `max_gross_exposure_pct` | 100% | Sum of all open notionals — no implicit leverage |
| `adv_participation_pct` | 1% | Qty ≤ this fraction of 20-day average daily volume |
| `min_stop_distance_pct` | 0.35% | Stop-distance floor — thin ORB ranges cannot explode position size |
| `max_open_positions` | 10 | |

### Sizing

```
stop_dist = max(|entry − stop|, entry × min_stop_distance_pct)
qty       = int(equity × risk_per_trade / stop_dist)
qty       = min(qty, equity × max_notional_pct / entry)
qty       = min(qty, 20d_ADV × adv_participation_pct)   # liquidity cap
```

The 20-day ADV query uses a time-bounded `WHERE time >= CURRENT_DATE - 30 days` on the `bars` hypertable so TimescaleDB can exclude old chunks without a full-table scan.

### P&L tracking (DB-backed, restart-proof)

Realized P&L (all-time / today / this week) comes from the `trades` table, refreshed every monitoring cycle. A process restart never resets the loss meter. If the DB query fails, the engine falls back to the in-process portfolio with a warning.

### Persistent loss halts

A daily or weekly loss halt is written to `run/halt_state.json` before the halt fires. On the next restart, `load_persisted_halt()` re-enters the halt if it is still in scope — a restart cannot silently re-arm a halted session. To resume: `DELETE run/halt_state.json` + restart, or `POST /api/killswitch` followed by manual resume.

### Kill switch

`POST /api/killswitch` (protected by `DASHBOARD_TOKEN`) writes `run/killswitch`. The RiskEngine's monitoring loop picks it up within ~10 seconds and halts + flattens. The RiskEngine is the **only** component that may halt; nothing bypasses it.
