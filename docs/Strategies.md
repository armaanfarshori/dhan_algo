# Strategies, Gate, and Risk

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
- One attempt per side per session (`_long_tried` / `_short_tried`); per-security entries capped by `max_orders_per_session`

### Exit rules

- **Target:** entry ± 1.5× the opening range (`target_multiplier`)
- **Stop:** opposite OR edge padded by `sl_buffer_pct`
- **EOD square-off:** 15 minutes before close, always

### Mid-session restart behaviour

A trader process started after 09:30 cannot observe the opening range live. Two safeguards:

1. The EOD square-off is evaluated **before** the OR-locked gate, so a restored position can never be stranded past close.
2. At boot, `seed_opening_ranges()` reconstructs each security's true OR from REST intraday bars and marks any side that already broke out (while the process was down) as *tried* — the engine never chases a breakout hours late at a worse price.

### Position state

The strategy holds only a *view* of the position (`position`, `entry_price`), updated via `notify_fill()`/`notify_flat()` by the runner. The authoritative position lives in the DB-persisted Portfolio and is resynced into the strategy at boot.

---

## Watchlist screener (`core/nse_screener.py`)

At trader boot, `get_top_volatile()` ranks NSE equities by average daily ATR% over the lookback window, with three filters that all exist because their absence cost real (paper) money:

| Filter | Default | Why |
|---|---|---|
| `min_avg_volume` | 50K shares/day | Illiquid names produce meaningless fills |
| `min_price` | ₹50 | ATR% naturally over-ranks penny stocks, where one tick ≈ several bps and simulated slippage flatters every fill |
| Scrip-master validation | EQUITY in segment | A cached watchlist once smuggled in a stock *index*, which traded both directions of a whipsaw for the worst loss of day 1 — and would have been rejected by the broker in live mode |

Securities holding open positions are always appended (orphan protection) and exempt from validation — whatever the engine holds, it must be able to exit.

---

## Kronos gate (`ml/kronos_gate.py`)

Kronos is an OHLCV foundation model (AAAI 2026): it tokenizes candles and forecasts the next bars. The platform uses it as a **directional gate** on ORB entries:

```
ORB wants BUY → KronosSignalEngine.score_from_db(security)
   → 400 × 1-min bars → 30-bar forecast
   → {side, confidence, forecasted_return, data_age_min}
shadow mode:    verdict + features persisted to signals; trade proceeds regardless
enforcing mode: confidence ≥ threshold AND direction agrees → allow, else block
```

Design rules:

- **Fail-open.** Any model error returns "allow" — an AI hiccup must never prevent the rule-based engine from managing risk.
- **Staleness is explicit.** Every verdict records `data_age_min`. A verdict scored on stale bars is flagged and excluded from calibration. (Historically, a feed bug meant *all* early verdicts were stale — which is exactly why the gate must earn its way out of shadow mode.)
- **Shadow until proven.** The gate blocks nothing until the calibration report says its blocks would have saved money.

## Calibration loop (`ml/calibration.py`)

The mechanism that decides whether the gate ever gets teeth:

- `python -m ml.calibration fill` — for each gate verdict, computes the realized 30-minute forward return of the underlying and writes it back into the verdict's feature snapshot. Runs from cron after each session.
- `python -m ml.calibration report` — on **fresh-data rows only**: ALLOW-vs-BLOCK outcome comparison (the gate-value measure), confidence-bucket accuracy, and a threshold sweep. Emits an explicit arm / do-not-arm verdict.

**Re-arm criterion: n ≥ 30 fresh outcomes with ≥ 55% directional accuracy.** Re-arming is manual: `KRONOS_SHADOW_MODE=false` + restart.

## Fine-tuning plan (after the clean data replica)

1. Build the clean training set (liquid names, corporate-action-adjusted, no circuit-breaker days)
2. Spot GPU (g4dn.xlarge), fine-tune **Kronos-base** — tokenizer first, predictor second
3. **Split by date, never randomly** (train ≤ 2024, validate 2025, test 2026 — test touched once)
4. Checkpoint → S3, terminate the GPU, set `KRONOS_CHECKPOINT`
5. Promote only if the three-way backtest (ORB vs +zero-shot vs +fine-tuned, identical costs) shows a meaningfully better Sharpe

---

## Risk engine (`engine/risk.py`)

- **Sizing:** `qty = equity × risk_per_trade / |entry − stop|`, capped by `max_notional_per_trade`. Stop distance comes from the strategy's Decision, so wider ranges automatically mean smaller positions.
- **Daily-loss halt:** monitors the **portfolio** (realized + unrealized — paper losses trip it too; an earlier design watched the empty real account, so the halt could never fire in paper). Breach → halt, flatten all positions, Telegram alert.
- **Kill switch:** a file at `run/killswitch` (written by `POST /api/killswitch`, or by hand) routes through the same halt path within ~10 s. The RiskEngine is the *only* component that may halt; nothing bypasses it.
