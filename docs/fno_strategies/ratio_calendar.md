# F&O Strategy Spec — Ratio Spread & Calendar/Diagonal

**Branch:** `feat/fno-options-strategies`
**Status:** PLANNING (this doc only — no code written)
**Author:** strategy planner
**Date:** 2026-06-20
**Scope:** Two new builders for the F&O options backtester. One is honestly
backtestable on our historical data; one is **not** and is therefore spec'd as
forward-paper-only.

---

## 0. Engine model & data ground-truth (read first)

The engine contract that every strategy must conform to is:

```
strategy = builder(spot, atm_strike, step, dte, sigma, params) -> list[Leg]
```

A `Leg` carries `(strike, option_type ∈ {CE, PE}, side ∈ {BUY, SELL}, qty_lots)`.
Pricing, cost, and resolution mirror the iron-condor reference
(`research/backtest/fno_condor.py` + `research/backtest/fno_costs.py`):

- **Pricing:** undiscounted Black-76, `F ≈ spot`, `T = dte / 365`, a **single
  annualised `sigma`** for all legs (`black76_call` / `black76_put`). Skew/smile
  are ignored at daily resolution — Phase-0 holds no per-strike historical IV.
- **Costs:** `research.backtest.fno_costs.condor_costs(legs, exercise_intrinsic)`
  — `legs` is a list of `(premium_per_unit, qty_units, side)`. STT is SELL-side
  only on premium; a separate exercise STT applies to ITM intrinsic at expiry.
  `slippage(premium, pct=0.005)` is adverse and applied per leg (shorts fill at
  `mid − slip`, longs at `mid + slip`).
- **Resolution:** intrinsic payoff at `expiry_spot`. Generalise the condor
  payoff: writer keeps premium minus the net option payoff of every leg, scaled
  by lot. `NIFTY_LOT = 65`.
- **Settlement proxy (historical):** NIFTY index daily **close** on expiry_date
  (NSE Final Settlement Price / FSP is the more accurate 15:00–15:30 futures
  VWAP — a known approximation, same caveat as the condor harness).

### The one data fact that decides everything below

We have, **historically**, exactly **one volatility number per trading day**:
`realized_vol_20d` (NIFTY spot) and an implied proxy = **India VIX/100** (or, once
it accrues, a single nearest-expiry `straddle_iv`). We do **NOT** have a
historical **multi-expiry IV term structure** — i.e. we cannot observe, for a past
date, the near-weekly ATM IV *and* the far-monthly ATM IV as two distinct numbers.

- A **single-expiry** strategy (all legs share one expiry, one `sigma`) is
  honestly priceable with Black-76 → **historically backtestable**.
- A **two-expiry** strategy (calendar/diagonal) needs two different IVs at two
  different `T`s. Pricing both expiries off the *same* `sigma` proxy would
  fabricate the term-structure edge the strategy exists to harvest. That is
  **dishonest** and is explicitly forbidden here.

We *do* have a **forward** full option chain (`option_chain_snapshot`, migration
010) captured EOD by `core/fno_collector.py`, with real per-strike LTP/IV across
**all live expiries**. That is the only honest source for calendar pricing → the
calendar is **forward-paper-only**, mirroring `core/fno_paper.py`.

---

## 1. FEASIBILITY MATRIX

| Strategy | Legs | Expiries | Needs term structure? | Single-σ Black-76 honest? | Verdict |
|---|---|---|---|---|---|
| **1×2 Ratio Spread (call)** | buy 1 CE @ K1, sell 2 CE @ K2>K1 | **one** | No | **Yes** | **HISTORICAL-BACKTESTABLE** (Black-76, daily-step) |
| **1×2 Ratio Spread (put)** | buy 1 PE @ K1, sell 2 PE @ K2<K1 | **one** | No | **Yes** | **HISTORICAL-BACKTESTABLE** |
| **Calendar (sell near / buy far, same K)** | sell near CE/PE, buy far CE/PE @ same K | **two** | **Yes** | **No** | **FORWARD-PAPER-ONLY** |
| **Diagonal (sell near / buy far, different K)** | sell near CE/PE @ Ka, buy far CE/PE @ Kb | **two** | **Yes** | **No** | **FORWARD-PAPER-ONLY** |

**Bottom line:** ship the ratio spread into the historical backtest engine now;
ship the calendar/diagonal only into the forward paper-log. Do **not** fabricate
historical calendar P&L with a single sigma.

---

## 2. RATIO SPREAD (1×2) — HISTORICAL-BACKTESTABLE

### 2.1 Definition

A **front-ratio spread**: buy 1 nearer-the-money option, sell 2 further-OTM
options of the **same type and same expiry**. Net entry is a **credit or
near-zero debit**. It profits if the underlying drifts toward the short strike
(max payoff at expiry exactly at K2), keeps the credit if it expires below the
long strike (call variant), and has **partial undefined (naked) risk** beyond the
short strikes because one of the two shorts is uncovered.

This is single-expiry → one `sigma` prices every leg honestly via Black-76.

### 2.2 Legs

**Call ratio (1×2), bearish-to-neutral on the upside:**

| Leg | Type | Side | Strike | Qty |
|---|---|---|---|---|
| Long  | CE | BUY  | `K1` (nearer ATM) | 1 lot |
| Short | CE | SELL | `K2 > K1` (further OTM) | **2 lots** |

`legs = [Leg(K1, CE, BUY, 1), Leg(K2, CE, SELL, 2)]`

**Put ratio (1×2), bullish-to-neutral on the downside (mirror):**

| Leg | Type | Side | Strike | Qty |
|---|---|---|---|---|
| Long  | PE | BUY  | `K1` (nearer ATM) | 1 lot |
| Short | PE | SELL | `K2 < K1` (further OTM) | **2 lots** |

`legs = [Leg(K1, PE, BUY, 1), Leg(K2, PE, SELL, 2)]`

### 2.3 Strike placement (builder)

Anchor to ATM and an OTM offset expressed in implied-move multiples (consistent
with `build_condor`'s `move_mult` convention) so it adapts to the vol regime:

```
em = implied_move(spot, sigma, dte)            # core.fno_derived.implied_move
# call variant
K1 = round_to_step(atm_strike + k1_mult * em)  # long, nearer (k1_mult default 0.0 → ATM)
K2 = round_to_step(atm_strike + k2_mult * em)  # short, further OTM (k2_mult default 1.0)
# put variant: subtract instead of add
K1 = round_to_step(atm_strike - k1_mult * em)
K2 = round_to_step(atm_strike - k2_mult * em)
```

Reuse the condor's `_round_to_step(value, step)` (round-half-up to grid).
Enforce `K2 > K1` (call) / `K2 < K1` (put); if the rounding collapses them to the
same strike (tiny `em` / low vol), **widen K2 by one `step`** so the structure is
always a true ratio (never a degenerate 1×2 at one strike). The builder must
return legs only when the geometry is valid.

### 2.4 Pricing (Black-76, single sigma)

```
T = dte / 365 ;  F = spot
c1 = black76_call(F, K1, T, sigma)             # long premium paid (×1)
c2 = black76_call(F, K2, T, sigma)             # short premium received (×2)
net_credit_per_unit = 2*c2 - c1                # >0 = credit, <0 = debit
```

Apply adverse slippage exactly as the condor does:
- long leg fills at `c1 + slippage(c1)`
- each short leg fills at `c2 - slippage(c2)`

```
net_credit_per_unit_adj = 2*(c2 - slip(c2)) - (c1 + slip(c1))
net_credit_per_lot = net_credit_per_unit_adj * NIFTY_LOT
```

Put variant identical with `black76_put`.

### 2.5 Payoff, breakevens, undefined-risk zone (call variant)

Per unit at `expiry_spot S`, writer perspective (credit `C` = `net_credit_per_unit_adj`):

```
payoff(S) = C + max(S - K1, 0)      # long CE (we own 1)
              - 2 * max(S - K2, 0)  # short CE (we are short 2)
```

Regions:
- **S ≤ K1:** all OTM → `payoff = C` (keep full credit; this is the best zone for a credit ratio).
- **K1 < S ≤ K2:** long CE in the money, shorts still OTM → payoff rises;
  **peak at S = K2**: `payoff_max = C + (K2 - K1)`.
- **S > K2:** the *uncovered* short kicks in. Net delta turns short 1 contract
  (long 1 − short 2 = net short 1 above K2). Payoff falls **linearly and without
  bound** as S rises → **partial undefined risk**.

**Breakevens (call):**
- Lower BE (only if entered for a net **debit**, `C < 0`): `S = K1 - C` (i.e. `K1 + |C|... ` — solve `C + (S-K1) = 0`). For a credit (`C ≥ 0`) there is **no lower breakeven** — every S ≤ K2 region is ≥ 0 once `S ≥ K1 - C`; with a credit the position is already ≥ 0 at S ≤ K1.
- **Upper BE (the one that matters):** set payoff = 0 in the `S > K2` region:
  `C + (S - K1) - 2(S - K2) = 0  →  S_BE_upper = 2*K2 - K1 + C`.
  **Above `S_BE_upper` the position loses money, unbounded.**

**Put variant** is the mirror image (reflect through ATM; undefined risk to the
**downside** below K2, floored only by `S → 0` so it is large-but-bounded in
practice; treat as undefined for risk purposes).

### 2.6 Undefined-risk treatment + required risk stop

Because one short is naked, max loss is unbounded (call) / floor-at-zero-spot
(put). The backtest must **not** report an unbounded `max_loss`.

> ⚠️ **IMPLEMENTATION STATUS:** The current historical `run_strategy_backtest`
> in `fno_strategies.py` is **EXPIRY-ONLY** — it holds every trade to expiry with
> no intra-cycle path stop. The mandatory risk-stop described below is a
> **forward/live-paper feature and a future historical runner extension — it is
> NOT implemented in the historical backtest.** Ratio-spread GO verdicts from the
> historical runner are therefore **tail-blind and diagnostic-only**; the
> unlimited-loss leg is unrestricted in the current engine.

The target stop design (for forward paper and future path-stop extension) applies a **risk stop**
mirroring the naked-short treatment:

- **Stop rule:** close the position when the underlying breaches the short
  strike by a configurable buffer, i.e. when, at any **daily step** between entry
  and expiry, the index close crosses **`stop_level`**:
  - call: `stop_level = K2 + stop_mult * em` (default `stop_mult = 1.0`)
  - put:  `stop_level = K2 - stop_mult * em`
- **Stop fill:** the position's loss at the stop is computed by **re-pricing all
  legs with Black-76 at the stop step** (spot = that day's close, remaining `dte`,
  same `sigma`) — not the expiry intrinsic — because we exit early. This is the
  honest mark-to-model exit given we only have a daily sigma series.
  Conservatively add slippage on the closing legs (buy back 2 shorts at
  `+slip`, sell the long at `−slip`).
- **Reported `max_loss`** for sizing/`go_no_go` = the larger of the modelled
  stop-loss magnitude and a hard cap; never `inf`.

> **Daily-step caveat (be honest):** with daily bars the index can gap *through*
> `stop_level` overnight. The realised stop loss is therefore **optimistic** —
> intraday/gap fills would be worse. State this in the report. This is the same
> class of caveat the condor harness documents for entry/settlement.

### 2.7 SPAN-margin proxy for the extra short

The defined-risk condor needed no margin model (wing-capped). The ratio has a
**naked short** (1 net uncovered contract above/below K2), so capital usage is
**SPAN+exposure margin**, not max-loss. We have no live SPAN engine historically,
so use a documented proxy for the **single uncovered short**:

```
span_margin_per_lot ≈ max(
    span_pct  * spot * NIFTY_LOT,        # ~ NSE SPAN scan range proxy
    expo_pct  * spot * NIFTY_LOT         # exposure margin floor
) + short_premium_received_per_lot       # premium adds to blocked margin
```

Defaults (Params, flagged `[verify-me]` against the live `fno_instruments`
margin columns — `mtf_leverage`, circuits — and a real SPAN file before any go):
- `span_pct = 0.10` (≈ index-option scan-range order of magnitude)
- `expo_pct = 0.02`

Only **one** uncovered short is margined (the other short is covered by the long
CE/PE). Use `span_margin_per_lot` for return-on-capital and to size positions; do
**not** reuse the condor's `wing_width − credit` formula — it is invalid here.

> The cleanest future fix is to read SPAN from `fno_instruments` / a NSE SPAN file
> at runtime; the proxy is a Phase-0 placeholder, explicitly conservative-ish but
> **not** authoritative. A GO on the ratio must be re-validated with real SPAN.

### 2.8 Vol-gate condition

The ratio is **net short vega / short gamma** (two shorts vs one long) → it is a
**premium-selling** structure. Gate it with the existing VRP gate:

```
gate_decision(realized_vol_20d, sigma, k) == SELL_PREMIUM   # ml.fno_vol_gate
```

Only enter on `SELL_PREMIUM` (implied rich vs realized). On `BUY_PREMIUM` /
`STAND_ASIDE`, skip the cycle — identical pattern to `run_backtest` /
`record_paper_entry`. (A **back-ratio**, buy 2 / sell 1, would be the
`BUY_PREMIUM` long-vol variant — out of scope here; note it as a future builder.)

### 2.9 Params (builder + runner)

| Param | Default | Meaning |
|---|---|---|
| `variant` | `"call"` | `"call"` or `"put"` |
| `k1_mult` | `0.0` | long strike offset in implied-move units from ATM |
| `k2_mult` | `1.0` | short strike offset (must place K2 further OTM than K1) |
| `ratio` | `(1, 2)` | (long lots, short lots); only 1×2 supported in v1 |
| `step` | `50` | NIFTY strike grid |
| `move_mult` (alias) | — | not used directly; offsets are k1/k2_mult |
| `stop_mult` | `1.0` | risk-stop buffer beyond K2 in implied-move units |
| `slippage_pct` | `0.005` | adverse per-leg (from `fno_costs.slippage`) |
| `k` (gate) | `DEFAULT_K = 0.9` | VRP SELL_PREMIUM threshold |
| `span_pct` | `0.10` `[verify-me]` | SPAN scan-range proxy for the naked short (only 1 uncovered leg; two-sided straddle/strangle uses 0.12 in the engine as the mid of 10–15%; ratio uses 0.10 as only one net short is uncovered — both are tunable approximations) |
| `expo_pct` | `0.02` `[verify-me]` | exposure-margin floor |
| `lot` | `NIFTY_LOT = 65` | contract size |
| `capital` | `200_000` | allocated ₹ for RoC / drawdown |

### 2.10 Unit-test cases (pure builder/pricing/payoff — no DB)

Author these against the builder + a `price_ratio` / `resolve_ratio` pair that
parallels `price_condor` / `resolve_condor`. All deterministic, math-only.

1. **Builder geometry (call):** `spot=22000, atm=22000, step=50, sigma=0.12,
   dte=7, k1_mult=0, k2_mult=1`. Assert exactly 2 legs; `K1 == 22000`;
   `K2 > K1` and `K2` on the 50-grid; long qty 1, short qty 2, types CE.
2. **Builder geometry (put mirror):** same inputs, `variant="put"`. Assert
   `K2 < K1`, both PE, qty 1/2. K1/K2 are the reflection of case 1 about ATM.
3. **Degenerate-strike guard:** tiny `em` (`sigma=0.02, dte=1`) so K1 and K2
   round equal → assert builder widens K2 by one `step` (never returns a 1×2 at
   one strike).
4. **Net credit sign:** with a realistic OTM gap the structure is a **net
   credit** → assert `net_credit_per_unit_adj > 0` (2·c2 received vs 1·c1 paid,
   after slippage). Use a hand-checkable Black-76 set.
5. **Payoff peak at K2:** `resolve_ratio` at `expiry_spot == K2` returns
   `credit + (K2 - K1)` per unit (×lot) — the maximum payoff. Check ±epsilon.
6. **Full credit below K1:** `expiry_spot <= K1` → payoff == credit (all legs
   OTM). And `expiry_spot == upper_BE = 2*K2 - K1 + credit` → payoff ≈ 0.
7. **Undefined-risk slope:** for two spots above `upper_BE`, assert payoff is
   negative and **decreases by ~1×lot per +1 point of spot** (net short one
   contract above K2) — verifies the unbounded-loss leg.
8. **Risk-stop re-pricing:** simulate the index reaching `stop_level` with
   `remaining_dte > 0`; assert the stop loss equals the Black-76 mark-to-model
   exit (legs re-priced at stop spot/dte/sigma + closing slippage), and that the
   reported `max_loss` is finite (never `inf`) and ≥ the stop loss.

(Optionally also: cost-stack test — `condor_costs` on the 3 executed legs
charges SELL-side STT on both shorts, BUY stamp on the long, brokerage ×3.)

---

## 3. CALENDAR / DIAGONAL — FORWARD-PAPER-ONLY

### 3.1 Why it is NOT in the historical backtest (the honest note)

A calendar/diagonal **sells a near-expiry option and buys a far-expiry option**.
Its entire edge is the **term structure of implied volatility** plus the faster
theta decay of the near leg. Pricing it requires, on a single date, **two
different implied vols** — `sigma_near` at `T_near` and `sigma_far` at `T_far` —
because the near and far options trade at materially different IVs (front weeklies
routinely 2–8 vol points above/below the next month depending on the regime and
event calendar).

**Our historical data has exactly one vol number per day** (`realized_vol_20d`
and a VIX/100 implied proxy, or at most one nearest-expiry `straddle_iv`). There
is **no historical multi-expiry IV term structure** in the schema (migration
010's `option_chain_snapshot` is **forward-only** — captured EOD going forward;
it has no back-history; and `expiry_calendar` is forward-only too, as the condor
loader documents).

Therefore, if we priced both legs of a calendar with the **same** `sigma`:

```
near = black76(F, K, T_near, sigma)
far  = black76(F, K, T_far,  sigma)      # SAME sigma — WRONG
```

the only difference between the legs would be `T` (calendar-time), so the model
would attribute the entire calendar P&L to time decay at one flat vol — it would
**manufacture** a term-structure result that the data cannot support and that
could be arbitrarily wrong in sign. **This is exactly the kind of fabricated
number this spec forbids.** A single-sigma calendar backtest is not
"approximate"; it is **structurally unable to represent the strategy's edge**.

> **Rule:** Do **not** add a calendar/diagonal to the historical `run_backtest`
> path. Do **not** fake `sigma_far` from `sigma_near` (e.g. a fixed term-premium
> add-on) — that injects an unfalsifiable assumption that *is* the result.
> Report **no historical numbers** for the calendar.

### 3.2 What it requires to be honest

Two **independently observed** implied vols at two expiries on the same timestamp:
`(sigma_near @ T_near)` and `(sigma_far @ T_far)`, ideally real per-strike LTPs.
We have this **only forward**, via `option_chain_snapshot`, which already captures
the **full chain across all live expiries** every EOD.

### 3.3 Forward-paper design (mirror `core/fno_paper.py`)

Implement a `record_calendar_paper_entry()` / `resolve_calendar_paper_trades()`
pair that mirrors the existing condor paper functions, but spanning **two
expiries** read from the **real chain** — no Black-76, no sigma proxy for entry.

**Entry (EOD, once per cycle):**
1. From `option_chain_snapshot` (`underlying_scrip = 13`), find the **two nearest
   future expiries** `expiry_near < expiry_far` (the next two weeklies, or weekly
   + next-month for a longer calendar — make it a Param).
2. Take the latest `snapshot_time` for each expiry; load the chains.
3. Choose strike `K` at ATM (calendar) — `nifty_atm_strike(spot, step)`. For a
   **diagonal**, `K_far` is offset OTM by a Param (`diagonal_offset_strikes`).
4. Read **REAL LTPs** for the legs (nearest available strike if exact missing,
   same `_nearest_strike` helper; **bail if any leg LTP is missing/≤0**, exactly
   like `record_paper_entry`):
   - **Calendar (call):** SELL near CE @ K, BUY far CE @ K (put variant mirrors).
   - **Diagonal:** SELL near CE @ K_near, BUY far CE @ K_far.
5. `net_debit_per_unit = far_ltp - near_ltp` (calendars are typically a **net
   debit** — the far leg costs more; the position's "max loss" is the net debit
   paid, *if held to near-expiry without rolling*; gains come from near-leg decay
   + a far-IV that holds up).
6. Entry costs via `condor_costs` on the 2 executed legs (1 SELL + 1 BUY).
7. **Gate:** record the regime label for analysis. A **long-calendar** is long
   vega (net long the far leg's vega) → conceptually a `BUY_PREMIUM` / long-vol
   tilt; but its dominant driver is term structure + theta, so do **not**
   hard-gate it on `SELL_PREMIUM`. Suggest: enter on `STAND_ASIDE` or
   `BUY_PREMIUM`, log the label, and let the forward track reveal which regime
   pays. (Make the entry-regime filter a Param so it is honest and tunable.)
8. INSERT into a **new** table `fno_calendar_paper_trades` (do not overload the
   condor table — it has condor-specific columns). `ON CONFLICT DO NOTHING` on a
   `(symbol, expiry_near, expiry_far, strike, structure)` unique key so each
   cycle is entered once.

**Resolution (at near-expiry):**
- The near short expires/settles; the far leg is **still open**. The honest
  resolution is **two-stage** and is the key difference from the condor:
  1. At `expiry_near`: settle the near short at index settlement proxy (close;
     FSP TBD — same caveat). The **far leg cannot be resolved by intrinsic** —
     it still has time value, so it must be **marked at its real LTP** from the
     `option_chain_snapshot` captured on/after `expiry_near` (the chain still
     carries the far expiry). Net P&L at near-expiry =
     `(near_credit_settled) + (far_ltp_now − far_ltp_entry) − costs`.
  2. The position is then either **closed** (sell the far leg at its real LTP,
     book P&L) or, for a calendar that **rolls**, opens a new near short — but a
     v1 paper log should **close at near-expiry** (mark far at real LTP) to keep
     accounting unambiguous and fully data-backed.
- Because the far leg is marked from **observed LTPs**, no sigma is invented at
  any point. If the far-leg LTP is unavailable at resolution, **defer** the row
  (do not mark-to-model) — same defensive pattern as `resolve_paper_trades`.

**New table sketch (migration 012, forward-only):**
`fno_calendar_paper_trades(id, symbol, structure ∈ {CALENDAR,DIAGONAL},
opt_type, entry_date, expiry_near, expiry_far, strike_near, strike_far, lot,
spot_entry, near_ltp_entry, far_ltp_entry, net_debit, entry_costs,
gate_label, k, status, near_settle_spot, far_ltp_exit, gross_pnl, exit_costs,
net_pnl, win, resolved_at, raw JSONB, UNIQUE(symbol, expiry_near, expiry_far,
strike_near, structure))`.

### 3.4 Calendar Params (forward-paper)

| Param | Default | Meaning |
|---|---|---|
| `structure` | `"calendar"` | `"calendar"` (same K) or `"diagonal"` (offset K_far) |
| `opt_type` | `"CE"` | `CE` or `PE` |
| `near_rank` | `0` | which future expiry is the short (0 = nearest) |
| `far_rank` | `1` | which future expiry is the long (1 = next; 4 ≈ next month) |
| `diagonal_offset_strikes` | `0` | strikes to offset `K_far` (only for diagonal) |
| `step` | `50` | strike grid |
| `entry_regime_filter` | `{"STAND_ASIDE","BUY_PREMIUM"}` | which gate labels permit entry (log-only, tunable) |
| `roll` | `False` | v1: close at near-expiry (mark far at real LTP); rolling deferred |
| `lot` | `NIFTY_LOT = 65` | contract size |

### 3.5 What we will (and will not) report

- **Will:** a forward, real-IV, two-expiry paper track once
  `option_chain_snapshot` accrues enough cycles — analogous to the condor forward
  log, validating term-structure edge with **observed** prices.
- **Will NOT:** any historical Sharpe / win-rate / P&L for the calendar. There is
  no honest historical number to report. State plainly in any go/no-go that the
  calendar is **forward-pending**, not backtested.

---

## 4. Summary of deliverables (for the implementer, later PRs)

1. **Ratio spread builder** (`build_ratio`), pricer (`price_ratio`), resolver
   (`resolve_ratio`), risk-stop logic, SPAN proxy, and wiring into the historical
   `run_backtest` path with its own `RatioTrade` record + `go_no_go`. 8 unit
   tests per §2.10. **Backtestable now.**
2. **Calendar/diagonal forward paper** (`record_calendar_paper_entry`,
   `resolve_calendar_paper_trades`, `calendar_paper_summary`) + migration 012
   table, mirroring `core/fno_paper.py`. **No historical backtest.** Tests use
   mocked chain rows (two expiries) — never a single sigma.
3. Both builders conform to `builder(spot, atm_strike, step, dte, sigma, params)
   -> list[Leg]`; the calendar builder is used only by the paper path (it returns
   the two-expiry leg shape, but pricing comes from the real chain, not Black-76).
