# 07 — Cost & Slippage Realism for Option Scalping

**Scope:** Define, honestly and conservatively, the *full* per-scalp cost stack for the
intraday NIFTY long-option scalper (`strategies/options_scalper.py`), with the dominant
real-world cost — the option **bid-ask spread paid on every entry AND exit** — modeled
explicitly. End with a break-even and expected-value (EV) analysis that sets the bar the
*entry edge must clear* before this strategy can be promoted past PAPER.

> **Why this document is the gate.** Costs and spread are what kill retail option
> scalpers. A signal that "works" on mid-price will reliably lose money once you pay the
> spread on both legs of every trade plus statutory charges and impact. This file
> quantifies that drag so §07's break-even number becomes a hard threshold for any
> entry-edge claim in the other specs. If the measured edge does not clear it, do not ship.

**Source of truth for rates:** `research/backtest/fno_costs.py` (post-April-2026 NSE/SEBI
stack). **Source of truth for contract mechanics:** `ScalperParams` in
`strategies/options_scalper.py` (`lot = 65`, `tp_ladder_pct`, `stop_pct`, `tranche_lots`,
`max_rungs`). This doc must stay numerically consistent with both; if a rate changes there,
re-run the arithmetic here.

---

## 0. Conventions

- **One "scalp"** = a single tranche round trip: BUY 1 lot of one ATM option, then SELL
  that same lot. The ladder (`max_rungs=3`) is just several scalps stacked; cost scales
  ~linearly per tranche, so per-tranche economics are the unit of analysis.
- **Lot size:** `NIFTY_LOT = 65` units (from `fno_costs.py` / `ScalperParams.lot`). One lot
  is the smallest tradeable quantity.
- **Premium `P`** = option price per unit (₹). For ATM NIFTY weekly options, `P` is
  typically **₹80–₹150** intraday depending on time-to-expiry and IV; we use **`P = ₹100`**
  as the running example (one lot notional premium = `100 × 65 = ₹6,500`).
- **All figures are PER LOT** unless stated. Multiply by lots for ladder totals.
- **Point conversion:** for an ATM option, Δpremium ≈ delta × Δunderlying. ATM delta ≈ 0.5,
  so **1 NIFTY point ≈ ₹0.5 of premium per unit** near the money. This lets us translate
  "premium move needed" into "NIFTY points needed," which is how the entry edge is stated.

---

## 1. The statutory + brokerage cost stack (per round trip, per lot)

These come straight from `research/backtest/fno_costs.py`. The scalper closes in the market
(it never holds to expiry / exercise), so **`exercise_intrinsic = 0`** — there is no
exercise STT. Each round trip = **2 executed orders** (1 BUY entry, 1 SELL exit).

Rates (post-April-2026):

| Component | Rate | Applied to |
|---|---|---|
| Brokerage | ₹20 flat / executed order | both legs → ₹40 |
| STT (sell) | **0.15%** of premium | **SELL leg only** |
| NSE txn fee | 0.03553% (₹35.53/lakh) | total premium turnover (both legs) |
| SEBI fee | ₹10/crore (0.000001) | total turnover |
| Stamp duty | 0.003% (0.00003) | **BUY leg only** |
| GST | 18% | (brokerage + exchange fee + SEBI fee) |

> NOTE: `OPTION_EXCHANGE_PCT` (0.03553%) carries a `[verify-me]` flag in `fno_costs.py` —
> confirm the live NSE circular before any go/no-go. STT-sell at 0.15% reflects the
> Apr-2026 hike (was 0.10%).

### Worked example — `P = ₹100`, 1 lot (65 units), entry = exit = ₹100

Turnovers: BUY = SELL = `100 × 65 = ₹6,500`; total turnover = `₹13,000`.

| Component | Calculation | ₹ |
|---|---|---|
| Brokerage | ₹20 × 2 | 40.00 |
| STT (sell) | 0.0015 × 6,500 | 9.75 |
| Exchange fee | 0.0003553 × 13,000 | 4.62 |
| SEBI fee | 0.000001 × 13,000 | 0.013 |
| Stamp duty | 0.00003 × 6,500 | 0.195 |
| GST | 0.18 × (40 + 4.62 + 0.013) | 8.03 |
| **Statutory + brokerage total** | | **≈ ₹62.6** |

So the **fixed/statutory drag ≈ ₹62.6 per lot per round trip** (at `P=₹100`). In premium
terms that is `62.6 / 65 ≈ ₹0.96 per unit`, i.e. the option must move **~₹0.96/unit (~1.9
NIFTY points)** *just to cover charges* — before spread.

The **dominant term is brokerage + its GST (₹48 of the ₹62.6)** — it is a *fixed* ₹40+GST
per round trip regardless of premium. This is why scalping with small premiums / 1 lot is
brutal: a fixed ₹~48 floor amortized over only 65 units.

---

## 2. The dominant cost: the option BID-ASK SPREAD (paid on entry AND exit)

The statutory stack above is the *small* part. The **bid-ask spread** is what actually
kills the scalper, and `fno_costs.py` only models it as a flat `slippage()` of 0.5% of
premium. That is **optimistic** for a market-taking scalper. Model it explicitly here.

### 2a. How spread is paid

A long-option scalper **takes liquidity on both ends** (it needs to be in/out fast):

- **Entry (BUY):** lifts the **ask** → pays `mid + spread/2`.
- **Exit (SELL):** hits the **bid** → receives `mid − spread/2`.
- **Round-trip spread cost = full spread `S` per unit** (you give up half on each side).

This is *separate from and additional to* the statutory stack. It is the difference between
the backtest mid-price fill (a fiction) and the real fill.

### 2b. Realistic ATM NIFTY weekly spreads

NIFTY is among the most liquid option chains in the world, but a *scalper trades the worst
moments* — the move it chases is exactly when the spread widens. Honest assumptions:

| Regime | ATM weekly spread `S` (₹/unit) | As % of `P=₹100` |
|---|---|---|
| Calm, mid-session, deep ATM | ₹0.30 – ₹0.50 | 0.3% – 0.5% |
| Normal intraday (base case) | **₹0.50 – ₹1.00** | **0.5% – 1.0%** |
| Around the move / fast tape / open / event | ₹1.50 – ₹3.00+ | 1.5% – 3%+ |
| Near-expiry afternoon (low premium, jumpy) | ₹1.00 – ₹2.00 on `P≈₹40` | 2.5% – 5% |

**Base case for the model: `S = ₹1.00 per unit` round-trip** (0.5% of mid each side at
`P=₹100`). This is *deliberately conservative-realistic*, not optimistic — a scalper firing
on momentum (`mom_thresh`, ATR filter active) is trading the wide-spread regime more often
than the calm one.

> The `fno_costs.py` default `slippage(pct=0.005)` = ₹0.50 per leg = ₹1.00 round trip at
> `P=₹100`, which matches this base case **only if interpreted as per-leg**. The scalper
> backtest MUST apply slippage on BOTH legs (entry and exit). Applying it once is a silent
> 2× understatement of the dominant cost.

### 2c. Spread cost in ₹ per lot

`Spread cost per lot = S × 65`.

| `S` (₹/unit round-trip) | ₹ per lot |
|---|---|
| 0.50 | 32.5 |
| **1.00 (base)** | **65.0** |
| 2.00 | 130.0 |
| 3.00 | 195.0 |

### 2d. Market impact (the third cost, often ignored)

The strategy ladders up to `tranche_lots × max_rungs` lots. For 1–3 NIFTY lots ATM weekly,
**impact is ~0** (depth easily absorbs it). It becomes real only if size grows:

- ≤ 3 lots ATM weekly: impact ≈ 0 (ignore).
- 10+ lots, or the move/event regime, or 2nd/3rd rung added *into* a fast move: add
  **+0.25–0.5 spread (₹0.25–0.50/unit)** of additional impact, because rungs are added
  precisely when the book is thin (pyramiding into momentum).

**Model rule:** impact = 0 for ≤3 lots in normal regime; otherwise add `0.5 × S` per lot.
For this spec's base case (small size), **impact = 0**.

---

## 3. Total all-in cost per scalp (round trip, per lot)

`Total = statutory_stack + spread_cost + impact`

Base case (`P = ₹100`, 1 lot, `S = ₹1.00`, impact = 0):

| Bucket | ₹ per lot | Share |
|---|---|---|
| Statutory + brokerage (§1) | 62.6 | 49% |
| Bid-ask spread (§2, round-trip) | 65.0 | 51% |
| Impact (§2d) | 0.0 | 0% |
| **All-in per scalp** | **≈ ₹127.6** | 100% |

**In premium terms:** `127.6 / 65 ≈ ₹1.96 per unit`.

**The headline number:** at `P=₹100`, **the option premium must rise ~₹1.96/unit just to
break even on one scalp.** Note that **spread (51%) ≈ brokerage+statutory (49%)** — the two
are comparable in size at 1 lot, and spread *dominates* as soon as you assume the realistic
move-regime spread (`S=₹2`) or count it on both legs.

### Sensitivity table — all-in ₹ per lot per scalp

| | `S=₹0.50` | `S=₹1.00` (base) | `S=₹2.00` | `S=₹3.00` |
|---|---|---|---|---|
| **`P=₹60`** (near expiry) | 89 | 122 | 187 | 252 |
| **`P=₹100`** (base) | 95 | **128** | 193 | 258 |
| **`P=₹150`** | 104 | 137 | 202 | 267 |

(Statutory scales mildly with `P` via the %-of-turnover terms; the fixed ₹48 brokerage+GST
floor dominates the low end. Spread scales with `S` only, not `P`, in absolute ₹/unit — but
near expiry the *same* ₹ spread is a far larger % of a small premium.)

---

## 4. Break-even analysis — how far must the option move?

**Break-even premium move (₹/unit) `= Total_cost_per_lot / 65`.**

| Scenario | All-in ₹/lot | BE premium move (₹/unit) | BE in NIFTY points¹ |
|---|---|---|---|
| Optimistic (`P=100, S=0.50`) | 95 | 1.46 | **~2.9 pts** |
| **Base (`P=100, S=1.00`)** | **128** | **1.96** | **~3.9 pts** |
| Move-regime (`P=100, S=2.00`) | 193 | 2.97 | **~5.9 pts** |
| Near-expiry jumpy (`P=60, S=2.00`) | 187 | 2.88 | **~5.8 pts** |

¹ Using ATM delta ≈ 0.5 → 1 NIFTY pt ≈ ₹0.5 premium. **Near expiry delta drifts and gamma
spikes, so the points-needed is noisier — treat the points column as indicative.**

### Compare against the strategy's own TP/stop ladder

`ScalperParams` defaults: `tp_ladder_pct = [0.10, 0.20, 0.35]`, `stop_pct = 0.20`.

At `P=₹100`:
- **TP[0] = +10% = +₹10/unit = ₹650/lot gross.** Net of base cost (₹128): **+₹522/lot.**
- **Stop = −20% = −₹20/unit = −₹1,300/lot gross.** Net of base cost: **−₹1,428/lot.**
- **Break-even (cost only) = +1.96% premium move.** So TP[0] (+10%) clears cost ~5×; that
  is healthy headroom **per winning scalp**. The problem is never the size of a single win
  — it is the *frequency of small losses + spread bleed on every entry that never reaches
  TP[0]* (signal-flip exits, time-stops, trail-stops near entry).

> **The silent killer:** every ladder rung that opens and is closed *near entry* (time-stop
> at `time_stop_min=12`, signal-flip flatten, or a trail that triggers just above cost) pays
> the full ~₹128/lot all-in and earns ~₹0 of premium move. A 50% "win rate" where the
> losers are full stops and the small chop-outs each cost ₹128 will bleed even with a
> generous TP.

---

## 5. Expected-value (EV) math — the bar the entry edge must clear

Define per-scalp outcomes in **₹ per lot**, base case cost `C = ₹128`:

- Win: hits TP[0] (+10%) on average → gross **+₹650**, net `W = 650 − 128 = +₹522`.
- Loss: hits stop (−20%) → gross **−₹1,300**, net `L = −(1300 + 128) = −₹1,428`.
- (Real distributions also have partial-TP wins, trail exits, and chop-outs ≈ −₹128; the
  two-outcome model below is a *clean upper bound* on how good a fixed TP/stop edge looks.)

**EV per scalp** with win probability `p`:

```
EV = p·W + (1−p)·L = p·522 + (1−p)·(−1428)
```

**Break-even win rate (EV = 0):**

```
p* = |L| / (W + |L|) = 1428 / (522 + 1428) = 1428 / 1950 ≈ 0.732
```

> **You need ≈ 73% win rate** just to break even with the default +10% TP / −20% stop
> ladder, because the stop risks 2× the first TP. **That is a very high bar for an intraday
> momentum signal.** This is the single most important number in this document.

### What changes the bar

The payoff ratio `b = W/|L|` is unfavorable (≈ 0.37) *by construction* of the
`stop_pct=0.20` vs `tp[0]=0.10` choice. Required win rate `p* = 1/(1+b)`:

| TP[0] | Stop | Gross W:L | Net b (after ₹128 ea.) | Required `p*` |
|---|---|---|---|---|
| +10% | −20% | 650 : 1300 | 522 : 1428 = 0.37 | **73%** |
| +15% | −20% | 975 : 1300 | 847 : 1428 = 0.59 | **63%** |
| +20% | −20% | 1300 : 1300 | 1172 : 1428 = 0.82 | **55%** |
| +20% | −15% | 1300 : 975 | 1172 : 1103 = 1.06 | **48%** |
| +35% (TP top) | −20% | 2275 : 1300 | 2147 : 1428 = 1.50 | **40%** |

**Implications for the entry edge:**
1. With the **current default ladder, the signal must win ~73% of scalps** — implausible.
   The ladder's *real* expectancy comes from the **TP ladder + trailing remainder** letting
   winners run to +20%/+35% while cutting losers, i.e. the *blended* payoff must beat 1:1,
   not the first rung.
2. **Cost is ~₹128/lot of dead weight on every scalp.** Halving the spread (limit-ish
   entries, calmer regime, fewer chop scalps) drops `C` toward ₹95 and moves `p*` from 73%
   to ~71% — *spread reduction alone is not enough*; the payoff asymmetry dominates.
3. **The lever that matters most is letting winners reach TP[1]/TP[2] and trail** (raising
   blended `b`), and **not opening scalps that get chopped out at ~−₹128** (i.e. the ATR
   filter `min_atr_pts` and `mom_thresh` must be strict enough that entries genuinely
   precede a >4-point move with >50% reliability).

### The hard threshold this spec sets

> **GO/NO-GO BAR:** Over a faithful PAPER sample (real entry+exit spreads applied on BOTH
> legs, full statutory stack, impact per §2d), the scalper's **blended net EV per scalp must
> be positive after ₹~128/lot all-in cost, AND the realized win-rate must exceed the `p*`
> implied by its realized blended payoff ratio.** A mid-price backtest that ignores
> round-trip spread is invalid and must not be used to justify promotion.

---

## 6. Modeling rules for the backtest / paper harness (binding)

1. **Charge spread on BOTH legs.** Entry fills at `ask = mid + S/2`; exit fills at
   `bid = mid − S/2`. Never fill a scalp at mid. (`fno_costs.slippage()` must be applied per
   leg, not per round trip.)
2. **Use the regime-aware spread**, not a flat 0.5%. Minimum `S = ₹1.00/unit` round-trip in
   normal regime; widen to `S = ₹2–3` for entries fired during fast tape / first 15 min /
   event windows / final-30-min near-expiry.
3. **Apply the full statutory stack via `fno_costs.condor_costs()`** with the 2-leg
   round-trip (BUY entry leg + SELL exit leg), `exercise_intrinsic=0` (scalper never holds
   to exercise).
4. **Add impact per §2d** when size > 3 lots or rungs are added into momentum.
5. **Account for chop-out scalps explicitly** — a scalp opened and closed near entry still
   pays full `C`. Do not net these to zero.
6. **Re-derive `C` if `P`, `S`, or any rate changes.** The headline `C ≈ ₹128/lot` is
   anchored to `P=₹100, S=₹1.00`; near-expiry low-premium scalps have a *higher* effective
   cost as a %-of-premium even when the ₹ figure is similar.
7. **Report cost as a % of gross P&L** in the go/no-go: if all-in cost eats > ~30–40% of
   gross winning premium, the edge is too thin to survive live spread variance.

---

## 7. Summary — the numbers to remember

- **All-in cost per 1-lot scalp ≈ ₹128** (`P=₹100`, base spread): ~half statutory/brokerage,
  ~half bid-ask spread (paid twice — entry + exit).
- **Break-even move ≈ ₹1.96/unit ≈ 3.9 NIFTY points** before any profit.
- **Spread is the dominant, regime-dependent cost** and *must* be charged on both legs at
  ≥₹1/unit round-trip; flat-0.5%-once modeling understates reality ~2×.
- **With the default +10% TP / −20% stop, break-even win rate ≈ 73%** — the entry edge bar
  is set by the *blended* payoff ladder, and the strategy is only viable if winners reach
  TP[1]/TP[2]/trail while chop-outs are rare.
- **This is the bar:** positive blended net EV per scalp after ₹~128/lot, win-rate above the
  realized-payoff `p*`, validated on a spread-on-both-legs PAPER sample. Anything claimed on
  mid-price fills is rejected.
