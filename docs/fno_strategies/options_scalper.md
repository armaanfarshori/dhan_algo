# Intraday Options Scalper (Ladder) — strategy spec (F&O backtester)

**Status:** PLAN — research only, PAPER. No live order paths. Intraday, long single-leg, high-turnover.
**Branch:** `feat/fno-options-strategies`
**Author audience:** the engineer who will code this strategy + (conditional) intraday backtester next.

This document specifies an **intraday options scalper** that buys a **single long option leg**
(long CE or long PE), scalps **many small profits**, and **ladders** in and out of the position.
It is fundamentally different from the other specs in this folder (`defined_risk.md`,
`premium_sell_undefined.md`, `verticals.md`, `ratio_calendar.md`), which are all **single-expiry,
hold-to-(or-near)-expiry, daily-step, premium-collection** structures. This one is **long
premium, intraday, closed before close every day**, and lives or dies on **per-round-trip cost
and slippage** because it trades a lot.

It reuses the project conventions but **NOT** the daily-cycle condor harness. The directional
intraday logic mirrors `strategies/orb.py` (synchronous `on_tick → Decision`, position fed back
via `notify_*`, unconditional EOD square-off, future-skew guard). Black-76 from
`research/backtest/fno_condor.py` is reused **only** for the optional backtest premium model.

> **READ §5 (DATA FEASIBILITY) FIRST IF YOU ARE ABOUT TO BACKTEST THIS.** The DB has **NO
> intraday option premiums** and likely **no intraday NIFTY index 1-min bars**. A faithful
> backtest of this strategy is *blocked on intraday data we do not have today*. Do not fabricate
> an intraday premium path from daily bars. The honest near-term path is **forward paper-trade
> against a live intraday option-LTP feed** (which the collector does not provide yet — a flagged
> data gap), not a historical backtest.

---

## 0. What "scalp + ladder" means here (precise definitions)

Two orthogonal mechanics, both included:

- **Direction signal (§1):** decide *bullish vs bearish* on the underlying intraday → buy CE
  (bullish) or buy PE (bearish). One direction at a time; never both legs.
- **Ladder (§2):** how we *scale into* the position (pyramiding entries at price rungs) **and**
  *scale out of* it (laddered partial take-profits at successive small premium targets).

"Scalp" = small per-tranche take-profit (a few % of premium / a few premium points), tight stop,
and a **time-stop** because long options bleed theta. High turnover → costs dominate (§5).

The single source of truth for the position is **filled tranches**, exactly like ORB tracks
`position`/`entry_price` only via `notify_fill`/`notify_flat`. The strategy never assumes a
requested order executed.

---

## 1. CE/PE direction signal (the researched part)

### 1a. Survey of common intraday direction signals

| Signal | How it reads | Pros | Cons for a 1-min option scalper |
|---|---|---|---|
| **Price vs intraday VWAP** | long-bias above VWAP, short-bias below | anchored to the day's fair value; institutional default; cheap to compute; mean-reverts intraday whipsaw less than a fast MA | needs intraday volume to be a *true* VWAP; on the index spot we have price but volume is index-synthetic — use a typical-price VWAP |
| **Fast/slow EMA crossover (e.g. 5/20 on 1-min)** | EMA_fast > EMA_slow → bullish | classic, smooth, easy to tune | lags; in chop it flips repeatedly → death-by-costs for a scalper |
| **Opening-range direction (ORB)** | break of first-N-min high/low sets the bias | reuses `orb.py` logic verbatim; one decision/day-ish | too few signals for a *scalper*; misses intraday regime changes |
| **Supertrend (ATR bands)** | flip on ATR-band cross | trend-following, fewer flips than EMA | another param (ATR mult); still a lagging flip model |
| **Signed k-min momentum + volatility/ADX filter** | sign of trailing-k-min return, gated by a trend-strength / range filter | directly "is it moving, and which way" — the scalper's actual question; the filter suppresses chop entries | needs a sane filter or it overtrades flat tape |

### 1b. Chosen default — **VWAP-anchored bias + signed-momentum trigger, with a trend/vol filter**

The single weakest point of an intraday option scalper is **overtrading flat, choppy tape**
(every flip pays the full cost stack twice). So the default combines a *slow regime anchor* with
a *fast trigger* and an *activity filter*:

1. **Regime anchor — VWAP.** Compute a running intraday VWAP of the **underlying** (NIFTY 50
   spot, `security_id "13"`) from session open. Bias is `LONG` while `price > VWAP·(1+band)`,
   `SHORT` while `price < VWAP·(1−band)`, else `FLAT` (no new entries). `band` (default `0.0005`,
   5 bps) is a deadband that stops VWAP-hugging chop from flipping the bias.
2. **Trigger — signed k-min momentum.** Only enter in the anchor's direction when the trailing
   `k`-minute return of the underlying confirms it: `LONG` needs `ret_k > +mom_thresh`,
   `SHORT` needs `ret_k < −mom_thresh`. Default `k = 5` min, `mom_thresh = 0.0008` (8 bps).
3. **Activity filter — realized 1-min range / ATR-style.** Suppress entries when intraday is
   dead: require trailing `atr_window`-min average true range of the underlying
   `≥ min_atr_pts` (default `min_atr_pts = 6.0` NIFTY points over `atr_window = 14`). This is the
   ADX-spirit "don't scalp a flat tape" guard, expressed in points so it is cheap and transparent.

Bullish (anchor LONG + trigger + filter) → **buy CE**. Bearish → **buy PE**. While `FLAT`, hold
existing tranches per the exit rules but **open no new ladder rungs**.

**Why this default:** it gives *enough* signals for a scalper (momentum trigger fires intraday)
without the EMA-crossover whipsaw, and the VWAP anchor + ATR filter directly attack the
cost-killer (overtrading chop). It is fully computable from an **underlying intraday price path
alone** — no option-chain dependency for the *signal*.

### 1c. Swapping the signal

`ScalperParams.signal` selects the model so the backtest can A/B them on identical tape:

- `"vwap_mom"` *(default)* — the §1b combination.
- `"ema"` — `EMA(fast) vs EMA(slow)`; params `ema_fast=5`, `ema_slow=20`. Anchor only (trigger
  off). Provided as the obvious baseline to beat.
- `"orb"` — first-`orb_minutes` range break sets a fixed daily bias; reuse `orb.py` semantics.
- `"momentum"` — signed k-min momentum only, no VWAP anchor, no filter (the noisy baseline that
  shows how much the anchor/filter actually save in costs).

A **separate pure helper** computes the signal from the trailing underlying-price window:
`direction_signal(bar_window, params) -> "LONG" | "SHORT" | "FLAT"`. It does **not** live inside
the entry/exit logic and it never touches option premiums.

### 1d. Strike selection

Bias → buy **ATM by default**, with an explicit offset knob:

- `strike_offset = 0` *(default, ATM)* — round underlying to the 50-grid (`_round_to_step`). ATM
  is the liquidity sweet spot (tightest spread → least slippage, and slippage is the whole game
  here) and has delta ≈ 0.5 with healthy gamma — a few points of underlying move converts to a
  scalp-able premium move.
- `strike_offset = -1` (one step ITM, i.e. CE strike = ATM−50 / PE strike = ATM+50) — **higher
  delta (~0.6–0.7)** so premium tracks the underlying more 1:1; costs more premium and a touch
  more spread, but less theta bleed as a fraction. Use when you want directional fidelity.
- `strike_offset = +1` (one step OTM) — **cheaper, higher gamma**, but worse spread and faster
  theta decay; only sensible on strong, fast moves. Generally **not** recommended for a scalper
  because the wider relative spread eats the small targets.

`strike_offset` is in **grid steps** (signed; − = ITM for the chosen side, + = OTM). Default `0`.
Strike is fixed at **first entry of a ladder** and reused for every add of that ladder (you ladder
the *same contract*, not new strikes).

### 1e. Warm-up

No entries until all signal inputs are warm: need ≥ `max(ema_slow, k, atr_window)` one-minute
underlying bars **and** `t ≥ MARKET_OPEN + warmup_minutes` (default `warmup_minutes = 15`). This
also keeps us out of the opening auction's noise (see the trade window in §3/§4).

---

## 2. LADDER mechanics (precise)

The ladder has **two independent halves**, each parameterised:

### 2a. Laddered ENTRIES (scale-in / pyramiding)

Add tranches as the trade moves **favorably in the underlying** (pyramiding a winner — the
default), measured in **underlying points** from the ladder's first-entry underlying level so the
rung spacing is signal-native and does not depend on the noisy option premium:

- First tranche on the §1 signal. Record `ladder_anchor_underlying = underlying at first fill`.
- Add tranche *i* when the underlying has moved a further `rung_spacing_pts` **in the trade's
  favor** from the previous rung (LONG: up; SHORT: down). Default `rung_spacing_pts = 10.0`.
- **Tranche sizing:** `tranche_lots` lots per rung, default **1 lot** (=65 units). `max_rungs`
  caps total exposure, default **3** → max 3 lots open per ladder. Sizing mode is a knob:
  `ladder_size_mode = "flat"` *(default, equal lots/rung)* or `"decreasing"` (1, then halve,
  floor 1 — classic anti-martingale that limits adds at extended prices).
- **Direction of laddering** is a knob: `ladder_mode = "pyramid"` *(default — add on favorable
  move)* or `"scale_in_dips"` (add when the underlying retraces `rung_spacing_pts` *against* the
  trade while the §1 bias is unchanged — averaging into a pullback). **Default is `pyramid`**:
  for a long-option scalper, scaling into adverse moves compounds theta + delta loss and is the
  classic blow-up; pyramiding only adds when the thesis is already working.
- No new rung while the §1 anchor is `FLAT` or has flipped against the open ladder (a flip closes
  the ladder per §3, it never adds).

### 2b. Laddered EXITS (partial take-profits + trailing remainder)

Each **open tranche** is scalped out independently at successive small premium targets measured
in **premium %** of *that tranche's* fill premium (premium-relative so it works across strikes):

- `tp_ladder_pct = [0.10, 0.20, 0.35]` *(default)* — exit one tranche's worth of lots at each
  successive level: first reached target sells 1 tranche, next sells the next, etc. With the
  default 3-rung ladder this means "+10% closes the first lot, +20% the second, +35% the third."
- The **last remaining tranche trails**: once `tp_ladder_pct[-1]` is hit on any lot, convert the
  remainder to a trailing stop at `trail_pct` (default `0.12`, i.e. give back 12% of premium from
  the high-water premium) so a runaway move is not capped at the top rung.
- If fewer rungs filled than TP levels, the TP ladder still applies in order to whatever lots are
  open (e.g. a 1-lot ladder just uses `tp_ladder_pct[0]` then trails).

### 2c. Position / fill tracking

The strategy holds a small list of **open tranches**, each: `{lots, entry_premium,
entry_underlying, hi_water_premium}`. `notify_fill(side, qty, premium)` appends (BUY) or reduces
(SELL, FIFO) tranches; `notify_flat()` clears all. Net `position_lots` and weighted
`avg_premium` are derived. **Decisions reference tranches, not a single average** — partial exits
are per-tranche. This mirrors ORB's "position view updated only via notify_*", extended to a
multi-tranche book.

### 2d. Concrete default ladder (one line)

> **Buy ATM CE/PE 1 lot on signal; add 1 lot every +10 NIFTY pts in favor up to 3 lots
> (pyramid, flat sizing); take partial profit at +10% / +20% / +35% of each lot's premium, then
> trail the last lot 12% off its premium high; hard stop −20% per lot, time-stop 12 min, flat by
> EOD.**

---

## 3. Scalp exit logic (full)

Per **open tranche** unless noted (whole-ladder rules flagged):

1. **Take-profit ladder** — §2b. Premium-relative, per tranche, in order.
2. **Trailing stop on the remainder** — §2b, after the top TP rung is hit.
3. **Hard stop-loss** — exit a tranche at `−stop_pct` of its fill premium. Default
   `stop_pct = 0.20`. (Whole-ladder convenience: if *all* open tranches breach their stop on the
   same tick, flatten the ladder in one EXIT.)
4. **Time-stop (theta guard)** — if a tranche has been open `≥ time_stop_min` without hitting its
   first TP rung, exit it at market. Default `time_stop_min = 12`. Long options bleed; a
   stagnant scalp is a losing scalp.
5. **Signal-flip exit** *(whole ladder)* — if the §1 anchor flips against the open direction
   (LONG→SHORT or vice-versa), flatten the entire ladder immediately (do not reverse on the same
   tick; a fresh entry must re-qualify next tick — avoids ping-pong).
6. **Re-entry cooldown** — after any *full* flatten of a ladder, no new ladder for
   `cooldown_min` minutes (default `3`). Prevents instant re-fire on the same micro-move.
7. **Max trades / day** *(whole strategy)* — `max_trades` counts **ladders started** per day
   (default `8`). At the cap, signals are ignored for the rest of the session (existing tranches
   still managed/exited normally).
8. **Daily-loss kill** *(whole strategy)* — track realized intraday P&L (net of modeled costs);
   if it drops below `−daily_loss_cap` (₹, default `8000`), flatten everything and **stand down
   for the day** (no new ladders; EOD square-off still applies). This is the strategy-level
   analogue of the platform RiskEngine; in live wiring the real `RiskEngine` still owns the
   kill-switch and is never bypassed.
9. **Trade window** — no new ladders before `MARKET_OPEN + no_trade_open_min` (default `15`, also
   the warm-up) or after `MARKET_CLOSE − no_trade_close_min` (default `20`). Opening auction and
   the pre-close illiquidity/expiry-pin both produce bad fills.
10. **EOD square-off — UNCONDITIONAL.** At `MARKET_CLOSE − squareoff_before_close_min`
    (default `5`), if `position_lots != 0` emit a single `EXIT` that flattens the **entire**
    ladder, regardless of signal, ladder state, P&L, or warm-up — exactly like `orb.py`'s
    unconditional square-off (long options must never be carried overnight on a paper scalper).

---

## 4. Params dataclass (fields + defaults)

```python
from dataclasses import dataclass, field

@dataclass
class ScalperParams:
    # ── direction signal (§1) ──────────────────────────────────────────────
    signal: str = "vwap_mom"      # "vwap_mom" | "ema" | "orb" | "momentum"
    vwap_band: float = 0.0005     # deadband around VWAP (5 bps) for LONG/SHORT/FLAT
    mom_k: int = 5                # trailing minutes for the momentum trigger
    mom_thresh: float = 0.0008    # min |k-min return| to trigger (8 bps)
    atr_window: int = 14          # ATR-style activity filter window (min)
    min_atr_pts: float = 6.0      # min avg true range (NIFTY pts) to allow entries
    ema_fast: int = 5             # used when signal == "ema"
    ema_slow: int = 20
    orb_minutes: int = 15         # used when signal == "orb"
    warmup_minutes: int = 15      # no entries before OPEN + this

    # ── strike (§1d) ───────────────────────────────────────────────────────
    strike_offset: int = 0        # grid steps; - = ITM for the side, + = OTM
    step: int = 50                # NIFTY strike grid

    # ── ladder entries (§2a) ───────────────────────────────────────────────
    ladder_mode: str = "pyramid"          # "pyramid" | "scale_in_dips"
    rung_spacing_pts: float = 10.0        # underlying-pt spacing between adds
    tranche_lots: int = 1                 # lots per rung (×65 units)
    max_rungs: int = 3                    # max tranches per ladder
    ladder_size_mode: str = "flat"        # "flat" | "decreasing"

    # ── ladder exits / scalp (§2b, §3) ─────────────────────────────────────
    tp_ladder_pct: list = field(default_factory=lambda: [0.10, 0.20, 0.35])
    trail_pct: float = 0.12               # trail remainder off premium high-water
    stop_pct: float = 0.20                # hard stop per tranche (% of fill premium)
    time_stop_min: int = 12               # theta time-stop per tranche
    cooldown_min: int = 3                 # after a full flatten, before new ladder

    # ── risk / session limits (§3) ─────────────────────────────────────────
    max_trades: int = 8                   # ladders started per day
    daily_loss_cap: float = 8000.0        # ₹ realized-loss stand-down
    no_trade_open_min: int = 15
    no_trade_close_min: int = 20
    squareoff_before_close_min: int = 5

    # ── contract ───────────────────────────────────────────────────────────
    lot: int = 65                          # NIFTY_LOT (fno_costs.NIFTY_LOT)
```

Decision protocol mirrors `orb.py` but carries option context:

```python
@dataclass(frozen=True)
class ScalpDecision:
    action: str        # "ENTER" | "EXIT"
    side: str = ""     # ENTER: always "BUY" (long-only). EXIT: "SELL".
    option_type: str = ""   # "CE" | "PE"
    strike: int = 0
    lots: int = 0           # tranche size this decision acts on
    reason: str = ""
```

`on_tick(now, underlying_price, option_premium=None, high=None, low=None)`:
- `underlying_price` drives the **signal + entry rungs** (underlying-pt spacing).
- `option_premium` (LTP of the *currently held* contract) drives **TP/stop/trail/time-stop**.
  In live/forward paper this is the broker LTP of the held option; in the optional backtest it is
  the Black-76 model price (§5). **If `option_premium is None` while a position is open, the
  strategy must NOT invent it** — it returns no exit decision and logs a stale-premium warning
  (fail-safe: never fabricate a premium move).

---

## 5. DATA FEASIBILITY — read this honestly (the crux)

This strategy needs an **intraday option-premium path**. The current DB does not have one.

### 5a. What we actually have (from migrations 009–011)
- `index_bars` — **DAILY** OHLCV for NIFTY (id `13`) and India VIX (id `21`) + `realized_vol_20d`.
  `timeframe` defaults `"1d"`. **No intraday index bars are ingested today.**
- `option_chain_snapshot` — the full chain, but captured **EOD / forward** by
  `core/fno_collector.py` (post-close cron). It is a handful of snapshots per expiry, **not an
  intraday LTP time series.** Dhan has **no historical option IV/LTP** intraday (documented in
  the handoff and `fno_collector` docstring).
- `fno_paper_trades` — a **hold-to-expiry** condor log; not intraday.

**Therefore: there is NO intraday option-premium series and (today) NO intraday NIFTY 1-min
series in this DB.** A faithful historical backtest of an intraday scalper is **blocked on data
we do not have.** This is the single most important fact in this document.

### 5b. The honest near-term path — **forward paper-trade on a live intraday feed (RECOMMENDED)**
Run the §1–§4 strategy as a **pure `on_tick` engine** (it already is) driven by a **live intraday
feed**:
- Underlying 1-min (or tick) for NIFTY spot → signal + entry rungs.
- **Option LTP of the held ATM contract** → TP/stop/trail/time-stop.

**Data gap to flag loudly:** `core/fno_collector.py` is **EOD-only** — it does not stream or even
poll intraday option LTPs. Forward paper-trading this strategy **requires a new intraday
option-LTP source** (a live WS/poll subscription to the specific ATM CE/PE security_ids for the
day, re-selected as spot moves across strikes). That subscription does not exist yet. Building it
is the prerequisite, and it is **separate from this strategy's logic**. Log every paper fill +
exit to a table analogous to `fno_paper_trades` (intraday variant) so an honest forward track
accrues. This is the recommended path: it uses *real* premiums and *real* spreads, which is
exactly where this strategy's edge (or lack of it) is decided.

### 5c. OPTIONAL Black-76 intraday backtest — **EXPLICITLY conditional on intraday index data**
*Only if* an **intraday NIFTY index 1-min path** becomes available (e.g. a backfill into
`index_bars` at `timeframe="1min"`, or an external 1-min series), you may approximate the option
premium each minute with Black-76:

```
premium_t = black76_call/put(F=underlying_t, K=strike, T=dte_t/365, sigma=σ_t)
```

reusing `research/backtest/fno_condor.black76_call/put`. Constraints, stated so no one fakes it:

- **HARD DEPENDENCY:** this path **requires a real intraday underlying path**. **Do NOT synthesize
  an intraday path from daily OHLCV** (interpolating/Brownian-bridging daily bars into fake 1-min
  ticks would manufacture the very premium moves the strategy trades — that is fabrication, not a
  backtest). If intraday index data is absent, this path is **not available**; use §5b instead.
- **`σ_t` (IV) is itself a model, not data.** We have no intraday IV. Best available proxy: hold
  `σ` constant intraday at the day's ATM IV (VIX/100 or the EOD chain ATM IV), optionally with a
  deterministic intraday-vol smile-in-time. State the assumption in the report; it understates
  real IV crush around lunch and the pre-close vol pickup.
- **Black-76 ≠ the bid/ask the scalper actually trades.** The model gives a mid; a scalper pays
  the spread on **every** round-trip. Slippage must be applied aggressively (§5d) or the backtest
  will be optimistically biased.
- Treat any result from this path as **directional/structural sanity only**, never as a go/no-go.
  The go/no-go for an intraday scalper comes from §5b forward paper with real LTPs.

### 5d. Costs & slippage DOMINATE — stress this
A scalper pays the **full cost stack on every round-trip**, and it does many per day:
- Use `fno_costs.condor_costs(legs, ...)` per round-trip (the name is historical; it costs any
  leg list). Each scalp = a BUY leg + a SELL leg → 2 × `BROKERAGE_PER_ORDER` (₹20) = **₹40
  brokerage alone per round-trip**, before STT (sell-side 0.15% of premium), exchange fee, SEBI,
  stamp, GST.
- Apply `fno_costs.slippage(premium, pct=...)` on **both** legs of **every** tranche. For an
  intraday long scalp use a **conservative pct** (default `0.005` is for liquid mids; for
  small-target scalping consider `0.01`+ because you cross the spread both ways). **Worked
  feel:** an ATM NIFTY premium ~₹120; a +10% target = ₹12/unit ≈ ₹780/lot gross; ₹40 brokerage +
  STT/fees + ~₹1.2–2.4/unit slippage each side can erase a third-to-half of that small target.
  **The strategy is only viable if the average winning scalp clears the round-trip cost+slippage
  with margin** — make the backtest/forward report show **net-of-cost per-scalp expectancy** and
  the **break-even target %** at the assumed cost/slippage, prominently.

---

## 6. Hard rules (mirror `orb.py`)

1. **Session reset** — on a new `now.date()`, reset VWAP accumulator, signal state, ladder book,
   `trades_today`, daily realized P&L, cooldown. (ORB `_reset_session` analogue.)
2. **Unconditional EOD square-off** — §3.10. Above any signal/ladder/warm-up gate; flattens an
   open book even after a mid-session restart with unknown signal state.
3. **Future-skew guard** — copy ORB's `MAX_FUTURE_SKEW` (2 min) check verbatim: a tick stamped
   implausibly in the future must not reset the session nor advance any timer; ignore it.
4. **Position via fills only** — the tranche book changes **only** through `notify_fill` /
   `notify_flat`; `on_tick` never assumes a requested order filled.
5. **Long-only** — every ENTER is `side="BUY"`; the only SELLs are exits. No naked shorting.
6. **Fail-safe on missing premium** — if a position is open and `option_premium is None`, emit no
   premium-based exit and warn; never fabricate a premium (cf. Kronos fail-open spirit).
7. **PAPER only** — no live order path from this spec. Live wiring (later) routes through the
   real `RiskEngine`, which owns the kill-switch and is never bypassed.

---

## 7. Unit-test cases (deterministic `on_tick` / premium sequences)

All times IST, NIFTY step 50, lot 65, defaults from §4 unless overridden. Feed
`(now, underlying_price, option_premium)`; assert the returned `ScalpDecision` (or `None`).
Each case is a pure-function test of the strategy class — **no DB, no network** (same testability
contract as `orb.py`). Premiums in entry/exit cases are supplied by the test (representing the
held contract's LTP), never derived by the strategy.

1. **Warm-up blocks entry.** Feed a clean LONG signal (price rising above VWAP, momentum + ATR
   satisfied) at `09:20` (before OPEN+15). Expect `None` (no ENTER until `09:30`).

2. **First entry — bullish → buy CE ATM.** After warm-up, underlying `22030`, signal LONG.
   Expect `ENTER BUY option_type="CE" strike=22050? ` → with `strike_offset=0`, ATM round of
   `22030` = `22050`; assert CE at the rounded ATM strike, `lots=1`. Record ladder anchor.

3. **First entry — bearish → buy PE ATM.** Symmetric: underlying below VWAP−band with negative
   k-min momentum and ATR ok → `ENTER BUY PE` at ATM. Confirms side selection.

4. **FLAT zone suppresses entry.** Underlying inside `VWAP·(1±band)` (within deadband) → signal
   FLAT → `None` even though ATR/momentum could otherwise qualify.

5. **Activity filter blocks chop.** Strong momentum but trailing ATR < `min_atr_pts` (flat tape)
   → `None`. Then raise ATR ≥ threshold with the same momentum → `ENTER`.

6. **Ladder add (pyramid).** After case 2 fills (notify_fill BUY 1 lot @ premium ₹120, anchor
   underlying 22030). Underlying advances to `22040` (+10 pts) with bias still LONG → second
   `ENTER BUY CE` (same strike, `lots=1`). At `22050` (+20) → third add. At `22060` (+30) → **no
   add** (`max_rungs=3` reached).

7. **TP ladder partial exits.** With 3 lots open (fills @ ₹120 each), held-contract premium rises:
   at ₹132 (+10%) → `EXIT SELL lots=1` (first TP). At ₹144 (+20%) → second `EXIT SELL lots=1`.
   At ₹162 (+35%) → top TP → remaining lot converts to trailing.

8. **Trailing remainder.** Continuing case 7: premium runs to ₹180 (high-water) then falls to
   `180·(1−0.12)=₹158.4` → `EXIT SELL` the last lot (trail hit). Assert no exit at ₹165 (above
   trail line).

9. **Hard stop.** Single tranche filled @ ₹120; premium falls to `120·(1−0.20)=₹96` → `EXIT SELL`
   that tranche (stop). Assert no exit at ₹100 (above stop).

10. **Time-stop (theta).** Tranche filled @ `10:00`, premium oscillates within ±5% (never hits
    TP1). At `10:12` (≥ `time_stop_min`) → `EXIT SELL` that tranche. Assert no time-stop exit at
    `10:11`.

11. **Signal-flip flattens ladder.** 2 lots open LONG (CE). Underlying drops decisively below
    VWAP−band with negative momentum → anchor flips SHORT → single `EXIT` flattening **all** lots;
    assert **no** immediate re-ENTER on the same tick (cooldown + re-qualify next tick).

12. **Cooldown + max_trades.** After a full flatten at `11:00`, a fresh LONG signal at `11:02`
    (< `cooldown_min=3`) → `None`; at `11:03` → allowed. Separately, after `max_trades=8` ladders
    started, the 9th qualifying signal → `None` (cap), while an already-open ladder still exits
    normally.

13. **Daily-loss kill.** Accumulate realized net losses past `−daily_loss_cap` (₹8000). Next tick
    flattens any open book and stands down: all subsequent qualifying signals → `None` for the
    rest of the day (EOD square-off still fires if somehow holding).

14. **Unconditional EOD square-off.** Position open (any lots) at `15:25`
    (`MARKET_CLOSE − squareoff_before_close_min`) → single `EXIT` flattening the entire ladder,
    regardless of signal/ladder/P&L. With no position at `15:25` → `None`.

15. **Future-skew guard.** Feed a tick stamped `+5 min` ahead of wall clock → `None`, and assert
    it did **not** reset the session, advance the time-stop, or widen VWAP (mirror ORB's test).

16. **Stale premium fail-safe.** Position open, `on_tick(..., option_premium=None)` → returns no
    premium-based exit (only session/EOD rules may fire) and logs a warning; assert the strategy
    did **not** synthesize a premium or fabricate a TP/stop.

---

## 8. Implementation notes for the coder

- Build the strategy class first (`strategies/options_scalper.py` or a research module), pure and
  synchronous, exactly like `orb.py`. Get §7 green with hand-fed sequences before any data wiring.
- The **signal helper** is a separate pure function over a trailing underlying-price window; unit
  test it independently for each `signal` mode.
- **Do not** wire a historical backtest until §5 is resolved. If you build the optional Black-76
  intraday path, gate it behind an explicit "intraday index data present" check and emit a loud
  warning in the report that premiums are *modeled, not observed*, with `σ` held constant.
- Reuse, do not re-derive: `black76_call/put`, `_round_to_step`, `fno_costs.slippage`,
  `fno_costs.condor_costs`, `NIFTY_LOT`.
- The forward-paper LTP feed (§5b) is the real unlock and is **out of scope for this strategy
  file** — flag it as a dependency PR (intraday ATM CE/PE subscription + intraday paper log).
```
