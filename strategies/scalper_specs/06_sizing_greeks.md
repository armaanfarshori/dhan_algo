# 06 — Position Sizing + Greeks / DTE Awareness (Options Scalper)

**Status:** PLAN — research only, PAPER. No live order paths.
**Scope:** sizing rules, premium-at-risk model, lot/exposure constraints, theta/gamma awareness,
DTE preferences, and how sizing interacts with the daily-loss governor for
`strategies/options_scalper.py`.
**Companions:** strategy spec `docs/fno_strategies/options_scalper.md`; cost stack
`research/backtest/fno_costs.py` (`NIFTY_LOT = 65`, `condor_costs`, `slippage`).

> This document specifies *how big* each ladder/tranche should be and *when not to trade* given
> the option's Greeks and time-to-expiry. The scalper today sizes by a **fixed `tranche_lots`
> (default 1) × `max_rungs` (default 3)** with no risk-based sizing and **no DTE/Greeks gate**.
> This spec defines the additions. None of it changes the hard rules in §6 of the strategy spec;
> it sits *above* the entry logic as a sizing/eligibility layer. **The platform `RiskEngine` still
> owns the kill-switch and is never bypassed** — everything here is strategy-internal sizing.

---

## 1. Why long-option scalps need explicit sizing + Greeks awareness

A long single-leg scalp is **decaying, convex, and spread-sensitive**:

- **Premium-at-risk is bounded but real.** The most a long option can lose is its premium, but the
  scalper's *stop* is `stop_pct` (default 20%) of premium, so the *intended* risk per tranche is
  `stop_pct × premium × lot`, not the full premium. Sizing must be driven by that intended risk,
  not by a fixed lot count — at ₹80 premium vs ₹240 premium, "1 lot" is a 3× different ₹-risk.
- **Theta bleeds the position even when the underlying is flat.** Near expiry (0–1 DTE), an ATM
  NIFTY option can lose a meaningful fraction of its premium per hour of stagnation. The
  `time_stop_min` (12 min) guard exists for exactly this, but theta also dictates **which DTE we
  should scalp at all** and **how late in the day** a fresh ladder is sane.
- **Gamma is the edge and the danger.** ATM gamma peaks near expiry, which is what makes a few
  NIFTY points convert into a scalp-able premium move (good). But high gamma also means the stop
  can be hit on noise (bad). DTE choice trades theta bleed against gamma responsiveness.
- **Costs are fixed-₹ per order + %-of-premium.** A bigger tranche amortises the ₹40 round-trip
  brokerage better, but %-slippage and STT scale with premium turnover. Sizing interacts with the
  break-even target.

The strategy spec already nails *exits* (TP ladder / trail / hard-stop / time-stop). This spec
fills the two gaps it left open: **how many lots** and **at what DTE / time-of-day**.

---

## 2. Fixed-fractional position sizing (premium-at-risk)

### 2a. Core model — risk a fixed fraction of capital per ladder

Define one risk budget **per ladder** (not per tranche), so pyramiding never silently multiplies
intended risk:

```
risk_capital            = account equity allocated to this strategy (₹)   [param: risk_capital]
risk_frac_per_ladder    = fraction of risk_capital risked if the WHOLE ladder stops out
                          [param: risk_frac_per_ladder, default 0.01 = 1%]

ladder_risk_budget_inr  = risk_capital * risk_frac_per_ladder
```

`ladder_risk_budget_inr` is the ₹ we are willing to lose if **every filled tranche** hits its
`stop_pct` hard stop. It is computed once at the **first rung** of a ladder and frozen for that
ladder's life (consistent with `ladder_strike` / `ladder_anchor_underlying` being fixed at first
fill).

### 2b. From risk budget → lots

The per-unit risk of one tranche is the stop distance in premium:

```
per_unit_risk   = entry_premium * stop_pct                 # ₹/unit lost if stop hits
per_lot_risk    = per_unit_risk * lot                       # lot = 65
```

We want the **fully-laddered** position to risk at most `ladder_risk_budget_inr`. With `max_rungs`
planned tranches of equal size (`ladder_size_mode="flat"`), the max lots **per tranche** is:

```
lots_per_tranche = floor( ladder_risk_budget_inr / (per_lot_risk * max_rungs) )
lots_per_tranche = max(min_lots, min(lots_per_tranche, max_lots_per_tranche))
```

- `entry_premium` at sizing time = the **best available premium estimate at the first rung**
  (live LTP of the ATM contract in forward-paper; Black-76 mid in the optional backtest). If
  premium is unknown at sizing time, **do not size** — skip the entry (fail-safe, mirrors the
  `notify_fill BUY premium<=0` guard already in the code).
- `ladder_size_mode="decreasing"` (1, halve, floor 1): size the **first** tranche from the budget,
  then the existing `_tranche_lots_for_rung` halving applies to subsequent rungs. Risk is then
  *below* budget (conservative) — acceptable.
- Result feeds `ScalperParams.tranche_lots` *dynamically* per ladder rather than the static
  default. (Implementation note: add a `sizing` block that overrides `tranche_lots` at
  `_evaluate_new_ladder`; the existing `_tranche_lots_for_rung` stays the shape function.)

### 2c. Worked example

```
risk_capital            = 200000
risk_frac_per_ladder    = 0.01            -> ladder_risk_budget_inr = 2000
entry_premium           = 120  (ATM NIFTY CE)
stop_pct                = 0.20
max_rungs               = 3
lot                     = 65

per_unit_risk = 120 * 0.20            = 24 ₹/unit
per_lot_risk  = 24 * 65               = 1560 ₹/lot
lots_per_tranche = floor(2000 / (1560 * 3)) = floor(0.427) = 0  -> clamp to min_lots=1
```

So at ₹120 premium with 1% risk on ₹2L, the risk-based size **wants less than one lot**; we clamp
to `min_lots = 1` and accept that one full 3-rung ladder then risks ~₹4,680 (1560 × 3) ≈ 2.3% —
which is why the **`max_concurrent_ladders` and exposure caps in §3 are the real governors at this
account size.** At a larger `risk_capital` (e.g. ₹1,000,000 → budget ₹10,000) the formula yields
`floor(10000 / 4680) = 2` lots/tranche and sizing becomes the binding constraint as intended.

**Takeaway for params:** at small allocations the floor dominates; document the risk a single
min-lot ladder actually carries so the daily governor (§5) is calibrated to it, not to the
nominal `risk_frac`.

### 2d. Why fixed-fractional (not fixed-lot, not Kelly)

- **Fixed-lot** ignores premium level → wildly different ₹-risk across strikes/days. Rejected.
- **Kelly / vol-targeting** needs a reliable edge estimate the strategy does not have yet (no
  honest backtest — see strategy spec §5). Premature. Revisit only after a forward-paper track
  produces a stable per-scalp expectancy.
- **Fixed-fractional on premium-at-risk** is the standard, transparent middle ground and maps
  cleanly onto the existing `stop_pct`. Chosen.

---

## 3. Lot constraints + concurrent exposure caps

### 3a. Lot discretisation
- NIFTY lot = **65 units** (`fno_costs.NIFTY_LOT`). All sizes are integer lots; `lots × 65` units.
- `min_lots = 1`, `max_lots_per_tranche = 5` (hard ceiling regardless of what §2b computes — a
  liquidity/slippage guard; a scalper exiting >5 ATM lots at once moves the book).

### 3b. Per-ladder exposure (premium notional)
Cap the **premium outlay** of a single ladder so a high-premium day (post-event IV spike) cannot
quietly buy a huge position:

```
ladder_premium_outlay = sum(tranche_lots_i * lot * entry_premium_i)   over filled rungs
require: ladder_premium_outlay <= max_ladder_premium_inr   [param, default 60000]
```

Block the **next rung** if adding it would breach `max_ladder_premium_inr` (the first rung always
allowed if the strategy is otherwise eligible — sizing in §2 already constrained it).

### 3c. Concurrent exposure across ladders
The current engine runs one ladder at a time (`_tranches` is a single book; a new ladder only
opens when flat). Keep that as the default (`max_concurrent_ladders = 1`) — **simplest correct
exposure control for a scalper.** Reserve a portfolio-level cap for any future multi-instrument
version:

```
max_concurrent_premium_inr = 80000   [param]   # total long-premium across all open ladders
```

With `max_concurrent_ladders = 1` and `max_ladder_premium_inr = 60000`, this is naturally
satisfied; it becomes load-bearing only if concurrency > 1 is ever enabled.

### 3d. Interaction with `max_rungs`
`max_rungs` (3) is the **shape** cap (how many adds); the §2b risk math sizes each rung; §3b is the
**₹** cap. All three apply — a rung is added only if (i) `rungs_requested < max_rungs`, (ii) the
favorable-move trigger fired, **and** (iii) the new rung keeps `ladder_premium_outlay` under
`max_ladder_premium_inr`.

---

## 4. Theta / Gamma awareness + DTE handling

The scalper is **long premium**, so theta is a pure cost and gamma is the engine. The contract is
NIFTY weekly options; DTE on entry ranges 0 → ~4 within a weekly cycle (Tuesday expiry as of 2026;
**verify current NIFTY weekly expiry weekday before live**).

### 4a. DTE preference table (entry eligibility)

| DTE (on entry) | Gamma | Theta bleed | Spread | Verdict for this scalper |
|---|---|---|---|---|
| **0 (expiry day)** | extreme (ATM) | extreme — premium can halve in hours of chop | tight early, widens near pin | **ALLOW but restricted** (§4c): tighter time-stop, no fresh ladders in the last leg, smaller size |
| **1** | high | high | tight | **PREFERRED** — best gamma:theta:liquidity balance for a fast scalp |
| **2** | moderate | moderate | tight | **ALLOW** — default sweet spot; premium moves enough, theta survivable |
| **3–4** | lower | low | tight | **ALLOW but de-prioritised** — premium less responsive to a few points; bigger move needed to clear costs |
| **≥5 / next weekly** | low | low | wider on far weekly | **AVOID** — too sluggish for a points-scalp; gamma too low to convert §1 signal into premium |

Param: `allowed_dte = [0, 1, 2, 3]` (default; excludes ≥4). `min_dte = 0`, `max_dte = 3`.
An entry whose ATM contract DTE falls outside `[min_dte, max_dte]` → **no new ladder** (existing
positions still managed/exited normally).

### 4b. Theta-scaled time-stop

Theta is non-linear in DTE: the 12-min `time_stop_min` is calibrated for ~1–2 DTE. On expiry day,
a stagnant scalp bleeds far faster, so tighten the time-stop by DTE:

```
effective_time_stop_min = round(time_stop_min * theta_dte_mult[dte])

theta_dte_mult = {0: 0.5, 1: 0.83, 2: 1.0, 3: 1.0}   [param]
```

So 0 DTE → ~6 min, 1 DTE → ~10 min, 2–3 DTE → 12 min. This makes the existing per-tranche
time-stop DTE-aware without changing its mechanic (it still only fires on tranches that have not
hit TP1, per §3.4 of the strategy spec).

### 4c. Expiry-day (0 DTE) intraday time-decay handling

On 0 DTE, theta accelerates through the afternoon and the pre-close pin distorts premiums:

- **No fresh ladders after `dte0_last_entry`** (default **13:30 IST**). After this, manage/exit
  only — a new long scalp opened into accelerating afternoon decay + pin risk is a coin flip with
  negative drift.
- **Size haircut:** multiply §2b `lots_per_tranche` by `dte0_size_mult` (default **0.5**, floor
  `min_lots`) — smaller bets when both gamma noise and theta are extreme.
- **Tighter hard-stop optional:** allow `stop_pct` to be overridden to `dte0_stop_pct`
  (default keep `0.20`; expose the knob). Not tightened by default because gamma noise would whip
  a tighter stop; left to forward-paper tuning.
- **Earlier square-off unaffected** — the unconditional `squareoff_before_close_min` (5 min) still
  governs; 0 DTE just stops *new* entries earlier and sizes them smaller.

### 4d. Gamma / strike interaction (links to strike_offset, §1d of strategy spec)
- ATM (`strike_offset=0`, default): delta ≈ 0.5, peak gamma → best signal-to-premium conversion
  and tightest spread. Default and recommended.
- ITM (`strike_offset=-1`): higher delta (~0.6–0.7), **lower gamma and lower theta fraction** →
  steadier, less noise-stop, but costs more premium → **larger per-lot ₹-risk** → §2b yields fewer
  lots. Sensible on cleaner trends; sizing self-adjusts.
- OTM (`strike_offset=+1`): high gamma, fast theta, **wide relative spread eats the small TP
  targets** → not recommended; if used, the §2c break-even check below will usually reject it.

### 4e. Greeks data source
We have **no intraday IV feed** (strategy spec §5). Greeks here are used **qualitatively** (DTE
buckets, time-of-day rules) — **not** computed live for sizing. The sizing math in §2 needs only
**premium + stop_pct**, both of which we have (live LTP / Black-76 mid). Do **not** gate entries on
a fabricated live delta/gamma/theta number; the DTE table is the proxy. If the optional Black-76
path is built, Greeks may be reported (with σ held constant, clearly flagged) for diagnostics only.

---

## 5. Interaction with the daily-loss governor

The strategy already has a strategy-level **daily-loss kill** (`daily_loss_cap`, default ₹8000;
`_update_realized_pnl` → `_standing_down`). Sizing must be coherent with it, otherwise one ladder
can blow the daily cap, or the cap can be unreachable.

### 5a. Calibrate the daily cap to the per-ladder risk
The daily cap should allow a **sane number of full losers** before standing down:

```
max_full_losers_per_day  = daily_loss_cap / worst_case_ladder_loss

worst_case_ladder_loss   ≈ max_rungs * lots_per_tranche * lot * entry_premium * stop_pct
                           + round_trip_costs(per ladder)
```

Worked (defaults, ₹120 premium, 1 lot/tranche, 3 rungs):
```
worst_case_ladder_loss ≈ 3 * 1 * 65 * 120 * 0.20  + ~₹150 costs ≈ 4680 + 150 ≈ 4830
max_full_losers_per_day = 8000 / 4830 ≈ 1.65
```

**Finding:** with default ₹8000 cap and default sizing, the strategy stands down after **~1.6 full
losing ladders** — effectively after the *second* full stop-out. That may be intentionally
conservative, but it means `max_trades = 8` is nearly unreachable on a bad day. **Recommendation:**
either (a) raise `daily_loss_cap` to ~₹15,000 (≈3 full losers) for a more usable session, or
(b) keep ₹8000 and document it as a hard 2-loser circuit-breaker. Default in this spec: **keep
₹8000** and treat it as the 2-loser breaker; expose the knob.

### 5b. Remaining-budget gate on new ladders (proactive, not just reactive)
Today `_standing_down` fires *after* a loss crosses the cap. Add a **pre-trade** check so we never
**open** a ladder whose worst-case loss would exceed the **remaining** daily budget:

```
remaining_daily_budget = daily_loss_cap + daily_realized_pnl     # pnl is negative when down
if worst_case_ladder_loss > remaining_daily_budget:
    -> no new ladder (size down to min_lots first; if min-lot ladder still exceeds, skip)
```

This makes the governor and the sizer consistent: the sizer never commits risk the governor would
have to kill mid-trade. (Down-sizing first, then skipping, is the order — preserve participation
where a smaller lot still fits the budget.)

### 5c. Precedence (unchanged hard rules win)
Sizing/eligibility is a layer **below** the strategy's hard rules. Order of authority:
1. **Unconditional EOD square-off** (strategy §6.2) — always.
2. **Platform RiskEngine kill-switch** (live wiring) — always; never bypassed.
3. **Daily-loss stand-down** (`_standing_down`) — blocks all new ladders.
4. **§5b remaining-budget gate** — blocks/sizes-down a new ladder.
5. **§4 DTE / 0-DTE time gates** — block new ladders out of allowed DTE / after `dte0_last_entry`.
6. **§3 exposure caps** — block the next rung.
7. **§2 fixed-fractional sizing** — sets lots for an allowed ladder/rung.

Exits (TP/trail/stop/time-stop/signal-flip) are **never** blocked by any sizing layer — a position
must always be manageable to exit (mirrors the screener's "open positions exempt from validation"
principle).

---

## 6. New parameters (add to `ScalperParams`)

```python
# ── position sizing (fixed-fractional, premium-at-risk) ── §2
risk_capital: float = 200000.0        # ₹ allocated to this strategy (sizing base)
risk_frac_per_ladder: float = 0.01    # fraction risked if the whole ladder stops out (1%)
min_lots: int = 1                     # floor per tranche
max_lots_per_tranche: int = 5         # hard ceiling per tranche (liquidity guard)

# ── exposure caps ── §3
max_ladder_premium_inr: float = 60000.0      # max premium outlay per ladder
max_concurrent_ladders: int = 1              # one ladder at a time (current engine default)
max_concurrent_premium_inr: float = 80000.0  # total long-premium across ladders (multi only)

# ── DTE / theta / gamma ── §4
min_dte: int = 0                      # inclusive
max_dte: int = 3                      # inclusive  (allowed_dte = [0,1,2,3])
theta_dte_mult: dict = field(default_factory=lambda: {0: 0.5, 1: 0.83, 2: 1.0, 3: 1.0})
dte0_last_entry: str = "13:30"        # IST; no NEW ladders after this on 0 DTE
dte0_size_mult: float = 0.5           # size haircut on expiry day
dte0_stop_pct: float = 0.20           # expiry-day hard stop (knob; default = base stop_pct)

# ── daily governor coherence ── §5
# daily_loss_cap already exists (default 8000.0). This spec keeps it and adds the
# proactive remaining-budget gate (§5b) computed from worst_case_ladder_loss.
```

Notes:
- `dte0_last_entry` is a string parsed to `dtime` at session reset (consistent with how
  `MARKET_OPEN`/`MARKET_CLOSE` are handled), or store as a `dtime` field if preferred.
- `dte` must be supplied to the strategy (it knows the underlying, not the expiry calendar).
  Plumb it in via the `on_tick` caller / a `set_expiry(expiry_date)` setter computed against the
  NIFTY weekly expiry calendar — **the strategy must not hardcode the expiry weekday.**

---

## 7. Concrete default one-liner (sizing/Greeks layer)

> **Risk 1% of the ₹2L allocation per ladder; size each tranche from premium-at-risk
> (`stop_pct × premium × lot`) across `max_rungs`, clamped to 1–5 lots; cap a ladder at ₹60k
> premium outlay and one ladder at a time. Trade only 0–3 DTE (prefer 1–2); on expiry day halve
> size, tighten the time-stop to ~6 min, and open no fresh ladder after 13:30. Never open a ladder
> whose worst-case 3-rung stop-out would exceed the remaining daily-loss budget (₹8,000 cap ≈ a
> 2-full-loser circuit-breaker). Exits are never sized-gated — a position can always be flattened.**

---

## 8. Open items / flags for the coder

1. **Expiry/DTE plumbing is a dependency**, not in this file's scope: the strategy needs the held
   contract's expiry to compute DTE and the 0-DTE rules. Provide it from the caller
   (forward-paper feed selects the ATM contract → knows its expiry). Do **not** hardcode the
   weekly-expiry weekday.
2. **Small-account reality (§2c finding):** at ₹2L / 1% the risk formula floors to 1 lot and the
   exposure/governor caps are the binding constraints. Calibrate the daily cap to the *actual*
   min-lot ladder risk (~₹4.8k worst case), not the nominal 1%.
3. **Daily-cap usability (§5a finding):** default ₹8000 ≈ 1.6 full losers → stands down after the
   2nd full stop-out, making `max_trades=8` mostly unreachable on bad days. Documented as a
   deliberate 2-loser breaker; raise to ~₹15k if a more active session is wanted.
4. **No live Greeks (§4e):** Greeks are used qualitatively (DTE buckets, time-of-day), never
   computed for sizing. Sizing needs only premium + `stop_pct`. Do not gate on fabricated deltas.
5. **Costs vs. size:** larger tranches amortise the ₹40 round-trip brokerage but scale STT/slippage
   with premium turnover; the break-even target % is roughly size-invariant in % terms but the
   ₹-floor (brokerage) hurts tiny single-lot scalps most. The forward-paper report must show
   net-of-cost per-scalp expectancy by lot size (ties back to strategy spec §5d).
6. **All PAPER.** This layer changes sizes/eligibility only; the platform `RiskEngine` remains the
   kill-switch owner in any live wiring and is never bypassed.
