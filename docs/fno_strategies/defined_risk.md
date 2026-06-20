# Defined-Risk Premium-Selling Strategies — F&O Backtester Spec

**Status:** PLANNER spec (code not yet written). Branch `feat/fno-options-strategies`.
**Scope:** Three SELL_PREMIUM, vol-gated, **defined-risk** NIFTY weekly-options structures for the
generalized multi-leg backtest engine. Research-only, PAPER, no live order paths.

This document is the implementation contract for the leg builders. It is written against the
existing reference code so the strategies drop into the generalized engine with no surprises:

- `research/backtest/fno_condor.py` — Black-76 pricing (`black76_call` / `black76_put`),
  `_round_to_step`, the existing `build_condor` / `price_condor` / `resolve_condor`, slippage
  application on legs, and `condor_costs` wiring. **All three strategies reuse this machinery.**
- `research/backtest/fno_costs.py` — `NIFTY_LOT = 65`, `slippage(premium, pct=0.005)`,
  `condor_costs(legs, exercise_intrinsic)` where each leg is `(premium_per_unit, qty_units, side)`.
- `core/fno_derived.implied_move(spot, straddle_iv, dte)` → `spot · iv · √(dte/365)` index points.
- `ml/fno_vol_gate.gate_decision(realized_vol, implied_vol, k=DEFAULT_K)` → only **SELL_PREMIUM**
  cycles are traded; `DEFAULT_K ≈ 0.9`.

---

## 0. The generalized leg model (target engine)

A strategy is a pure builder:

```python
builder(spot, atm_strike, step, dte, sigma, params) -> list[Leg]
```

```python
@dataclass(frozen=True)
class Leg:
    option_type: str   # "CE" (call) or "PE" (put)
    side: str          # "SELL" (short) or "BUY" (long)
    strike: int        # rounded to the NIFTY strike grid (step=50)
    qty_lots: int = 1  # number of lots; engine multiplies by NIFTY_LOT (65) for units
```

**Engine-supplied inputs** (identical for every strategy, so the three builders are interchangeable):

| input        | meaning                                                              | source |
|--------------|---------------------------------------------------------------------|--------|
| `spot`       | NIFTY index level at entry (Black-76 forward `F ≈ spot`)             | `index_bars.close` (boundary day) |
| `atm_strike` | `_round_to_step(spot, step)` — nearest strike to spot               | derived |
| `step`       | strike grid spacing = **50** for NIFTY                               | constant |
| `dte`        | calendar days to expiry                                              | cycle |
| `sigma`      | annualised ATM straddle IV fraction (e.g. 0.14)                     | `India VIX/100` proxy |
| `params`     | per-strategy `@dataclass` (below)                                   | caller |

**Implied move** (the placement unit for OTM short strikes):
`implied_move = implied_move(spot, sigma, dte) = spot · sigma · √(dte/365)` (index points).

**Wing-width convention (shared).** Each `*_wing_width` param accepts EITHER absolute index points
OR a multiple of `implied_move`, selected by a boolean `wing_in_move_units`:

```python
def _wing_points(wing_width: float, implied_move: float, in_move_units: bool, step: int) -> int:
    raw = wing_width * implied_move if in_move_units else wing_width
    # wings are a strike DISTANCE → round the distance to the grid, min one step
    return max(step, _round_to_step(raw, step))
```

All strikes are produced by `_round_to_step(value, step)` (round-half-up, returns `int`), exactly
as in `fno_condor.py`. Builders never emit an off-grid strike.

**Pricing / credit (shared, reuses Black-76 from `fno_condor.py`).** For a leg list,
per-unit premium of each leg = `black76_call/put(F=spot, K=strike, T=dte/365, sigma)`.

```
credit_per_unit = Σ(short leg premiums)  −  Σ(long leg premiums)
```

Slippage is applied per leg as in the reference: short legs fill at `premium − slippage(premium)`
(receive less), long legs fill at `premium + slippage(premium)` (pay more) — both adverse. The
slippage-adjusted `credit_per_unit_adj` is the credit of record. `credit_total = credit_per_unit_adj · NIFTY_LOT · qty_lots`.
A genuine **credit structure has `credit_per_unit_adj > 0`** (cash received at entry).

**Costs (shared).** Build the leg tuple list for `condor_costs`:
`legs = [(fill_premium_per_unit, NIFTY_LOT·qty_lots, side) for each Leg]` and pass
`exercise_intrinsic` = aggregate ITM intrinsic of the **long** legs at expiry (the legs that get
exercised in our favour and incur exercise STT). `n_legs` drives the flat ₹20/leg brokerage, so a
4-leg condor pays brokerage on 4 legs, the butterfly also 4, etc.

**Resolution = hold to expiry intrinsic (shared).** No intra-cycle stops; the cycle P&L is the
terminal payoff. For a generic leg list at settlement `S`:

```
def leg_payoff(leg, S):                       # buyer's intrinsic
    intrinsic = max(S - leg.strike, 0) if leg.option_type == "CE" else max(leg.strike - S, 0)
    return intrinsic if leg.side == "BUY" else -intrinsic   # short = we owe it

gross_per_unit = credit_per_unit_adj + Σ leg_payoff(leg, S)
gross_pnl      = gross_per_unit · NIFTY_LOT · qty_lots
net_pnl        = gross_pnl − total_costs
```

> **Settlement caveat (applies to ALL three).** NIFTY weekly options settle on the NSE **Final
> Settlement Price (FSP)** = the 30-minute VWAP of NIFTY futures, 15:00–15:30 IST. The backtest
> uses the index daily **CLOSE** as `S`. Close ≠ FSP; the gap is usually small but can be tens of
> points on volatile expiry days. A GO verdict must be re-validated against true FSP before any
> live consideration.

**SPAN-margin approximation (shared, the whole point of "defined risk").** For a fully-hedged
defined-risk spread, the broker's SPAN+exposure margin is *capped at the structure's max loss*
(you cannot lose more than max_loss, so the exchange requires no more). We therefore use:

```
margin_required ≈ max_loss        (per lot, in ₹)
return_on_margin = net_pnl / max_loss
```

This is the conservative, exact-for-defined-risk approximation. (Naked/undefined legs would need a
real SPAN scan; none of these three has an unhedged leg, so the cap holds.)

**Vol-gate condition (shared).** A cycle is traded **iff**
`gate_decision(realized_vol_20d, sigma, k) == SELL_PREMIUM`, i.e. roughly
`realized_vol_20d < k · sigma` (implied richer than realized by the `k` cushion). All three are
short-vega / short-gamma premium harvests, so they share the identical SELL_PREMIUM gate. Builders
do NOT call the gate themselves — the runner gates the cycle, then calls the builder.

**Honest caveats (shared across all three):**
- **Black-76 single-σ proxy.** One ATM IV (`sigma`) prices all legs — no volatility skew. Real
  OTM puts trade at higher IV (put skew) and OTM calls lower; this **understates** put-wing
  premium and **overstates** symmetry. Credit estimates are therefore regime-dependent and
  conservative-to-biased; the broken-wing skew logic in particular is sensitive to this.
- **σ = India VIX/100 horizon mismatch.** VIX is a 30-day implied; weekly straddles are ~5–10 DTE
  and usually richer (term structure). The Black-76 single-sigma bias is **regime-dependent and
  conservative-to-biased across ALL credit strategies (condor, fly, broken-wing)**; in
  high-skew/crisis regimes the OTM-put understatement may not be uniformly conservative — flag
  GO verdicts sourced from high-VIX cycles.
- **Daily-step entry/exit.** Entry uses boundary-day CLOSE, not next-morning open; settlement uses
  CLOSE not FSP (above).
- **No early management.** Held to expiry; no profit-target / stop-loss / roll. Real desks manage
  at ~50% of max profit, which changes the return distribution materially.

**Feasibility (shared).** All three are **single-expiry** structures (every leg on the same weekly
expiry). They are fully reconstructable from the daily-resolution Phase-0 data
(`index_bars` close + VIX proxy + synthetic weekly boundaries) → **historically backtestable today**
via the existing `cycles_from_db(mode="weekly")` path. No multi-expiry calendar data is required.

---

## 1. Iron Condor (generalized)

Short OTM put + long further-OTM put (put-spread) **and** short OTM call + long further-OTM call
(call-spread). Symmetric. Generalizes the existing `build_condor`.

### Legs (4)

```
short_put_k  = round( spot − move_mult · implied_move )          → Leg(PE, SELL, short_put_k)
long_put_k   = short_put_k − wing                                 → Leg(PE, BUY,  long_put_k)
short_call_k = round( spot + move_mult · implied_move )          → Leg(CE, SELL, short_call_k)
long_call_k  = short_call_k + wing                                → Leg(CE, BUY,  long_call_k)
```
where `wing = _wing_points(wing_width, implied_move, wing_in_move_units, step)` (same width both sides).
Round each strike with `_round_to_step(·, step)`.

### Credit / max loss / breakevens

- **Entry credit:** `credit = (sp + sc) − (lp + lc)` per unit (slippage-adjusted), **> 0**.
- **Max loss (defined):** `max_loss_per_lot = (wing − credit_per_unit_adj) · NIFTY_LOT`, clamped ≥ 0.
  (Width identical on both sides → single `wing` term; matches `fno_condor.py`.)
- **Max profit:** `credit · NIFTY_LOT`, kept when `short_put_k ≤ S ≤ short_call_k`.
- **Breakevens:** `lower_BE = short_put_k − credit_per_unit_adj`,
  `upper_BE = short_call_k + credit_per_unit_adj`.
- **SPAN ≈ max_loss** → `return_on_margin = net_pnl / max_loss`.

### Params

```python
@dataclass(frozen=True)
class IronCondorParams:
    move_mult: float = 1.5          # short strikes at ATM ± move_mult · implied_move
    wing_width: float = 100.0       # wing distance (points, or × implied_move if wing_in_move_units)
    wing_in_move_units: bool = False
    lot: int = 1                    # qty_lots
    k: float = 0.9                  # vol-gate threshold (runner-level)
```

---

## 2. Iron Butterfly

Short ATM straddle (short ATM call + short ATM put, both at `atm_strike`) hedged by long OTM wings
(long put below, long call above) at `atm_strike ± wing`. Higher credit, **narrower** profit zone
than a condor (single profit peak at ATM vs the condor's plateau).

### Legs (4)

```
short_put_k  = atm_strike                                         → Leg(PE, SELL, atm_strike)
short_call_k = atm_strike                                         → Leg(CE, SELL, atm_strike)
long_put_k   = atm_strike − wing_strikes × step                  → Leg(PE, BUY,  long_put_k)
long_call_k  = atm_strike + wing_strikes × step                  → Leg(CE, BUY,  long_call_k)
```
where `wing_strikes` is an integer (default 4) and `step = 50` → default wing = 200 pts.
The long legs land on exact grid strikes (integer × step guarantees no rounding). Both short legs sit
on `atm_strike` (this is what makes it a butterfly, not a condor).

> **Wing parameter note.** The code's `build_iron_fly` uses `wing_strikes: int` (integer number
> of steps, default **4** → 200 pts for NIFTY at step=50) to place the long wings. This is
> different from the `wing_width`/`wing_in_move_units` float API used by the iron condor and
> broken-wing builders. The iron fly wing is always grid-exact (no sub-step rounding needed
> because the distance is already an integer multiple of `step`).

### Credit / max loss / breakevens

- **Entry credit:** `credit = (short_put + short_call) − (long_put + long_call)` per unit, **> 0**.
  Strictly **larger than the condor's** for the same wing (ATM options are the richest), at the cost
  of a far narrower break-even band.
- **Wing in points:** `wing_pts = wing_strikes × step` (e.g. default 4×50 = 200 pts).
- **Max loss (defined):** `max_loss_per_lot = (wing_pts − credit_per_unit_adj) · NIFTY_LOT`, clamped ≥ 0.
  Identical algebra to the condor — the wing distance is the same on both sides.
- **Max profit:** `credit · NIFTY_LOT`, realized **only** at `S == atm_strike` exactly (single peak).
- **Breakevens:** `lower_BE = atm_strike − credit_per_unit_adj`,
  `upper_BE = atm_strike + credit_per_unit_adj`. Band half-width = the credit itself.
- **SPAN ≈ max_loss** → `return_on_margin = net_pnl / max_loss`.

### Params

```python
@dataclass(frozen=True)
class IronButterflyParams:
    wing_strikes: int = 4           # integer number of step-widths for each wing;
                                    # wing_pts = wing_strikes × step (default 4×50=200 pts)
    lot: int = 1
    k: float = 0.9
    # No move_mult: shorts are pinned to atm_strike by definition.
    # NOTE: the code uses wing_strikes (int × step), NOT wing_width/wing_in_move_units.
    #       The long legs are placed at atm ± wing_strikes × step, always on-grid.
```

---

## 3. Broken-Wing Iron Condor

An asymmetric condor: the two wings have **different** widths so the risk is skewed to one side
(typically the put side gets the **wider** wing for cheaper / deeper downside protection, financed
by a tighter call wing). When skewed far enough, the structure can be a near-zero-cost or even
all-credit "broken wing" with no risk on the protected side.

### Legs (4)

Short strikes placed symmetrically (as in the standard condor), wings asymmetric:

```
short_put_k  = round( spot − move_mult · implied_move )          → Leg(PE, SELL, short_put_k)
short_call_k = round( spot + move_mult · implied_move )          → Leg(CE, SELL, short_call_k)
put_wing     = _wing_points(put_wing_width,  implied_move, wing_in_move_units, step)
call_wing    = _wing_points(call_wing_width, implied_move, wing_in_move_units, step)
long_put_k   = short_put_k − put_wing                            → Leg(PE, BUY,  long_put_k)
long_call_k  = short_call_k + call_wing                          → Leg(CE, BUY,  long_call_k)
```

`skew` is a convenience multiplier: when set, `put_wing_width` and `call_wing_width` are derived from
a single `base_wing` as `put = base_wing · skew`, `call = base_wing / skew` (skew > 1 ⇒ wider put
wing / downside-protected). If `put_wing_width` / `call_wing_width` are given explicitly, they win
and `skew` is ignored. Document the precedence in the builder.

### Credit / max loss / breakevens (asymmetric — the key difference)

Because the two spreads have different widths, **each side has its own max loss** and the structure's
max loss is the worse side:

- **Entry credit:** `credit = (sp + sc) − (lp + lc)` per unit. May be larger than a symmetric condor
  (the tighter wing is cheaper protection) — can even approach the wider wing's width.
- **Per-side defined risk (per unit):**
  - `put_side_loss  = put_wing  − credit_per_unit_adj`
  - `call_side_loss = call_wing − credit_per_unit_adj`
- **Max loss (defined):** `max_loss_per_lot = max(put_side_loss, call_side_loss, 0) · NIFTY_LOT`.
  The wider wing dominates. If one wing is tight enough that its side-loss ≤ 0, that side is
  **riskless** (the credit covers the whole spread there).
- **Breakevens (still one per side, but asymmetric):**
  `lower_BE = short_put_k − credit_per_unit_adj`, `upper_BE = short_call_k + credit_per_unit_adj`.
- **SPAN ≈ max_loss** (the max-of-sides) → `return_on_margin = net_pnl / max_loss`.

> **Skew caveat (in addition to the shared single-σ caveat).** The economic case for a wider put
> wing rests on **put skew** — OTM puts being richer than the flat-σ Black-76 model prices them. Our
> single-σ proxy *cannot see skew*, so it will **understate** the cost of the wide put wing and
> **overstate** the broken-wing's edge. Of the three strategies, broken-wing results are the least
> trustworthy under the Phase-0 pricing proxy and most in need of per-strike IV before any GO.

### Params

```python
@dataclass(frozen=True)
class BrokenWingCondorParams:
    move_mult: float = 1.5          # short strikes at ATM ± move_mult · implied_move
    put_wing_width: float | None = None   # explicit put-side wing (points or × move); overrides skew
    call_wing_width: float | None = None  # explicit call-side wing; overrides skew
    base_wing: float = 100.0        # used with skew when explicit widths are None
    skew: float = 1.5               # >1 ⇒ wider put wing (downside-protected); put=base·skew, call=base/skew
    wing_in_move_units: bool = False
    lot: int = 1
    k: float = 0.9
```

---

## 4. Unit-test cases

Conventions for all cases: `step = 50`, `NIFTY_LOT = 65`, `F = spot`, `T = dte/365`,
`implied_move = spot · sigma · √(dte/365)`. Strikes are exact-grid; "credit sign" and "max_loss"
are the load-bearing assertions (premiums are checked to a tolerance against `black76_*`, not exact
constants). Where a number is shown it is the rounded grid strike, computed via `_round_to_step`.

> Helper for reviewers: with `spot=22000, sigma=0.14, dte=7`,
> `implied_move = 22000·0.14·√(7/365) ≈ 22000·0.14·0.1385 ≈ 426.6` pts.
> With `spot=22000, sigma=0.12, dte=4`, `implied_move ≈ 22000·0.12·√(4/365) ≈ 22000·0.12·0.1047 ≈ 276.4` pts.

### 4.1 Iron Condor

| # | inputs (spot, sigma, dte, params) | expected legs | credit sign | max_loss |
|---|-----------------------------------|---------------|-------------|----------|
| IC-1 | 22000, 0.14, 7, move_mult=1.5, wing_width=100 | move≈426.6 → 1.5·move≈640 → SP=`21350`, SC=`22650`; LP=`21250`, LC=`22750` | credit > 0 | `(100 − credit_pu)·65 > 0` |
| IC-2 | 22000, 0.14, 7, move_mult=1.0, wing_width=100 | 1.0·move≈426.5 → round-half-up: 22000−426.5=21573.5→`21550`; 22000+426.5=22426.5→`22450`. SP=`21550`, SC=`22450`, LP=`21450`, LC=`22550` | credit > 0, **> IC-1** (shorts closer to ATM) | `(100 − credit_pu)·65`, smaller than IC-1 (more credit) |
| IC-3 | 22000, 0.14, 7, move_mult=1.5, wing_width=0.25, wing_in_move_units=True | wing≈0.25·426.6≈107→`100`; same SP/SC as IC-1 | credit > 0 | `(100 − credit_pu)·65` |
| IC-4 | 22000, 0.30, 7, move_mult=1.5, wing_width=50 | high σ → move≈914 → SP=`20650`, SC=`23350`, LP=`20600`, LC=`23400` | credit > 0 but small (far OTM) | `max(0, (50 − credit_pu))·65`; assert clamp ≥ 0 path reachable |
| IC-5 | 22000, 0.14, 7, lot=3, defaults | strikes as IC-1; qty_lots=3 on every leg | credit > 0 | `(100 − credit_pu)·65·3` (scales with lots) |
| IC-6 | 22000, 0.14, 7, defaults | n_legs == 4; option_types == {PE,PE,CE,CE}; sides == {SELL,BUY,SELL,CE-pair} | n/a (structure check) | LP < SP < SC < LC ordering holds |
| IC-7 | resolve: strikes from IC-1, S = 22000 (inside shorts) | — | — | `gross == credit·65` (full credit kept; max profit) |
| IC-8 | resolve: strikes from IC-1, S = 21000 (below long put) | — | — | `gross == −max_loss` (worst case = defined floor) |

### 4.2 Iron Butterfly

| # | inputs | expected legs | credit sign | max_loss |
|---|--------|---------------|-------------|----------|
| IB-1 | 22000, 0.14, 7, wing_strikes=2 (→ 100 pts) | atm=`22000`; SP=SC=`22000`; LP=`21900`, LC=`22100` | credit > 0 | `(100 − credit_pu)·65` |
| IB-2 | 22037, 0.14, 7, wing_strikes=2 | atm rounds to `22050`; SP=SC=`22050`, LP=`21950`, LC=`22150` | credit > 0 | `(100 − credit_pu)·65` (assert ATM rounding) |
| IB-3 | 22000, 0.14, 7, wing_strikes=4 (→ 200 pts, the default) | SP=SC=`22000`, LP=`21800`, LC=`22200` | credit > 0 | `(200 − credit_pu)·65`, larger floor than IB-1 |
| IB-4 | 22000, 0.14, 7 — compare butterfly (wing_strikes=2 → 100 pts) vs IC-1 condor (wing_width=100) | both shorts on ATM for fly vs OTM for condor | **butterfly credit > condor credit** (ATM richest) | butterfly `max_loss < condor max_loss` (more credit, same wing pts) |
| IB-5 | resolve: IB-1 strikes, S = 22000 | — | — | `gross == credit·65` (peak profit only at exact ATM) |
| IB-6 | resolve: IB-1 strikes, S = 22050 (one step above ATM, inside upper wing) | — | — | `gross < credit·65` and `> −max_loss` (partial loss; narrow band) |
| IB-7 | resolve: IB-1 strikes, S = 21800 (≤ long put) | — | — | `gross == −max_loss` (defined floor) |

> **IB note on removed test case.** A former IB-5 tested `wing_width=0.5, wing_in_move_units=True`
> for the iron fly. This param API does **not exist** in `build_iron_fly` (the code uses
> `wing_strikes: int`, not the float `wing_width/wing_in_move_units` API). That test case was
> removed as unreachable. If you need 200-pt wings, use `wing_strikes=4` (default).

### 4.3 Broken-Wing Iron Condor

| # | inputs | expected legs | credit sign | max_loss |
|---|--------|---------------|-------------|----------|
| BW-1 | 22000, 0.14, 7, base_wing=100, skew=2.0 | put_wing=200, call_wing=50; SP=`21350`, SC=`22650`; LP=`21150`, LC=`22700` | credit > 0 | `max(200−c, 50−c, 0)·65 = (200−c)·65` (put side dominates) |
| BW-2 | 22000, 0.14, 7, put_wing_width=150, call_wing_width=50 (explicit) | SP=`21350`, SC=`22650`, LP=`21200`, LC=`22700`; skew ignored | credit > 0 | `(150 − c)·65` (put side wider) |
| BW-3 | 22000, 0.14, 7, base_wing=100, skew=1.0 | symmetric → put_wing=call_wing=100 | credit > 0 | `(100 − c)·65` — **equals the symmetric IC-1 max_loss** (degenerate check) |
| BW-4 | 22000, 0.14, 7, base_wing=100, skew=0.5 | put_wing=50, call_wing=200 (call side wider) | credit > 0 | `(200 − c)·65` (call side dominates — skew<1 inverts) |
| BW-5 | 22000, 0.30, 7, put_wing_width=50, call_wing_width=50, move_mult=1.5 | far-OTM shorts (σ high), tight equal wings | credit > 0, small | side losses equal → `(50 − c)·65`; assert max-of-sides == both |
| BW-6 | 22000, 0.14, 7, put_wing_width=20 (≈ rounds to one step 50), call_wing_width=300 | tight put side: if `(50 − c) ≤ 0` → put side **riskless** | credit > 0 | `max(50−c, 300−c, 0)·65 = (300−c)·65`; if 50−c≤0 assert put_side_loss clamped to 0 |
| BW-7 | resolve: BW-1 strikes, S = 22000 (inside shorts) | — | — | `gross == credit·65` (full credit) |
| BW-8 | resolve: BW-1 strikes, S = 21000 (below long put, wide side) | — | — | `gross == −(200−c)·65` (loss = the wider, dominant side) |

---

## 5. Implementation notes for the coder

- All three builders are pure (no DB, no network) and reuse `_round_to_step`, `black76_call/put`,
  `slippage`, and `condor_costs` from the existing modules — do not re-implement pricing.
- The runner gates the cycle once (`gate_decision`), computes `implied_move` once, then calls the
  selected builder; builders receive the precomputed `implied_move` (or `sigma`+`dte` to compute it)
  per the generalized signature.
- Max-loss algebra differs only for broken-wing (max-of-sides); condor and butterfly share the
  symmetric `(wing − credit)·lot·NIFTY_LOT` form.
- `return_on_margin = net_pnl / max_loss` with `margin_required = max_loss` (SPAN cap for
  defined-risk). Guard `max_loss == 0` (all-credit degenerate / clamp) → report `inf` or skip.
- Keep the FSP-vs-close and single-σ/skew caveats in the strategy docstrings so the go/no-go report
  inherits them.
