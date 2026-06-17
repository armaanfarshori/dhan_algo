# Backtesting Framework (M3) — Architecture

Extends the existing event-driven backtester in `research/backtest/` (it is NOT a
greenfield). The correctness bones of the original are preserved verbatim; this
doc records the generalization for the M3 decision study.

## Preserved correctness properties (regressions = fail)
- **Same strategy code as live** — instantiates `strategies/orb.py`, no backtest fork.
- **No look-ahead** — a decision on bar *i* (its close/high/low) fills at bar *i+1*'s
  **open** + adverse slippage; a last-bar decision fills at that bar's close.
  (`test_next_bar_fill_no_lookahead`, `test_gate_receives_only_past_bars`.)
- **Full Indian intraday cost stack** (`costs.py`) deducted from every round-trip.
- **Sizing via the live `engine.risk.RiskEngine`** — identical stop-distance math.
- **Honest Sharpe** — daily net P&L / starting equity × √252 (never per-minute).
- **Pluggable gate** — `gate_fn(sid, side, bars_so_far)`; reuses `KronosSignalEngine`.

## The core generalization — portfolio-level replay
The original replays each (security, day) **independently**, each with its own
`RiskEngine(equity_base=params.equity)`, then **sums** per-security P&L. That is
**unrealizable**: it implies infinite capital and taking every signal across ~1,700
names at once. The framework instead mirrors the **live** architecture
(`apps/trader.py`: N runners share ONE `RiskEngine` + ONE `Portfolio`):

- **One shared portfolio state per run** (in-memory, NOT DB-persisted — a 2y×1700
  backtest would be millions of `engine_positions` writes). Tracks: cash/equity,
  open positions, per-position committed risk, realized P&L (all-time + today).
- **One `RiskEngine`** reused for the *math* and *constraints*: `size_position`,
  `position_risk`, `register_risk`/`release_risk`/`committed_risk`,
  `daily_loss_budget`, `risk_budget_per_trade`. Its `_realized_total`/`_realized_today`
  are driven from the in-memory portfolio each step (no `refresh_pnl` DB call).
- **Time-merged bar stream** — all universe securities' 1m bars for a day, processed
  in timestamp order, so entries compete for shared capital at the moment they fire.
- **Constraints enforced during replay** (exactly the live caps):
  - concurrent-position cap (`max_open_positions`),
  - per-trade + aggregate risk budget (`committed_risk ≤ daily_loss_budget`),
  - `max_orders_per_session`,
  - **daily-loss kill-switch**: realized-today loss ≥ `daily_loss_budget` ⇒ halt the
    day — flatten all + block new entries (the live RiskEngine behaviour).

Equity **compounds** across days (realized P&L feeds `RiskEngine.equity`), so sizing
shrinks in drawdown exactly as live.

## Fidelity fixes (from the audit)
1. **Survivorship — label-as-ceiling.** Universe derives from the *current* scrip
   master (`dhan_clean.clean_universe`); delisted/suspended names are absent. A
   point-in-time master needs historical scrip masters + delisted OHLCV we do NOT
   have from the feed. So every result is labelled an **optimistic upper bound** in
   report output + docs; reconstruction is out of scope (documented, not silent).
2. **Slippage model + stress** — keep flat-bps default but add a price/tick-relative
   option and a stress multiplier (re-run best variant at ≥4 bps).
3. **Volume floor → 50k** — `dhan_clean` is already built at ≥50k avg volume, so the
   pool is pre-filtered; the param defaults to 50k for honesty (no candidate change).
4. **Partial fills** — cap fill qty at a % of the fill-bar's volume (size optimism).
5. **IS/OOS split** — `--split-date`; report IS and OOS KPIs separately.

## Reporting / reproducibility
- Full KPI panel (see `results/M3-RESULTS-TEMPLATE.md`), IS vs OOS, concentration,
  gate diagnostics (reuse `ml/calibration.py` `gate_value_summary`), slippage stress.
- Result JSON embeds the **git SHA + full param set**; any randomness seeded.
- Deterministic: identical inputs ⇒ identical outputs.

## Data source
Runs against **`dhan_clean`** via `config.backtest_db_url` (`research/backtest`
already wired). `universe.py` derives the point-in-time universe from
`clean_universe` membership + 1m-derived daily ATR (no `instruments`/`1d` — the
clean replica has neither).

## Build order (slices)
- **(a) portfolio-level replay** — shared capital + concurrent cap + daily kill-switch. *(critical correctness)*
- **(b) IS/OOS `--split-date` + reproducibility** (git SHA/params in JSON).
- **(c) slippage model + stress + partial fills.**
- **(d) KPI panel + fill `M3-RESULTS-TEMPLATE.md` + fix `Backtesting.md` survivorship wording.**
- Deferred: `BaseStrategy` multi-strategy generalization (M3 needs only ORB).

## Scope decisions (confirmed 2026-06-17)
- Extend in place (not greenfield).
- Branch `feat/backtest-framework` off `main` (the M2.5/S3/infra branches are merged).
- Survivorship = label-as-ceiling.
- ORB-only + portfolio-sim first; `BaseStrategy` later.
