# NIFTY Iron-Condor — Phase-1 Backtest Harness & Go/No-Go (status report)

**Status:** harness complete + unit-tested; **the real go/no-go is BLOCKED on Phase-0
data ingestion** (no F&O data exists in TimescaleDB yet). This document describes the
harness, the cost stack, the gate it consumes, and the exact procedure to produce the
real verdict on the trusted machine. See `docs/fno-handoff.md` §6–7.

---

## 1. What is built (this PR)

| Module | Role |
|---|---|
| `ml/fno_vol_gate.py` (PR4) | Vol-regime gate — `gate_decision(realized_vol, implied_vol, k)` → SELL_PREMIUM / STAND_ASIDE / BUY_PREMIUM. Sells premium only when predicted realized vol (persistence proxy = `realized_vol_20d`) < k × implied. |
| `research/backtest/fno_costs.py` | Post-Apr-2026 NIFTY **options** cost stack — sell-side STT 0.15%, exercise STT 0.15% of intrinsic, ₹20/order brokerage, exchange/SEBI/stamp/GST, ≥0.5% slippage. Mirrors the structure of the equity `costs.py`. |
| `research/backtest/fno_condor.py` | Daily-step iron-condor backtest over weekly cycles: Black-76 leg pricing, `build_condor` / `price_condor` / `resolve_condor`, `run_backtest` (after-cost aggregation), `go_no_go`. |

Methodology (handoff §7): one weekly cycle = gate decision at entry → if SELL_PREMIUM,
build an iron condor (shorts ≈ ATM ± `move_mult`×expected_move, wings `wing_strikes`
out), price the credit from `straddle_iv` via Black-76, apply **adverse** slippage to all
four legs, resolve the payoff at the weekly settlement, subtract the full cost stack.
Aggregate **after costs**: win-rate, profit factor, Sharpe (×√52), max drawdown, net P&L,
return-on-capital.

**Go/No-Go criteria** (encoded in `go_no_go`): GO iff
`n_trades ≥ 30` **and** `net_pnl > 0` **and** `profit_factor > 1` **and** `sharpe > 0`
**and** `|max_drawdown| < 15% × capital`. (The handoff names positive after-cost
expectancy + max-DD < 15%; the n_trades≥30 and sharpe>0 guards are added for statistical
sanity — see the QA note in the PR.)

---

## 2. Why there is no real verdict yet

`run_backtest` consumes a list of **cycles** — dicts of
`{entry_date, expiry_date, spot, straddle_iv, dte, realized_vol_20d, expiry_spot}`. Those
fields come from `futures_bars`, `option_atm_iv`, and `expiry_calendar` — tables that
**Alembic 009 created but that hold zero rows** until the Phase-0 ingestion runs. There is
also no historical option IV from Dhan (Open Q#1: option chain is live-snapshot only), so
`option_atm_iv` only accrues going-forward. **A real 2-year backtest therefore cannot run
from this machine** (no creds, no data, off-hours-only).

A `cycles_from_db()` loader is the one remaining wiring step (a documented TODO) — it joins
the three tables per weekly expiry. The `samples_from_db()` join in `ml/fno_vol_gate.py` is
the template for it.

---

## 3. Pipeline smoke test (NOT a result — illustrative only)

Running `run_backtest` on **randomly-generated** synthetic cycles confirms the pipeline
executes end to end and emits a verdict:

```
n_trades=70  win_rate=100.0%  pf=inf  sharpe=110.82  maxDD=₹0  net=₹125,084  roc=62.5%
go_no_go: (True, 'GO — all criteria pass …')
```

> ⚠️ **These numbers are meaningless.** On synthetic data the gate trivially dodges every
> (synthetic) vol-inversion week and the uncorrelated synthetic moves rarely breach the wide
> shorts, so every trade wins. This only demonstrates the code path; it says **nothing** about
> the strategy's real edge. Do not quote it.

---

## 4. How to produce the REAL go/no-go (trusted machine, off-hours)

1. **Ingest data** (PR2, off-hours): `python -m core.fno_backfill --futures --symbol NIFTY
   --security-id <front-month id> --from 2022-06-01 --to <today>`; `--expiry-calendar`;
   begin the daily `--atm-iv` post-close cron (forward-only);
   `--index --security-id 13 --symbol NIFTY --from 2022-06-01 --to <today>` (NIFTY 50 index bars via Dhan charts);
   `--index --security-id 21 --symbol INDIAVIX --from 2022-06-01 --to <today>` (India VIX via Dhan charts — replaces the old `--india-vix <NSE csv>` approach).
   - Resolve **Open Q#2** first: store one **continuous** front-month series under
     `symbol="NIFTY"` (roll-stitched) — multiple raw contracts under one symbol collide on
     the PK and corrupt `realized_vol_20d`.
2. **Derive** (PR3): `compute_realized_vol("NIFTY")`, `compute_implied_move("NIFTY")`.
3. **Build the cycles loader** (`cycles_from_db`, TODO) and run
   `run_backtest(cycles, k=calibrate_threshold(samples_from_db()))`.
4. **Verify-me cost inputs before quoting any verdict:** the NSE options exchange rate
   (`OPTION_EXCHANGE_PCT`, currently ₹35.53/lakh) and the exact post-Apr-2026 STT circular
   numbers (see `fno_costs.py` docstring).
5. Apply the handoff's fallbacks: negative post-cost → widen wings / cut frequency / raise k;
   spread < 0 regime → flip to debit/long-vol; paper DD > 20% → halve size.

**Stop point:** this is the end of the Phase-0 → Phase-1 scope. No strategy/live build until
the real go/no-go (step 3) passes.
