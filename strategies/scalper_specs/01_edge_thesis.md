# 01 — Edge Thesis: Intraday ATM NIFTY Option Scalping

**Status:** RESEARCH SPEC — honest edge analysis. PAPER only. No live order paths.
**Companion code:** `strategies/options_scalper.py` · **Companion spec:** `docs/fno_strategies/options_scalper.md`
**Audience:** the engineer/researcher deciding whether to invest further effort in this scalper.

---

## 0. The honest frame (read this first)

A scalper that **always** makes money does not exist. Anyone who claims one is either
curve-fit, ignoring costs, or lying. The question this document answers is narrower and
answerable:

> **Is there a genuine, repeatable, positive-expectancy edge in buying ATM NIFTY options
> and scalping them intraday — and if so, exactly what is the edge, and under what
> conditions does it survive the cost stack?**

Two facts constrain everything below:

1. **This strategy is currently UN-BACKTESTABLE.** The DB has **no intraday option-premium
   series** and (today) **no intraday NIFTY 1-min index bars** (see `options_scalper.md` §5).
   We will **not** synthesise an intraday premium path from daily OHLCV — interpolating or
   Brownian-bridging daily bars manufactures exactly the premium moves the strategy claims
   to trade. **GIGO.** Any "edge" derived from fabricated data is fabricated.

2. **A long-option scalper is the single hardest configuration to make net-positive.** It is
   long premium (theta works against it every minute), it crosses the option bid-ask on every
   round-trip (the widest spread in the chain after the deep wings), and it trades many times
   per day (so costs compound). It is the opposite of a structural-edge harvester. The burden
   of proof is on the edge, not on the skeptic.

This document is therefore deliberately skeptical. It does **not** conclude "this works." It
concludes "**here is the only edge that could plausibly survive, here is what would kill it,
and here is the falsifiable test that decides it** — run on real data, never fabricated."

---

## 1. Where premium P&L actually comes from (the decomposition)

A long option's intraday P&L per unit, over a short holding interval `Δt`, decomposes
(first-order Greeks) as:

```
ΔPremium ≈ Δ · ΔS                 (delta — directional move in the underlying)
         + ½·Γ · (ΔS)²            (gamma — convexity; always helps a long option)
         + Vega · Δσ              (vega — IV change over the interval)
         − Θ · Δt                 (theta — time decay; always hurts a long option)
         − spread/2 each side     (microstructure — you pay to enter AND to exit)
```

A scalper that **buys** options is structurally:
- **Long delta** in the direction it picked (CE = long, PE = short the underlying).
- **Long gamma** (convexity is its friend; big fast moves pay super-linearly).
- **Long vega** (an IV pop helps; an IV crush hurts).
- **Short theta** (bleeding every minute it holds).
- **Short the spread** (paying microstructure on every entry and every exit).

The **only** terms that can be a positive edge are `Δ·ΔS` (you predicted direction) and
`½Γ(ΔS)²` (the move was large/fast). Everything else (`−Θ·Δt`, spread) is a **guaranteed
drag**, and vega is a **coin-flip you don't control intraday**. So the entire edge thesis
reduces to one claim:

> **Can the strategy capture enough favorable `Δ·ΔS + ½Γ(ΔS)²` to overcome `Θ·Δt` + the
> spread paid twice + brokerage/STT/fees, on average, across many scalps?**

If yes → positive EV. If no → it is a theta-and-spread donation machine. There is no third
outcome. The rest of this document evaluates the candidate edges (the `ΔS` side) against the
headwinds (the drag side).

---

## 2. Candidate edges — is there directional `ΔS` to capture?

The signal layer (`options_scalper.py` §1) offers four direction models. Each rests on a
different claim about intraday underlying predictability. Evaluate each honestly.

### 2a. Short-term momentum / gamma continuation (`"momentum"`, `"vwap_mom"`)

**The claim:** intraday NIFTY exhibits **short-horizon return autocorrelation** — a move in
the last `k` minutes tends to continue for the next few minutes (order-flow imbalance,
delta-hedging feedback, retail momentum chasing, index-rebalance pressure).

**Is the claim real?** Partially, and **conditionally**:
- Intraday index futures/spot **do** show weak positive autocorrelation at the 1–5 min
  horizon during **trending regimes** (large directional days, post-event drift, strong
  global-cue opens). This is documented across index-futures microstructure literature and is
  the basis of every momentum-ignition desk.
- But the same series shows **negative autocorrelation (mean-reversion)** in **range-bound
  regimes**, which are the majority of NIFTY sessions. A naive momentum model in a mean-
  reverting tape is a cost-bleed machine — it buys the top of every micro-swing.
- The autocorrelation, where it exists, is **tiny** (a few bps of edge per signal). Tiny edge
  + large cost = the central problem.

**The gamma kicker (the genuinely interesting part):** because the position is **long gamma**,
the edge is **asymmetric in move size**. A long option doesn't need to be right *often* — it
needs to be right *big occasionally*. If the momentum signal catches even a minority of the
day's large, fast moves (the `(ΔS)²` term dominates on those), the convexity can pay for many
small losing scalps. **This is the strongest theoretical edge in the whole strategy:** not
"predict direction accurately" but "**be positioned long-gamma in the right direction when a
large fast move happens, and cut quickly when it doesn't.**" The trailing-remainder exit
(`options_scalper.py` §2b) is precisely the mechanism that monetises the convex tail; the
time-stop and hard-stop are precisely the mechanisms that cap the theta/spread bleed on the
losers.

**Verdict:** Plausible edge, but it is a **convexity/trend-capture** edge, NOT a "high win-
rate scalp" edge. It will have a **low win rate and positive expectancy driven by a fat right
tail**, or it has nothing. Any version optimised for a high win rate (tight TPs, no trailing)
will harvest theta on the losers and cap the winners — guaranteed negative EV.

### 2b. ORB-style underlying breakouts (`"orb"`)

**The claim:** a break of the opening-range high/low predicts a directional trend day; buy
the option in the breakout direction and ride the trend with the ladder.

**Is the claim real?** ORB has **documented positive expectancy on the underlying** in some
markets/regimes (it is literally the live equity strategy in this repo, `strategies/orb.py`).
The intraday F&O variant inherits that directional thesis. The breakout selects for exactly
the regime the long-gamma position wants (a fast directional move), which is **complementary**
to the convexity argument in §2a.

**The problems specific to options:**
- ORB gives **~1 signal per day per side** — far too few for a "scalper." This model is really
  a *directional intraday option position*, not a scalp. (The spec acknowledges this:
  `options_scalper.md` §1a — "too few signals for a scalper.")
- **False breakouts** are common; on a fakeout you are long premium into a reversal, eating
  delta loss + theta + spread. The hard stop and signal-flip exit must be tight.
- **Timing of the break matters enormously for theta:** an early break (09:30) with a full day
  of trend ahead is the dream; a late break (14:00) has little runway and pays theta into the
  close.

**Verdict:** The *cleanest directional thesis* of the four, and regime-aligned with the long-
gamma edge — but it is a **position strategy, not a scalp**, and its viability is essentially
"does ORB-on-NIFTY work" overlaid with "does the option structure preserve the underlying
edge after costs." Worth testing as the **low-turnover benchmark** that the high-turnover
scalp must beat.

### 2c. Mean-reversion (NOT a default signal — evaluated for completeness)

**The claim:** intraday NIFTY reverts to VWAP / the day's mean; fade extensions.

**Why it is dangerous for a LONG-option scalper specifically:** mean-reversion edges have
**bounded upside and unbounded-ish downside** (you fade until you're run over). On the
underlying that is survivable with a stop. **Buying an option to express mean-reversion is
doubly wrong:** you are long gamma (which *wants* big moves) while betting *against* big moves
(reversion = small moves back to mean). You pay theta + spread to express a thesis whose best
case is a small, slow move — the exact move that maximises theta drag relative to the payoff.
Mean-reversion is the **natural home of the option SELLER**, not the buyer.

**Verdict:** Structurally mismatched to long premium. **Do not build the long-option scalper
around mean-reversion.** (If reversion is the real intraday edge on NIFTY, the correct vehicle
is a defined-risk *short*-premium structure — see the sibling specs `premium_sell_undefined.md`
/ `verticals.md` — not this scalper.) Note this asymmetry explicitly so no one "improves" the
scalper by adding reversion entries.

### 2d. Summary of the directional edge

| Candidate | Edge real? | Aligned with long-gamma? | Role |
|---|---|---|---|
| Momentum / VWAP-mom | Weak, regime-dependent | **Yes** (catches fast moves) | Primary edge candidate — convexity capture |
| ORB breakout | Yes, in trend regimes | **Yes** | Low-turnover directional benchmark |
| Mean-reversion | Maybe on underlying | **No — inverted** | Wrong vehicle; exclude from this strategy |

**The honest synthesis:** the only directional edge worth chasing here is **trend/momentum
capture monetised through long gamma** — being positioned long-delta-long-gamma when a fast
directional move occurs, with ruthless theta/spread control on the (majority) non-moves.

---

## 3. The structural headwinds (what kills option scalpers)

These are not risks — they are **certainties** that the edge in §2 must out-earn. Quantified
roughly for ATM weekly NIFTY (premium ~₹100–150, lot 65).

### 3a. The bid-ask spread — paid on EVERY round-trip, twice

This is the **dominant killer**. ATM NIFTY weeklies are liquid but still quote a spread of
typically **₹0.5–₹2.0** per unit (worse off-hours, around events, and away from the round
ATM). A scalper **crosses the spread to enter AND to exit** — so the spread cost per round-
trip is roughly the **full quoted spread** per unit (half on each side, twice).

- At ₹1.0 spread/unit × 65 = **₹65/lot per round-trip in spread alone.**
- Against a +10% target on a ₹120 premium = ₹12/unit = ₹780/lot gross, the spread is **~8% of
  the target gone before any other cost.**
- The further from ATM, the wider the relative spread — which is exactly why the spec defaults
  to **ATM** (`strike_offset=0`): tightest spread = least slippage, and **slippage is the
  whole game** (`options_scalper.md` §1d).

### 3b. Theta bleed — the clock is always against you

A long option loses value every minute. For ATM NIFTY weeklies, intraday theta is small early
in the week but **accelerates brutally on expiry day** and through the afternoon. The
`time_stop_min` (default 12 min) exists *solely* to cap this: a scalp that hasn't moved in
favor within ~12 min is, on average, a slow theta loss and must be cut. **Theta makes "wait
and see" a negative-EV action** — the opposite of equity scalping where waiting is free.

**Critical regime note:** **expiry-day** scalping has the most gamma (good) but the most theta
(bad) and the worst pin-risk near the close. The two effects fight. Early-week ATM has gentle
theta but also less gamma. There is no free lunch — the edge has to be located in a specific
(DTE × time-of-day × regime) cell, not assumed uniform.

### 3c. Slippage beyond the quoted spread

Market orders on a fast-moving ATM option (exactly when you most want to enter/exit) get
filled **worse than the displayed quote** — the book thins as price moves. The spec's
`fno_costs.slippage` defaults to 0.5% for liquid mids but recommends **1%+ for small-target
scalping** (`options_scalper.md` §5d) precisely because you cross a moving spread both ways.
On a +10% (₹12) target, 1% slippage each side on a ₹120 premium = ₹1.2 × 2 = ₹2.4/unit =
**₹156/lot — 20% of the gross target.**

### 3d. Brokerage, STT, exchange/SEBI/stamp, GST — the fixed/percentage stack

Per round-trip (a BUY leg + a SELL leg):
- **Brokerage:** 2 × ₹20 = **₹40 flat per round-trip** regardless of size — this is why
  1-lot scalps are inefficient and why the `max_trades`/`max_rungs` caps matter.
- **STT:** sell-side ~0.0625% of premium notional (on options sell; rates change — use
  `fno_costs`).
- **Exchange txn charge, SEBI fee, stamp duty, GST on (brokerage + txn).**

Combined, the non-spread cost stack is on the order of **₹50–₹90 per round-trip per lot** for
small ATM premiums — a meaningful fraction of a small target.

### 3e. The aggregate break-even (the number that decides everything)

Stack §3a–§3d for one 1-lot ATM round-trip at premium ~₹120:

```
Spread (round-trip)     ~₹65/lot
Slippage (1% each side) ~₹156/lot
Brokerage               ~₹40/lot
STT + fees + GST        ~₹20–40/lot
─────────────────────────────────
TOTAL DRAG              ~₹280–300/lot per round-trip
```

Gross value of a **+10% scalp** = ₹780/lot. **Net ≈ ₹480–500/lot — IF the target is hit.**
But the win rate is not 100%; losers pay the **same ~₹280–300 drag plus the premium loss**
(up to the −20% stop = −₹156 premium + drag ≈ **−₹440/lot**). So a single rough expectancy
sketch:

```
EV/scalp ≈ p_win · (gross_win − drag) − p_loss · (gross_loss + drag)
```

To break even with the numbers above you need roughly **p_win ≳ 0.48–0.50 at the +10%/−20%
geometry** *before* the trailing-tail upside — i.e. you must be **right about as often as a
coin flip just to pay the costs**, and the actual edge has to come from the **trailing
remainder catching the fat-tailed big moves** (§2a). **This is the make-or-break sensitivity.**
A 5-percentage-point error in win rate, or a doubling of assumed slippage, flips the sign.

> **The backtest/forward report MUST prominently show: (1) net-of-cost per-scalp expectancy,
> (2) the assumed slippage, and (3) the break-even win rate at the assumed cost geometry.**
> Any result that omits these is meaningless (`options_scalper.md` §5d).

---

## 4. Is a positive-EV version plausible? — the conditions

A positive-EV version is **plausible but narrow**, and **only** under the conjunction of all
of the following. Miss any one and the edge is gone.

**C1 — The edge is convexity, not accuracy.** The strategy must be designed to **lose small
often and win big occasionally**, monetising the long-gamma tail via the trailing remainder.
Any tuning toward a high win rate (tight uniform TPs, no trail) caps the only positive term
and guarantees theta+spread bleed. *(Validates the §2b/§2a synthesis.)*

**C2 — Trade only when the underlying is actually moving.** The ATR activity filter
(`min_atr_pts`) and the VWAP deadband must genuinely suppress entries on flat/chop tape. The
majority of NIFTY sessions are range-bound; scalping them is a guaranteed donation. The
strategy must be **inactive most of the time** and only fire on the minority of trending/
volatile windows. *(A scalper that trades all day is dead — overtrading flat tape is the #1
killer, `options_scalper.md` §1b.)*

**C3 — ATM only, weekly, liquid window.** Off-ATM widens the relative spread past the target;
illiquid windows (first 15 min, last 20 min, lunch lull) blow slippage. The trade window and
ATM default exist for this reason and must hold.

**C4 — The favorable move must clear the full cost stack with margin.** Targets and stops must
be sized so the **average winning scalp nets meaningfully positive after ~₹280–300/lot drag**
(§3e). If the realistic average move per signal is ~10–15 NIFTY points and ATM delta ≈ 0.5,
that's ~5–7.5 premium points ≈ ₹325–490/lot gross — **uncomfortably close to the drag.** The
margin is thin; this is why it is "narrow."

**C5 — Right (DTE × time-of-day) cell.** Probably **early-to-mid week** (gentler theta) for
the trend-ride variant, and **avoid late-day expiry pin** unless the gamma is explicitly being
harvested with eyes open (§3b). The edge is not uniform across the contract life.

**C6 — Costs are modeled aggressively, on REAL premiums.** The go/no-go can come **only** from
forward paper-trading against **real intraday option LTPs and real spreads**
(`options_scalper.md` §5b) — because the entire question is decided in the microstructure (§3),
which a model cannot honestly reproduce. The optional Black-76 path (§5c) is **structural
sanity only, never go/no-go**, and is forbidden without a real intraday underlying path.

**If C1–C6 all hold, the realistic outcome is a low-Sharpe, low-frequency, fat-tailed
positive-EV strategy that makes money on a minority of trending sessions and is flat-to-
slightly-negative on the rest.** That is the *best* honest case. It is **not** "always makes
money"; it is "positive expectancy with a long flat-or-bleeding stretch between fat-tail
winners." The drawdowns (strings of small theta/spread losers in chop) will be psychologically
and statistically real, which is why `max_trades`, `daily_loss_cap`, and `cooldown_min` exist.

---

## 5. Conclusion (the honest verdict)

- **Is there a genuine edge?** There is **one** plausible edge: **trend/momentum capture
  monetised through long gamma** — being positioned long-delta-long-gamma when a fast
  directional move occurs (momentum or ORB entry), with ruthless theta/spread control (time-
  stop, hard stop, signal-flip) on the majority of non-moves, and a **trailing remainder** to
  catch the fat-tail moves that pay for everything. It is a **convexity edge, not an accuracy
  edge.**

- **What kills it?** The cost stack — **bid-ask paid twice per round-trip, slippage on moving
  options, theta bleed, brokerage/STT/fees** — which sums to roughly **₹280–300/lot per round-
  trip** against a ~₹780/lot gross +10% target, pushing the break-even win rate to ~coin-flip
  *before* the tail upside (§3e). Overtrading flat/chop tape converts the strategy into a
  donation machine.

- **Is positive EV plausible?** **Yes, but narrow** — only under C1–C6 (§4): convexity-first
  design, trade-only-when-moving, ATM/liquid/weekly, targets that clear the cost stack with
  margin, the right DTE/time cell, and **aggressive cost modeling on real premiums**. The best
  honest case is a **low-Sharpe, low-frequency, fat-tailed** strategy that is flat-or-bleeding
  most sessions and positive on the trending minority. **It will never "always make money."**

- **What is forbidden?** (1) **Mean-reversion entries** — structurally inverted for a long-
  option buyer; that edge belongs to the option *seller*. (2) **Any backtest on synthesised
  intraday premiums** — GIGO; the edge lives in microstructure a model cannot fake.

- **The decision rule:** the go/no-go is a **forward paper-trade on real intraday ATM option
  LTPs** (`options_scalper.md` §5b), reporting **net-of-cost per-scalp expectancy, assumed
  slippage, and break-even win rate** prominently. Until that real-premium evidence exists,
  treat this strategy as an **unproven hypothesis with a coherent but thin theoretical edge**,
  not a validated source of returns.

---

### Appendix — falsifiable predictions (how to know the thesis is wrong fast)

If the thesis in §5 is correct, the forward-paper track will show:
1. **Low win rate (~40–50%) but positive net expectancy** driven by a fat right tail (the
   trailing-remainder exits dominate total P&L). *If the win rate is high but EV is negative →
   theta/spread is eating you; thesis intact but execution is too tight.*
2. **P&L concentrated in a minority of trending sessions**, flat-to-negative on range days.
   *If P&L is evenly smeared across all days → the filter (C2) is not working and you are
   overtrading chop → expect long-run negative drift.*
3. **Removing the trailing remainder collapses EV toward/below zero.** *If it doesn't, the
   convexity thesis (C1) is wrong and there is no edge — abandon.*
4. **Doubling modeled slippage flips EV negative.** *Expected — it confirms the edge is thin
   and microstructure-bound (§3e). If a 2× slippage stress leaves EV comfortably positive,
   either slippage is under-modeled or the result is too good to be true — re-audit.*
