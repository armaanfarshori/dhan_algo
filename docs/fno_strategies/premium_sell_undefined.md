# Premium-Selling Strategies with UNDEFINED / Partial Risk

**Status:** PLAN — spec only, no code. Branch `feat/fno-options-strategies`.
**Scope:** NIFTY weekly index options, daily-step single-expiry backtest.
**Last updated:** 2026-06-20

This document specs three SELL-side option strategies whose risk is **not fully
defined** by long wings:

1. **Short Straddle** — undefined risk on both sides.
2. **Short Strangle** — undefined risk on both sides, wider safe zone.
3. **Jade Lizard** — partial defined risk (no upside risk; undefined downside).

It is the sibling of the already-shipped iron condor
(`research/backtest/fno_condor.py`), which is fully defined-risk. The condor's
`max_loss = wing_width − credit` does **not** apply here: a naked short option's
loss is unbounded, so a backtest cannot use `max_loss` for sizing/return. The
crux of this spec is therefore the **SPAN-margin approximation** and a
**mandatory risk-exit (stop)** — without both, an undefined-risk backtest is
meaningless and dangerous.

---

## 0. Engine model (shared contract)

All three strategies plug into the same engine signature the brief defines:

```python
def builder(spot, atm_strike, step, dte, sigma, params) -> list[Leg]
```

where:

```python
@dataclass(frozen=True)
class Leg:
    option_type: str   # "CE" | "PE"
    side: str          # "SELL" | "BUY"
    strike: int        # rounded to step grid
    qty_lots: int      # >= 1
```

Conventions reused from the condor harness (do NOT re-derive):

- **Pricing:** Black-76 undiscounted, `F ≈ spot`. Use the existing
  `research.backtest.fno_condor.black76_call / black76_put`. One `sigma`
  (= `straddle_iv`) for all legs; skew ignored at daily resolution.
- **Strike rounding:** round-half-UP to `step` via the existing
  `_round_to_step` (NIFTY `step = 50`). ATM = `nifty_atm_strike(spot, step)`.
- **Implied move (points):** `core.fno_derived.implied_move(spot, sigma, dte)
  = spot · sigma · √(dte/365)`.
- **Time:** `T = dte / 365.0`.
- **Lot:** `NIFTY_LOT = 65` from `research.backtest.fno_costs`.
- **Costs:** reuse `research.backtest.fno_costs.condor_costs(legs, ...)` and
  `slippage(premium)`. It is leg-count agnostic — pass whatever legs each
  strategy executes. SELL-side STT is on premium; exercise STT on ITM
  intrinsic at expiry.
- **Gate:** trade only when `ml.fno_vol_gate.gate_decision(realized_vol,
  straddle_iv, k) == SELL_PREMIUM`. All three are SELL-premium structures.
- **Per-unit vs ₹:** every premium/credit below is **per unit**; multiply by
  `qty_lots · NIFTY_LOT` for ₹.

### Sign convention for credit & P&L

For a short option, credit received per unit = its Black-76 premium **minus**
slippage. For a long (protective) option, you **pay** premium **plus** slippage.

```
credit_per_unit = Σ_sell (prem − slip)  −  Σ_buy (prem + slip)
gross_pnl_per_unit(S) = credit_per_unit − Σ_sell payoff(S) + Σ_buy payoff(S)
```

with `payoff(S)` the option's intrinsic at settlement `S` (call:
`max(S−K,0)`, put: `max(K−S,0)`).

---

## 1. THE CRUX — SPAN-margin approximation for naked short legs

A naked short option has **infinite** theoretical loss, so `max_loss` cannot be
the capital base. Real brokers margin these with **SPAN + Exposure**, computed
by the exchange's portfolio risk scan (16 price/vol scenarios). We cannot run
SPAN in a pure backtest, so we use a **defensible, documented proxy**.

### Proxy formula (per short leg, per unit, in ₹ on a per-lot basis)

```
span_margin_per_lot(leg) =
      span_pct · notional_per_lot
    + short_premium_per_lot
```

where

```
notional_per_lot      = spot · NIFTY_LOT          # underlying notional, not strike
short_premium_per_lot = leg_premium · NIFTY_LOT   # premium already collected, held as margin
```

For a multi-leg structure, total margin is the **sum of the short-leg margins**,
optionally reduced by a `margin_offset` factor for two-sided structures where
the exchange grants a spread benefit (a short straddle/strangle gets a partial
offset because both sides can't be deep-ITM simultaneously):

```
total_span_margin = margin_offset · Σ_short span_margin_per_lot(leg)
                    − long_leg_credit_relief        # see Jade Lizard
```

### `span_pct` — what it is and a defensible default

- `span_pct` is the **fraction of underlying notional** the exchange's worst-case
  scenario move would cost. For NIFTY index options the SPAN scan range is
  roughly **±3 σ of a ~6 % daily-equivalent**; in practice NIFTY naked-option
  SPAN+Exposure lands around **~10–15 % of notional** per side in calm regimes,
  higher in stress. The spec default here is `span_pct = 0.10` (10%); **the
  actual engine registry (`FNO_STRATEGIES`) uses `span_pct = 0.12` (12%)** for
  the two-sided naked structures (short straddle, short strangle, jade lizard) as
  a defensible mid of the 10–15% range, because both sides contribute to the
  combined NIFTY SPAN block. The ratio spread uses 0.10 (only 1 uncovered short).
  Both are tunable approximations — state `span_pct` explicitly in every output.
- `margin_offset` default **0.80** for two-sided straddle/strangle (NSE grants a
  meaningful but not full cross-margin benefit between the short call and short
  put). For a one-sided naked leg, `margin_offset = 1.0`.

> **IMPLEMENTATION NOTE:** the engine (`fno_strategies.py`) follows `_ENGINE.md §5b` and uses
> **only** `span_pct · notional_per_lot · short_lots` — it omits the `+short_premium` term and
> the `margin_offset` factor as a **deliberate Phase-0 simplification**. The formula in §1
> above (`span_pct · notional + short_premium` with `margin_offset`) is the more complete
> analytical spec; the code adopts the simpler form because: (a) it avoids double-counting the
> premium in the denominator, and (b) it uses `max(sell qty_lots)` to approximate the SEBI
> single-block netting rather than summing both sides. As a result the proxy is a **rough,
> slightly-conservative-to-rough estimate — not the exchange SPAN**. Treat all ROM figures from
> this engine as approximate and re-validate with a broker SPAN calculator before any go/no-go
> capital decision.

> **THIS IS AN APPROXIMATION.** `span_pct · notional + premium` is a *linear
> proxy* for a nonlinear, scenario-based, regime-dependent number. Real SPAN
> rises sharply when IV spikes (exactly when you're losing) — so this proxy
> **understates** margin in stress and **understates** the cash you'd actually
> need to hold the position. The honest metric for a premium seller is
> **return-on-margin** (`net_pnl / total_span_margin`), NOT return-on-`max_loss`
> (which doesn't exist) and NOT raw credit. Report ROM, and clearly label the
> margin figure as a SPAN approximation in every output. The real go/no-go must
> re-price margin with a broker SPAN calculator or live margin API before any
> capital decision.

### Why notional, not strike

We base SPAN on `spot · lot` (underlying notional) rather than `strike · lot`
because the scenario loss scales with the underlying's move; this matches how
exchange SPAN scans the futures price. Documented here so a reviewer doesn't
"fix" it to strike.

---

## 2. THE OTHER CRUX — mandatory risk exit (stop)

Holding a naked short to expiry in a backtest is **unrealistic and
catastrophic-tail-prone**: a single 1995-/2008-/2020-style gap can exceed many
months of credit. A real desk runs a stop.

> ⚠️ **IMPLEMENTATION STATUS:** The historical `run_strategy_backtest` in
> `fno_strategies.py` is **EXPIRY-ONLY** — it holds every trade to expiry with no
> MTM path stop of any kind. The mandatory risk-stop described below is a
> **forward/live-paper feature, NOT implemented in the historical backtest.**
> Undefined-risk GO verdicts from the historical runner are therefore
> **tail-blind and diagnostic-only**; they must never be treated as evidence of
> a safe strategy. The stop spec below is the target design for forward paper
> and for a future path-stop extension to the historical runner.

### Primary stop: premium / MTM stop (forward/live-paper design)

Exit (buy back the shorts) when the **mark-to-market loss reaches a multiple of
the credit collected**:

```
stop_loss_per_unit = stop_mult · credit_per_unit          # e.g. 2× credit
exit when  mtm_loss_per_unit ≥ stop_loss_per_unit
```

At daily resolution we don't have intraday MTM, so the stop is evaluated against
a **path proxy**. Two supported modes (param `stop_mode`):

- `"close"` *(default, conservative-ish)* — re-price all legs with Black-76 at
  each **daily NIFTY close** between entry and expiry, using the same `sigma`
  (or a per-day VIX-derived sigma if available). If on any day
  `mtm_loss ≥ stop_mult · credit`, the trade exits **that day** at that day's
  re-priced premiums (plus slippage + exit costs). Otherwise it resolves at
  expiry.
- `"expiry_only"` *(diagnostic, NOT for go/no-go)* — no path stop; resolve at
  expiry. Use only to measure how much the stop is protecting you. Loudly
  flagged as tail-blind.

### Secondary stop (optional, param `delta_stop`)

A delta/price stop: exit a side if `S` breaches the short strike by
`breach_mult · step` (e.g. short call strike + 2 steps). Cheap to compute, no
re-pricing. Off by default (`breach_mult = None`); when set, whichever stop
triggers first wins.

### Stop modelling caveats (state loudly)

- **Close-only path misses intraday spikes** — the true MTM can blow through
  `stop_mult · credit` intraday and you'd exit far worse than the daily close
  implies. The close-path stop is therefore **optimistic** about exit price.
- **Gap risk** — a stop is no protection against an overnight gap that opens
  beyond the stop level; you exit at the gapped price, not the stop level.
- These two effects mean the backtested stop **under-models tail losses**. See
  §7.

---

## 3. Strategy 1 — Short Straddle

Sell ATM call + sell ATM put. Maximum credit, maximum theta, **undefined risk
both sides**, narrowest profit zone.

### Legs

```
K_atm = nifty_atm_strike(spot, step)

legs = [
    Leg("CE", "SELL", K_atm, qty_lots),
    Leg("PE", "SELL", K_atm, qty_lots),
]
```

### Entry credit (per unit)

```
sc = black76_call(spot, K_atm, T, sigma)
sp = black76_put (spot, K_atm, T, sigma)
credit_per_unit = (sc − slip(sc)) + (sp − slip(sp))
```

`credit ≈ 2 · ATM premium ≈ 0.8 · implied_move` (ATM straddle price ≈ the
expected move; this is the same identity the gate exploits).

### Risk profile

- **Undefined on BOTH sides.** Loss grows linearly with `|S − K_atm|` once
  beyond a breakeven, unbounded as `S → 0` (put) or `S → ∞` (call).
- **Max profit** = full credit, achieved only if `S == K_atm` at expiry.
- Profit zone is the band between breakevens (narrow — width = 2·credit).

### Breakevens

```
lower_be = K_atm − credit_per_unit
upper_be = K_atm + credit_per_unit
```

### Margin (SPAN proxy)

Two short legs, two-sided → apply `margin_offset` (default 0.80):

```
m_ce = span_pct · spot · LOT + sc · LOT
m_pe = span_pct · spot · LOT + sp · LOT
total_span_margin = margin_offset · (m_ce + m_pe) · qty_lots
```

### Stop

Primary `stop_mult · credit` premium stop (default `stop_mult = 2.0`) — design
per §2. **NOT implemented in the current historical runner (expiry-only).** When
implementing forward paper or a path-stop extension, straddle is the most
stop-dependent of the three (narrowest zone) — never run it `expiry_only` for
go/no-go on a live decision.

### Gate

`gate_decision(...) == SELL_PREMIUM` (sell only when implied is rich vs
realized).

---

## 4. Strategy 2 — Short Strangle

Sell OTM call + sell OTM put at `ATM ± move_mult · implied_move`. Undefined risk
both sides, **wider safe zone**, **less credit** than the straddle.

### Legs

```
em      = implied_move(spot, sigma, dte)          # spot·sigma·√(dte/365)
K_call  = _round_to_step(spot + move_mult · em, step)
K_put   = _round_to_step(spot − move_mult · em, step)

legs = [
    Leg("CE", "SELL", K_call, qty_lots),
    Leg("PE", "SELL", K_put,  qty_lots),
]
```

`move_mult` default **1.0** (shorts ≈ ±1 expected move). The condor uses 1.5;
the strangle is typically tighter to collect meaningful premium, so default 1.0
and expose the param.

### Entry credit (per unit)

```
sc = black76_call(spot, K_call, T, sigma)
sp = black76_put (spot, K_put,  T, sigma)
credit_per_unit = (sc − slip(sc)) + (sp − slip(sp))
```

Smaller than the straddle (both legs OTM).

### Risk profile

- **Undefined on BOTH sides** beyond the breakevens.
- **Max profit** = full credit, kept for any `K_put ≤ S ≤ K_call` at expiry
  (a real range, unlike the straddle's single point).
- Loss linear/unbounded once `S < lower_be` or `S > upper_be`.

### Breakevens

```
lower_be = K_put  − credit_per_unit
upper_be = K_call + credit_per_unit
```

### Margin (SPAN proxy)

Same two-sided form as the straddle (OTM premiums are smaller, so margin is
slightly lower):

```
total_span_margin = margin_offset · (
      (span_pct · spot · LOT + sc · LOT)
    + (span_pct · spot · LOT + sp · LOT)
) · qty_lots
```

### Stop

Primary `stop_mult · credit` (default 2.0) — design per §2. **NOT implemented
in the current historical runner (expiry-only).** Strangle's wider zone means the
stop would trigger less often than the straddle's, but the per-event loss can be
larger (strikes are further out → you're already losing by the time MTM doubles
the smaller credit).

### Gate

`SELL_PREMIUM`.

---

## 5. Strategy 3 — Jade Lizard

A **short put** (cash-secured / naked) **plus a short call SPREAD** (short call +
long higher call). Sized so **total credit ≥ call-spread width → NO upside
risk**. Downside risk is undefined (the naked short put). **Partial defined
risk.**

### Legs

```
em      = implied_move(spot, sigma, dte)

# Short put: OTM below spot
K_sp    = _round_to_step(spot − put_mult · em, step)         # put_mult default 1.0

# Short call: OTM above spot
K_sc    = _round_to_step(spot + call_mult · em, step)        # call_mult default 0.5
# Long call: wing_strikes steps above the short call
K_lc    = K_sc + wing_strikes · step                         # wing_strikes default 2

legs = [
    Leg("PE", "SELL", K_sp, qty_lots),
    Leg("CE", "SELL", K_sc, qty_lots),
    Leg("CE", "BUY",  K_lc, qty_lots),
]
```

The defining constraint: place `K_sc` close enough (small `call_mult`) that the
call-spread credit plus the put credit clears the spread width.

### Entry credit (per unit)

```
sp_p = black76_put (spot, K_sp, T, sigma)
sc_p = black76_call(spot, K_sc, T, sigma)
lc_p = black76_call(spot, K_lc, T, sigma)

credit_per_unit = (sp_p − slip) + (sc_p − slip) − (lc_p + slip)
call_spread_width = K_lc − K_sc                # in points
```

### THE Jade-Lizard invariant (no upside risk)

```
NO upside risk  ⟺  credit_per_unit ≥ call_spread_width
```

If at construction `credit_per_unit < call_spread_width`, the position **does**
have capped upside risk = `(call_spread_width − credit)`. The builder should
either (a) tighten `K_sc` / widen until the invariant holds, or (b) flag
`upside_risk = max(0, call_spread_width − credit)` so the backtest accounts for
it honestly. **Do not silently assume zero upside risk** — verify the invariant
per cycle and record it.

### Risk profile

- **Upside (S > K_lc):** loss capped at `max(0, call_spread_width − credit)`.
  When the invariant holds, this is **≤ 0 → no loss** (you keep net credit even
  if the call spread goes max-ITM).
- **Middle (K_sp ≤ S ≤ K_sc):** full credit kept.
- **Downside (S < K_sp):** **UNDEFINED** loss — the naked short put.
  `loss = (K_sp − S) − credit` per unit, unbounded as `S → 0`.

### Breakeven (only one, on the downside)

```
lower_be = K_sp − credit_per_unit
# no upper breakeven when the invariant holds (upside is all profit/flat)
```

### Margin (SPAN proxy)

The short put is naked → full SPAN. The call spread is defined-risk → margin is
just its (small, capped) max loss, and the long call gives premium relief:

```
m_put          = span_pct · spot · LOT + sp_p · LOT            # naked → full
m_call_spread  = max(0, call_spread_width − (sc_p − lc_p)) · LOT  # defined-risk leg
total_span_margin = (m_put + m_call_spread) · qty_lots
```

No `margin_offset` here (the two sides are not symmetric; the call side is
already defined-risk and small).

### Stop

The undefined side is the **put only**. Primary stop (design per §2, **NOT
implemented in the current historical runner — expiry-only**):

- `stop_mult · credit` premium stop (default 2.0), OR
- price stop on the put: exit if `S < K_sp − breach_mult · step`.

The call spread needs no stop (capped). Exiting on a stop closes all three legs
(or at least the naked put — param `stop_legs = "put_only" | "all"`, default
`"all"` for cleanliness).

### Gate

`SELL_PREMIUM`. (Jade Lizard is still a net-short-vol / premium-collection
structure.)

---

## 6. Params dataclass

One dataclass per strategy, or a shared base — recommend a shared base plus
per-strategy fields. All defaults below.

```python
@dataclass(frozen=True)
class PremiumSellParams:
    # ── shared: structure ──────────────────────────────────────────
    step:          int   = 50          # NIFTY strike grid
    qty_lots:      int   = 1
    # ── shared: margin (SPAN proxy) ────────────────────────────────
    span_pct:      float = 0.12        # frac of notional; SPAN APPROXIMATION
                                       # engine registry uses 0.12 for two-sided naked
                                       # structures (straddle/strangle/jade) as a
                                       # defensible mid of 10–15%; ratio uses 0.10
                                       # (only 1 uncovered short). Both are tunable.
    margin_offset: float = 0.80        # cross-margin benefit, two-sided structs
    # ── shared: risk exit (MANDATORY) ──────────────────────────────
    stop_mult:     float = 2.0         # exit when mtm_loss ≥ stop_mult·credit
    stop_mode:     str   = "close"     # "close" | "expiry_only" (diagnostic)
    breach_mult:   Optional[float] = None  # price stop: strike ± breach_mult·step
    stop_legs:     str   = "all"       # "all" | "put_only" (jade lizard)
    # ── shared: gate + slippage ────────────────────────────────────
    k:             float = 0.9         # vol-gate threshold (DEFAULT_K)
    slip_pct:      float = 0.005       # 0.5% per-leg adverse slippage
    capital:       float = 200_000.0   # for return-on-capital reporting

    # ── strangle-specific ──────────────────────────────────────────
    move_mult:     float = 1.0         # shorts at ATM ± move_mult·implied_move

    # ── jade-lizard-specific ───────────────────────────────────────
    put_mult:      float = 1.0         # short put at spot − put_mult·implied_move
    call_mult:     float = 0.5         # short call at spot + call_mult·implied_move
    wing_strikes:  int   = 2           # long-call width in steps
```

(Straddle ignores `move_mult/put_mult/call_mult/wing_strikes`; that's fine —
keep one dataclass for engine uniformity.)

---

## 7. Honest caveats — STATE LOUDLY

1. **Black-76 `sigma` is a single-IV proxy and regime-dependent.** One `sigma`
   for all strikes ignores the volatility skew/smile. NIFTY puts trade at higher
   IV than equidistant calls (crash premium); using ATM `sigma` for OTM put legs
   **understates** their premium and **understates** downside risk pricing. The
   proxy is least accurate exactly where the undefined risk lives.

2. **Close, not FSP.** Settlement uses the NIFTY daily **close**, not the NSE
   **Final Settlement Price** (30-min VWAP of futures, 15:00–15:30 IST). Entry
   uses the boundary-day close, not the next morning's actual fill. Both are the
   same approximations the condor harness documents.

3. **TAIL RISK IS UNDER-MODELED — HISTORICAL RUNNER IS EXPIRY-ONLY.** ⚠️ This
   is the loudest caveat. The historical `run_strategy_backtest` holds every trade
   to expiry with **no MTM path stop**. Undefined-risk premium selling makes steady
   small gains and rare huge losses; without a stop the historical metrics are
   tail-blind. Even a future path-stop extension using daily closes:
   - cannot see the **intraday spike** that would trigger (and overshoot) the
     stop — so realized stop losses are worse than modeled;
   - cannot see **overnight gaps** beyond the stop level;
   - uses a **static `sigma`**, so it never models the IV explosion that makes
     both the loss AND the margin balloon together (margin call / forced exit at
     the worst price).
   The net effect: **the backtest's worst loss and drawdown are optimistic.** A
   GO on this backtest is **weaker evidence** than a GO on the defined-risk
   condor and is **diagnostic-only**. Treat a GO as "worth forward paper-trading
   on the real chain," never as "safe to deploy capital."

4. **SPAN is approximate and pro-cyclical.** `span_pct · notional + premium` is
   linear; real SPAN jumps in stress. Report **return-on-margin**, label margin
   as an approximation, and re-price with a broker SPAN tool before go/no-go.

5. **Stop fills are idealized.** We assume you can buy back the shorts at the
   re-priced (close) premium + slippage. In a fast tape, OTM short-option
   spreads widen brutally; real exit cost > modeled.

6. **Assignment / pin risk** at expiry near a short strike is not modeled
   (cash-settled NIFTY mitigates physical assignment, but FSP pin still bites).

---

## 8. Feasibility

- **Single-expiry, daily-step → backtestable today** with the existing
  `cycles_from_db("NIFTY", mode="weekly")` cycle loader (NIFTY + VIX from
  `index_bars`), exactly as the condor uses. `sigma = straddle_iv = VIX/100`
  (Phase-0 proxy) per cycle.
- The path stop (`stop_mode="close"`) needs **daily NIFTY closes between entry
  and expiry** — already in `index_bars`. A small extension to the cycle loader
  (or a per-cycle "path" list of daily closes) supplies this; otherwise the
  harness can query `index_bars` for the entry→expiry close path per cycle.
- Real-chain forward validation reuses the `core/fno_paper.py` pattern
  (`fno_paper_trades` table) — straightforward to add strategy-typed rows later;
  out of scope for this plan.
- No new dependencies; reuses `black76_*`, `condor_costs`, `slippage`,
  `implied_move`, `nifty_atm_strike`, `gate_decision`.

---

## 9. Unit-test cases (per strategy)

Inputs are deterministic; assert legs, credit sign, margin proxy, stop trigger.
Use a fixed cycle: `spot = 23_400`, `step = 50`, `dte = 7`, `sigma = 0.13`
(→ `T = 0.01918`, `implied_move ≈ 23400·0.13·√(7/365) ≈ 421 pts`),
`span_pct = 0.10`, `margin_offset = 0.80`, `stop_mult = 2.0`, `LOT = 65`.
(Compute exact expected numbers from `black76_*` in the test; values below are
the assertion *shape* + ballparks.)

### 9.1 Short Straddle (6 cases)

1. **Legs:** builder → exactly 2 legs: `SELL CE @ 23400`, `SELL PE @ 23400`
   (both at ATM = `nifty_atm_strike(23400,50) = 23400`).
2. **Credit > 0 and ≈ 2·ATM premium:** `credit_per_unit ≈ 0.8·implied_move`
   (≈ 320–360 pts); assert positive and within ±15 % of `2·black76_call(ATM)`.
3. **Breakevens symmetric:** `lower_be == 23400 − credit`,
   `upper_be == 23400 + credit`; assert `upper_be + lower_be == 2·23400`.
4. **Margin proxy:** `total ≈ 0.80·(2·(0.10·23400·65) + (sc+sp)·65)`. Assert it
   equals the formula to the rupee, and assert it is **far larger** than
   `credit·LOT` (margin ≫ credit — the whole point).
5. **Stop triggers on a move:** with a path containing a day where re-priced
   `mtm_loss == 2.1·credit`, assert the trade exits that day (not at expiry) and
   `net_pnl ≈ −stop_mult·credit·LOT − costs`.
6. **No stop → expiry win:** path stays within `[lower_be, upper_be]`,
   `expiry_spot = 23400` → `gross ≈ full credit`, `win == True`,
   `stop_mode="close"` did not trigger.

### 9.2 Short Strangle (6 cases)

1. **Legs:** `move_mult=1.0` → `SELL CE @ round(23400+421)=23800` (→ 23800),
   `SELL PE @ round(23400−421)=22950` (→ 22950 or 23000 — assert exact rounding
   via `_round_to_step`).
2. **Credit > 0, smaller than straddle:** assert
   `strangle_credit < straddle_credit` for the same cycle.
3. **Wider safe zone:** assert `(upper_be − lower_be)_strangle >
   (upper_be − lower_be)_straddle`.
4. **`move_mult` scaling:** `move_mult=2.0` pushes strikes further out and
   `credit` strictly smaller than `move_mult=1.0`.
5. **Margin proxy:** equals `0.80·((0.10·23400·65 + sc·65)+(0.10·23400·65 +
   sp·65))`; assert to the rupee.
6. **Stop trigger:** craft a path day with `mtm_loss = 2.0·credit` → exits;
   assert exit P&L and that an `expiry_only` run on the SAME path would have lost
   more (demonstrates the stop's protection).

### 9.3 Jade Lizard (7 cases)

1. **Legs:** 3 legs in order `SELL PE @ K_sp`, `SELL CE @ K_sc`, `BUY CE @ K_lc`
   with `K_lc == K_sc + 2·50`; assert strikes from `put_mult=1.0, call_mult=0.5,
   wing_strikes=2`.
2. **Invariant HOLDS:** choose `call_mult` small enough that
   `credit_per_unit ≥ call_spread_width (=100)` → assert `upside_risk == 0`.
3. **Invariant VIOLATED:** with a wider call spread (`wing_strikes=4 →
   width=200`) such that `credit < width`, assert
   `upside_risk == width − credit > 0` and that it's recorded, not hidden.
4. **Downside undefined:** at `expiry_spot = K_sp − 500`, assert
   `gross_pnl == (credit − (K_sp − S))·LOT < 0` and that loss scales linearly
   with deeper `S` (compare S−500 vs S−1000).
5. **Single breakeven:** assert `lower_be == K_sp − credit` and that there is no
   finite upper breakeven when the invariant holds (upside P&L ≥ 0 for all
   `S ≥ K_sc`).
6. **Margin proxy:** `total == (0.10·23400·65 + sp_p·65) +
   max(0, 100 − (sc_p − lc_p))·65`; assert naked-put term dominates and the
   call-spread term is small/capped.
7. **Stop on put side:** `breach_mult=2.0`, path where `S < K_sp − 2·50` → exit
   triggered, `stop_legs="all"` closes all three legs; assert net loss ≈ the
   stopped MTM, not the deep-expiry loss.

### 9.4 Shared / gate cases (apply to all three)

- **Gate blocks:** when `gate_decision(realized, implied, k) != SELL_PREMIUM`
  (e.g. `realized > implied` → BUY_PREMIUM), the runner records **no trade** for
  that cycle.
- **Fail-open inputs:** `sigma <= 0` or `dte == 0` → `implied_move` returns
  None/0; builder must degrade gracefully (skip cycle, no crash).
- **Return-on-margin reported:** harness output includes `return_on_margin =
  net_pnl / total_span_margin`, labeled as SPAN-approximate, alongside (not
  instead of) `return_on_capital`.

---

## 10. Build order (when coding starts — not in this PR)

1. `Leg` dataclass + `PremiumSellParams` (or extend existing types).
2. Three pure `build_*` functions (spot, atm, step, dte, sigma, params → legs).
3. Pure `price_*` / `resolve_*` per strategy (reuse `black76_*`).
4. `span_margin(...)` helper (the §1 proxy) — single source of truth.
5. Path-stop evaluator (`stop_mode="close"`) using per-cycle daily closes.
6. `run_backtest` mirroring the condor's loop + metrics, adding
   `return_on_margin` and stop diagnostics.
7. Unit tests from §9.
8. `go_no_go` reusing the condor's criteria, **plus** a margin-sanity criterion
   (ROM positive) and a loud "tail-under-modeled" disclaimer in the reason
   string.
