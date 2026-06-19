# DhanAIBot — F&O Hedged-Options Research Handoff

> **You are in a dedicated git worktree** (`~/Desktop/dhan_algo_fno`) on branch
> **`feat/fno-data-foundation`**, isolated from the main session (which is running
> the equity-Kronos GPU fine-tune in `~/Desktop/dhan_algo` on `main`). Do **not**
> touch the live order paths, the running fine-tune, or `main`. Branch + PR only.

---

## 0a. TRUSTED-MACHINE CORRECTIONS — apply these (repo ground-truth, 2026-06-19)

The research handoff below was written without live repo access. Verified against the
actual repo, these specifics are **wrong and must be adapted**:

| Handoff says | Repo reality → do this instead |
|---|---|
| `migrations/004_fno_foundation.sql` (raw SQL) | We use **Alembic**. Current head = **008** (`alembic/versions/008_daily_screen.py`). Create **`alembic/versions/009_fno_foundation.py`** using `op.create_table(...)` + `op.execute("SELECT create_hypertable(...)")`, matching the style of existing migrations. Provide a real `downgrade()` that drops only the 4 new tables. |
| Add gate to **`core/kronos_gate.py`** | The gate is at **`ml/kronos_gate.py`** and it is the **LIVE equity ORB shadow gate**. Do **NOT** repurpose it in place. Build a **new** module: **`ml/fno_vol_gate.py`**. |
| Creds via `core/auth.py` | No `core/auth.py`. Use **`config.py`** (pydantic-settings, the only env reader) + `core/client.py` / `core/token_manager.py`. |
| "check in-flight branches `feat/m25-…`, `feat/s3-…`, `chore/infra-…`" | **Already merged** (0 open PRs). Ignore — no duplication risk. |
| "existing OptionsScalper OCO logic transfers" | **Unverified — does not appear to exist.** Treat as a Phase-2 to-confirm, not a given. |
| `main.py` | **Gone** (removed in the Phase-1 rewrite). Two systemd processes: `apps/trader.py` + `apps/api.py`. |

**Open Q#4 is a real design decision (don't assume):** the existing Kronos emits a
**directional** signal for ORB (`core/kronos_signal.py: score_from_db → {side, score,
confidence, forecasted_return}`), **not a realized-vol forecast**. The checkpoint being
fine-tuned right now is **equity-trained** — it cannot forecast NIFTY vol without NIFTY
data (which this Phase 0 builds). So `ml/fno_vol_gate.py` should start with a **vol proxy**
(e.g. Kronos forecast dispersion, or an EWMA/GARCH of futures returns) until a NIFTY-trained
Kronos exists. Decide this explicitly in PR4; don't wire a vol forecast that doesn't exist.

**Conventions to match:** ruff clean + `pytest -q` green (use a Python 3.12 venv —
the repo standardizes on 3.12; the Mac's default `python3` is 3.9 and will false-fail
`dict | None` imports). CI gates every PR (3.12, x86+ARM, coverage, ruff). `PAPER_TRADING`
stays `true`. The agent merges PRs itself once green + outside market hours (09:15–15:30 IST).

**First action:** read this whole file + `CLAUDE.md`, then add a one-line F&O scope note
to `CLAUDE.md`, then do **PR1 = the Alembic 009 migration only**. Stop after PR5
(backtest go/no-go) and report before any Phase 2 strategy build.

---

## Original research handoff (verbatim)

**For:** Claude Code (trusted machine — executes, tests, deploys)
**From:** Research/architecture session (no infra access by design)
**Date:** 2026-06-19
**Status:** Research complete → ready to scaffold Phase 0 (data) on a feature branch

### 0. Read first / hard rules (from CLAUDE.md — do not violate)
- `PAPER_TRADING = true` stays true. Nothing in this handoff places a real order.
- Propose all changes on a **feature branch**; never commit to `main`. Branch: `feat/fno-data-foundation`.
- **No live/infra actions during market hours (09:15–15:30 IST).** Backfill of *historical* data is fine off-hours; anything touching live order paths is out of scope here.
- Never hardcode real IPs / account IDs / tokens (repo is private now, but treat as if public). Use `.env` + SSM.
- This is a **research → data-foundation** handoff. It does NOT yet build strategies or touch order placement. Strategy code is Phase 2, gated on Phase 0–1 results.

### 1. Why this work exists
Extending DhanAIBot from equity-spot into **index options**, starting with **NIFTY**, using
**hedged, defined-risk premium-selling** (iron condor / iron fly / credit spreads) rather than
naked selling — India's SPAN margin rewards hedging massively (a NIFTY iron condor needs ~₹44k
vs ~₹1.45L for the equivalent naked short strangle). ORB is kept but **demoted** (small/decaying
directional edge; must NOT be run as option-buying — theta works against it). Kronos is
repurposed into a **volatility-regime gate**: sell premium only when predicted realized vol <
implied move (harvesting the NIFTY variance risk premium, positive ~75% of days). **F&O data
must exist in TimescaleDB first — it does not. Phase 0 closes that gap.**

### 2. Current blocker
TimescaleDB holds **NSE_EQ spot bars only**. Zero F&O data (no futures bars, no option chain,
no IV, no India VIX). Cannot compute realized-vs-implied vol, price spreads, calibrate the gate,
or backtest. **Data first.**

### 3. Phase 0 — Bare-minimum dataset
**3.1 Three datasets**
1. **NIFTY futures bars — 1d close** (front-month or continuous), 2yr (target 5yr). 20d realized-vol + Kronos input. ~500 rows/yr.
2. **India VIX — 1d close**, 2yr. Source: NSE public CSV (free, no API quota). Implied-move baseline.
3. **ATM straddle IV at entry** — per weekly expiry, ATM call IV + put IV sampled once/day (EOD or ~15:00 IST). NOT the full chain. ~250 rows/yr. Implied move + spread pricing.

**3.2 OUT of scope for Phase 0 (defer):** full option chain, intraday option snapshots, Greeks
history, bid-ask history, BANKNIFTY/FINNIFTY/SENSEX. Add only after the NIFTY gate is validated.

**3.3 Dhan rate-limit:** Free tier ~100k calls/day. ATM-only straddle = ~250 calls per 2yr →
negligible (do NOT loop the full chain). Futures 1d = a handful. India VIX = zero API (CSV).
Reuse the equity backfill's rate-limiter + retry + screen-session pattern. Suspended/illiquid →
0 candles is normal; skip silently.

### 4. Schema (proposed — adapt to Alembic 009 per corrections above)
Four hypertables (additive; `IF NOT EXISTS`; no changes to existing tables):
- **futures_bars**(time, symbol, timeframe, open, high, low, close, volume, open_interest, expiry_date, realized_vol_20d) — hypertable on time; index (symbol, timeframe, time DESC).
- **option_atm_iv**(time, symbol, expiry_date, expiry_type, atm_strike, call_iv, put_iv, straddle_iv, dte, spot_ref, implied_move) — hypertable on time; index (symbol, expiry_date, time DESC).
- **index_bars**(time, security_id, symbol, timeframe, open, high, low, close) — hypertable on time; covers NIFTY 50 (security_id=13) and India VIX (security_id=21) via Dhan charts (IDX_I segment), plus any other index symbols ingested with `--index`. Replaces the earlier `india_vix` single-column table.
- **option_chain_snapshot**(time, security_id, symbol, expiry_date, strike, option_type, open, high, low, close, volume, open_interest, iv, delta, theta) — full chain snapshots; hypertable on time.
- **fno_instruments**(security_id, symbol, instrument_type, expiry_date, strike, option_type, lot_size, tick_size, segment) — detailed F&O scrip master from the Dhan instruments file; PK (security_id).
- **expiry_calendar**(symbol, expiry_date, expiry_type, PK(symbol, expiry_date)).

Decimal/types: confirm against the existing `bars` table style before applying. Provide a
downgrade that drops only these four tables.

### 5. Code to scaffold (new modules)
```
core/fno_backfill.py   backfill_futures_bars() · backfill_index_bars() · snapshot_option_chain() · build_expiry_calendar()   (off-hours only)
core/fno_derived.py    compute_realized_vol() (20d rolling c2c → futures_bars) · compute_implied_move() (from option_atm_iv)
ml/fno_vol_gate.py     calibrate_threshold() (realized-vs-implied spread → k, target ~70% pass) · gate_decision(bar) → SELL_PREMIUM | STAND_ASIDE | BUY_PREMIUM   [NEW module — NOT ml/kronos_gate.py]
tests/                 test_fno_backfill.py (mock Dhan; assert no calls in mkt hours) · test_fno_derived.py (numeric checks) · test_fno_vol_gate.py (gate truth table)
```
Invariants: backfill refuses order-path code (historical reads only); tests mock the Dhan
client (no real creds); `gate_decision` is pure given (kronos_vol, implied_move, k);
NIFTY ATM rounding = `round(spot/50)*50`.

### 6. The vol-regime gate (core logic)
```
realized_vol_20d = rolling 20d close-to-close vol of NIFTY futures   (futures_bars)
implied_move     = spot * straddle_iv * sqrt(dte/365)                 (option_atm_iv)
kronos_pred_vol  = Kronos forecast over next horizon                  (see Open Q#4 — use a proxy first)
# calibrate k so ~70% of days pass and passing days have positive (implied - realized). Start k = 0.9.
if   kronos_pred_vol < k * implied_move: SELL_PREMIUM   (iron condor / fly)
elif kronos_pred_vol > implied_move:     BUY_PREMIUM / debit / stand aside
else:                                    STAND_ASIDE
```
VRP context: NIFTY implied>realized ~75% of days, mean +1.2 vol pts, inversion ~25% — the gate
dodges the inversions. Compute the actual spread from TimescaleDB, don't trust the literature.

### 7. Backtest spec (Phase 1 — after data lands)
Daily-step (not intraday-gamma). Per weekly cycle over 2yr: realized_vol_20d from futures;
implied_move at entry; if SELL_PREMIUM build a condor (short ≈ ATM ± 1.5*expected_move, wings
1–2 strikes out), credit from straddle_iv, max_loss = wing_width − credit; resolve over the week;
record entry/credit/max_loss/pnl/win. Aggregate win-rate, profit factor, Sharpe, max DD — **AFTER
costs**. Costs MANDATORY: April-2026 STT hike (options sell-side STT 0.10→0.15%; exercise
0.125→0.15% of intrinsic), brokerage, ≥0.5% slippage on OTM wings; limit/spread orders only,
never market on wings.
**Go/no-go to Phase 2:** positive expectancy AFTER costs AND max DD < 15% of allocated capital.
Negative post-cost → widen wings / cut frequency / raise k. Spread < 0 regime → flip to debit/long-vol.
DD > 20% in paper → halve size before any live consideration.

### 8. Dhan API notes
- DhanHQ v2: `NSE_FNO` supported; historical endpoint serves futures OHLCV + option data.
  **Confirm the historical endpoint returns IV** — if not, derive via Black-76 from option LTP +
  spot + rate, or pull from the option-chain endpoint at snapshot time. **Verify this first.**
- Multi-leg later: basket / Super Orders, submit hedge (long) legs first so SPAN recognizes the hedge.
- Order API ~10/sec, 7000/day — irrelevant for Phase 0 historical reads.

### 9. Suggested PR sequence (small, reviewable; on feat/fno-data-foundation)
1. PR1 — Alembic 009 migration (+downgrade) — schema only.
2. PR2 — `core/fno_backfill.py` + `backfill_index_bars` (NIFTY 50 via security_id=13, India VIX via security_id=21) + `snapshot_option_chain` + tests — data in, off-hours guard.
3. PR3 — `core/fno_derived.py` (realized vol, implied move) + tests.
4. PR4 — `ml/fno_vol_gate.py` calibrate + decision + tests.
5. PR5 — backtest harness + go/no-go report on NIFTY. **Stop and report before Phase 2.**

### 10. Open questions to resolve on the trusted machine (don't guess)
1. Does Dhan's historical endpoint return option **IV** directly, or must we derive it? (PR2 scope.)
2. Front-month vs continuous-futures convention for realized-vol continuity across expiries.
3. Verify NIFTY weekly expiry = **Tuesday** for the full window (rules changed 2024–25; build `expiry_calendar` from actual data).
4. Is Kronos emitting a usable vol/quantile forecast, or only the ORB directional output? (See correction above — currently directional only.)

*Scope = data foundation + gate calibration + backtest. Strategy/live execution is a separate later handoff, gated on the Section 7 go/no-go.*
