# NIFTY Iron-Condor — Phase-1 Backtest Harness & Go/No-Go (status report)

> **Phase-0a fidelity update (2026-06-20, branch `feat/fno-backtest-fidelity`).**
> Four fidelity bugs in the harness were fixed; each is feature-flagged by data
> availability and changes results as noted. **The headline §0 GO numbers above
> were produced by the PRE-fix harness and must be RE-RUN on a box with DB access
> before being quoted again** — they are superseded pending that re-run.
>
> 1. **Tuesday expiry (fix #1).** NIFTY weekly expiry moved Thursday→Tuesday at the
>    project-convention cutover `2026-09-01` (real-life 2025-09-01; the repo runs
>    +1yr ahead). `cycles_from_db(mode="weekly")` now snaps each ISO week to its
>    real expiry weekday (Thu pre-cutover / Tue on/after) with a holiday roll-back
>    to the prior trading day, instead of "last trading day of the ISO week".
>    *Result impact:* shifts entry/expiry/settlement dates for many cycles → spot,
>    IV, DTE and settlement all move slightly; net edge can move either way. Most
>    historical cycles are pre-cutover (Thursday) so the change is modest, but the
>    Fri-floating boundaries of holiday/short weeks are corrected.
> 2. **Day-count (fix #2).** The vol-gate compared a √252-annualised
>    `realized_vol_20d` against a √365-annualised implied vol (VIX/100) — a
>    cross-basis mix biasing the realized/implied ratio by √(365/252)≈1.204 (~20%).
>    The gate now rebases realized vol onto the calendar (365) basis
>    (`realized_vol_to_calendar_basis`) before comparing, so both sides share ONE
>    convention. Black-76 pricing (calendar IV × T=dte/365) was already consistent
>    and is unchanged. *Result impact:* the rebased realized vol is ~17% LOWER, so
>    `realized < k·implied` fires MORE often → MORE SELL cycles at a given `k`. **`k`
>    should be re-calibrated on calendar-basis ratios; the old k≈0.898 is no longer
>    apples-to-apples.**
> 3. **Real ATM IV preference (fix #3).** When a cycle carries a real per-expiry
>    ATM IV (`atm_straddle_iv`, from `option_atm_iv.straddle_iv` /
>    `option_chain_snapshot`) it is used for pricing/settlement in preference to the
>    VIX proxy; the VIX/100 proxy remains the fallback for the historical window
>    with no forward IV. The IV source is logged per cycle and counted
>    (`n_real_iv` / `n_vix_proxy_iv`). *Result impact:* none on the current
>    historical run (no real IV accrued yet → all `vix_proxy`); takes effect as the
>    forward collector accrues real IV, at which point the calm-regime optimistic
>    bias is removed for those cycles.
> 4. **FSP settlement (fix #4).** Expiry resolution prefers NSE Final Settlement
>    Price (`fsp`), then a last-half-hour VWAP proxy (`expiry_halfhour_vwap`), then
>    the bar close (`expiry_spot`, least faithful). Source is recorded + counted
>    (`n_official_fsp` / `n_proxy_settlement`). When real FSP is not used, `go_no_go`
>    now appends a `FIDELITY:` disclaimer flagging the residual close-vs-FSP and
>    proxy-IV bias (the GO/NO-GO criteria themselves are unchanged). *Result impact:*
>    none on the current run (no FSP collected → all `close_proxy`); the residual
>    limitation is now explicit in the verdict string.

**Status (2026-06-19): FIRST REAL BACKTEST RUN — preliminary GO.** Data ingested into
`dhan_trading` (NIFTY 50 + India VIX index bars via Dhan charts, detailed scrip master,
expiry calendar). Realized vol computed; VIX used as the historical implied baseline.

## 0. Headline result (after costs)

NIFTY weekly iron condor, 2022-01 → 2026-06, synthetic ISO-week cycles, gate k=0.898
(calibrated; VRP pass-rate ~70%, India VIX > NIFTY realized on 100% of SELL days, mean
edge +3.8 vol pts). 164 trades after the gate, ₹2,00,000 capital:

| `move_mult` | trades | win% | profit factor | Sharpe | max DD | net P&L | ROC | verdict |
|---|---|---|---|---|---|---|---|---|
| 1.0 | 164 | 78.7% | 1.29 | 0.78 | −₹22,550 | +₹44,302 | 22.2% | **GO** |
| **1.5** (handoff default) | 164 | 90.9% | 1.74 | 1.22 | −₹8,451 | +₹36,420 | 18.2% | **GO** |

**Read this as a PRELIMINARY GO** — positive expectancy after costs with max DD < 15% of
capital under **preliminary assumptions whose bias directions are MIXED, not uniformly
conservative** (see §5 — notably the VIX-as-weekly-IV proxy is likely *optimistic* in calm
regimes). It clears the Phase-0→Phase-2 gate to *plan*, but is **not live-ready** and the real
edge could be lower: re-validate with NSE final settlement prices + real per-expiry ATM IV
(now accruing via the collector) — and ideally a NIFTY-trained Kronos vol — first.

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

## 2. Data source (RESOLVED — ingestion done 2026-06-19)

`run_backtest` consumes a list of **cycles** — dicts of
`{entry_date, expiry_date, spot, straddle_iv, dte, realized_vol_20d, expiry_spot}`.

Realized vol comes from the **NIFTY 50 spot index** (`index_bars`, IDX_I id 13, ~7yr) and
the implied baseline from **India VIX** (`index_bars`, id 21, ~4yr) — both ingested via Dhan
charts. Cycles are assembled by `cycles_from_db(mode="weekly")`, which derives **synthetic
ISO-week boundaries** from the index trading calendar. This is required because Dhan's expiry
endpoint is **forward-only** (`expiry_calendar` holds no historical expiries), and Dhan exposes
**no historical option IV** (Open Q#1) — so per-expiry ATM IV (`option_atm_iv`) and the full
`option_chain_snapshot` only accrue going-forward (a post-close cron). `cycles_from_db(
mode="expiry_calendar")` is the forward/live path for when historical expiries are recorded.

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

## 4. How the §0 result was produced (and how to reproduce/extend)

Run off-hours on a box with DB access + the cached Dhan token (read-only; never mint a
session). The §0 result was produced exactly this way:
1. `alembic upgrade head` (applies 009+010 to `dhan_trading`).
2. Ingest: `sync_fno_instruments()`; `backfill_index_bars(c, "13", "NIFTY", "2018-01-01", today)`;
   `backfill_index_bars(c, "21", "INDIAVIX", "2022-01-01", today)`; `build_expiry_calendar(c, "NIFTY")`;
   one `snapshot_option_chain(c, "NIFTY")` to start the forward IV record.
   (`backfill_index_bars` / `build_expiry_calendar` / `snapshot_option_chain` are **async** — run
   them inside `asyncio.run(...)` with a `DhanClient` built from the cached token;
   `sync_fno_instruments` + `compute_index_realized_vol` are sync.)
3. `compute_index_realized_vol("13")` → fills `index_bars.realized_vol_20d`.
4. `k = calibrate_threshold(samples_from_db(source="vix"))`;
   `run_backtest(cycles_from_db("NIFTY", mode="weekly"), k=k, move_mult=1.5)`.

**Before any live consideration (the §5 caveats are why §0 is only *preliminary*):**
- Replace VIX with **real per-expiry ATM IV** (`option_chain_snapshot`, once enough forward
  history accrues — or Black-76-derived from currently-listed option contracts).
- Use **NSE Final Settlement Price** (15:00–15:30 weighted avg) for `expiry_spot`, not the close.
- Model **next-morning entry** rather than prior-expiry close.
- Verify-me cost inputs: `OPTION_EXCHANGE_PCT` (₹35.53/lakh) + exact post-Apr-2026 STT circular.
- Apply the handoff fallbacks: negative post-cost → widen wings / cut frequency / raise k;
  spread < 0 regime → flip to debit/long-vol; paper DD > 20% → halve size.

**Stop point:** this is the end of Phase-0 → Phase-1 scope. The preliminary GO clears the gate
to *plan* Phase-2, but no strategy/live build until the caveats above are addressed.

---

## 5. Backtest fidelity caveats

The VIX-based first-pass go/no-go uses several approximations. Readers must understand these
before drawing any conclusions from a GO or NO-GO verdict.

**(a) India VIX (30-day) as weekly straddle IV — bias direction is REGIME-DEPENDENT (corrected
2026-06-19 by live data).**
`straddle_iv = India VIX close / 100` is a 30-day implied vol used in place of the true weekly
(≈ 4–7 DTE) ATM straddle IV. We initially assumed weekly IV trades *above* 30-day VIX (so VIX
would understate it → conservative). **The first live chain snapshot refutes this**: on a calm
day the real 4-DTE ATM IV was **9.89%** vs **India VIX 13.19%** (real/VIX = **0.75×**) — i.e. in
calm / normal-contango regimes the short tenor sits *below* VIX, so VIX **OVER**states weekly IV
→ the backtest placed shorts *too wide* and booked *too much* credit → **optimistic** in those
regimes. VIX only *understates* weekly IV in stressed/backwardated regimes. So the direction of
this bias is **not fixed and not reliably conservative** — it flips with the vol term-structure
regime, and the real-IV revalidation (workstream A) could move the result **either way**.

**(b) Settlement = index daily CLOSE, not NSE FSP.**
NSE's official Final Settlement Price (FSP) is the 30-minute volume-weighted average of NIFTY
futures from 15:00–15:30 IST on expiry day.  OHLC "close" is the last tick and can differ from
the FSP by several index points (occasionally 20–50 pts on volatile days).  Near-the-money
expiries on volatile weeks may be mis-classified as a winning or losing cycle.  Direction:
**ambiguous** (could inflate or deflate win rate depending on the specific week).

**(c) Entry at prior-expiry CLOSE (idealized).**
Each cycle's `spot` and `straddle_iv` are taken from the CLOSE of the prior expiry date (E_i),
not the following morning's open.  A real trader enters Monday morning (09:30–10:00 IST); the
overnight gap can be 50–200 pts on volatile weekends.  Direction: **slightly optimistic** (entry
close is generally smoother than next-morning open).

**(d) Realized vol = 20-day trailing vs VIX ≈ 30-day horizon.**
The vol-regime gate compares `realized_vol_20d` (20-calendar-day backward) against
`straddle_iv` (VIX, a 30-day forward measure).  These measure different horizons.  A more
faithful comparison would use a 30-day realised vol or a forward-estimate.  Direction:
**indeterminate** (the 20d/30d mismatch can cause the gate to fire slightly more or less
frequently than the true vol-regime would warrant).

**(e) move_mult default is now 1.5.**
Short strikes are placed at ± 1.5 × expected_move (per handoff §7).  Earlier prototypes used
1.0 ×.  The wider placement at 1.5 × reduces the credit collected but also reduces breach
frequency; it is the intended production default.  Any result produced with `move_mult=1.0`
is *not* comparable to one produced with 1.5 without re-running.

**(f) Sharpe is on absolute ₹ P&L (scale-dependent).**
The `sharpe` metric in `go_no_go` is computed as `mean_pnl / std_pnl × √52`, where P&L is
in raw rupees for the fixed capital allocation (default ₹2,00,000).  This makes the Sharpe
scale-dependent: doubling the position size doubles both mean and std, leaving Sharpe
unchanged.  However, comparisons across different `capital` values or lot sizes are
meaningless.  The metric is only informative within a single, consistent parameterisation.

**(g) Cycle boundaries are synthetic ISO-weeks, not actual expiries.**
`mode="weekly"` uses the last trading day of each ISO week as the cycle boundary (Dhan's
expiry list is forward-only, so historical expiry dates aren't available). This is a faithful
~weekly cadence, but it does not align exactly to the real NIFTY weekly expiry day (which
itself changed weekday over 2024–25), and a rare fully-closed ISO week would yield a >7-day
("multi-week") cycle treated like a normal one. Negligible for a regime-level screen; exact
expiry alignment comes once per-expiry data accrues forward (`mode="expiry_calendar"`).

### Net interpretation

The bias directions are MIXED, not uniformly conservative (this is a correction to an earlier
claim): the VIX-vs-weekly-IV bias is **regime-dependent** — *optimistic* in calm/contango
regimes (real 4-DTE IV < VIX, per the 2026-06-19 snapshot: 0.75×) and only conservative in
stress/backwardation; the daily-close-vs-FSP settlement can flip near-the-money weeks either
way; entry-at-prior-close is mildly optimistic. Therefore:

- A **GO** verdict from this loader is **PRELIMINARY ONLY — not assumed conservative**. Because the
  dominant IV bias can be *optimistic* in calm regimes (which 2022–24 largely were), the real edge
  could be **lower** than the headline. It MUST be re-validated with real per-expiry ATM IV
  (workstream A — the collector is now accruing it) and NSE FSP settlement before any live
  consideration; treat the result as "worth validating," not "validated."
- A **NO-GO** verdict is *solid* — if the strategy cannot clear these conservative hurdles, it
  will not improve with more realistic data.
