# Directional Vertical Spreads — strategy spec (F&O backtester)

**Status:** PLAN — research only, PAPER. No live order paths. Single-expiry, backtestable.
**Branch:** `feat/fno-options-strategies`
**Author audience:** the engineer who will code these builders/resolvers next.

This document specifies four **two-leg directional vertical spreads** (defined risk) for the
NIFTY weekly-cycle backtester. They reuse the existing pricing / cost / resolution machinery
in `research/backtest/fno_condor.py` and `research/backtest/fno_costs.py` — do **not** re-derive
Black-76 or the cost stack; call them.

---

## 0. Engine contract (the shape every builder must satisfy)

```python
@dataclass
class Leg:
    option_type: str   # "CE" (call) | "PE" (put)
    side: str          # "BUY" | "SELL"
    strike: int        # absolute strike, already rounded to the grid
    qty_lots: int      # number of lots (×NIFTY_LOT units inside cost/pnl math)

def builder(
    spot: float,
    atm_strike: int,
    step: int,          # NIFTY strike grid = 50
    dte: int,           # calendar days to expiry
    sigma: float,       # annualised IV fraction (straddle_iv); single IV, all legs
    params: <ParamsDataclass>,
    # NOTE: NO `direction` param — direction is encoded in the BUILDER NAME
    # (build_bull_put_spread, build_bear_call_spread, etc.). The caller (runner or
    # dispatcher) resolves the direction from the signal helper and picks which
    # builder to invoke; the builder itself is direction-unambiguous.
) -> list[Leg]
```

Every vertical returns **exactly two legs**. The four strategies map cleanly onto
`(direction, debit_or_credit)`:

| Strategy          | Direction | Type   | Short leg          | Long leg           |
|-------------------|-----------|--------|--------------------|--------------------|
| Bull Put Spread   | BULLISH   | CREDIT | sell higher PE     | buy lower PE       |
| Bear Call Spread  | BEARISH   | CREDIT | sell lower CE      | buy higher CE      |
| Bull Call Spread  | BULLISH   | DEBIT  | sell higher CE     | buy lower CE       |
| Bear Put Spread   | BEARISH   | DEBIT  | sell lower PE      | buy higher PE      |

A dispatcher chooses the structure from `(direction, prefer_credit)` so the backtest can run
credit-only, debit-only, or "natural" (credit spread in the trade direction). Keep the four
builders as small pure functions; the dispatcher is a thin wrapper.

### Reused machinery (already locked)
- `black76_call(F, K, T, sigma)` / `black76_put(...)` — undiscounted, `F ≈ spot`, `T = dte/365`.
- `_round_to_step(value, step)` — round-half-up to the strike grid.
- `core.fno_derived.implied_move(spot, sigma, dte)` → expected move in points (`spot·σ·√(dte/365)`).
- `fno_costs.slippage(premium, pct=0.005)` — adverse, % of premium, per leg.
- `fno_costs.condor_costs(legs, exercise_intrinsic=...)` — the cost stack works for **any**
  leg list `(premium_per_unit, qty_units, side)`; name is historical. Use it for 2-leg spreads.
- `NIFTY_LOT = 65`.

---

## 1. THE DIRECTIONAL SIGNAL PROBLEM (read this honestly)

The vol-gate (`ml/fno_vol_gate.gate_decision`) returns only `SELL_PREMIUM` / `BUY_PREMIUM` /
`STAND_ASIDE`. **It has no directional view** — it compares realized vs implied vol, nothing
about whether NIFTY goes up or down. Verticals *require* a direction. So the direction must
come from a **separate, explicitly-labelled, unproven** signal. We do not pretend the vol-gate
provides it.

**Design rule:** the *spread structure is the deliverable; the direction signal is the variable
under test.* The backtest must make the signal swappable and must always be able to report
**both directions** so the structure can be evaluated independently of any directional alpha.

### 1a. Signal source: trend filter on `index_bars` (NIFTY 50 spot, security_id "13")

Phase-0 directional signal = a simple, transparent trend/momentum filter on the daily NIFTY
close series already in `index_bars` (same table the condor/vol-gate read). Two interchangeable
modes, selected by `DirectionParams.signal`:

1. **`"sma"` (default):** compare entry-day close to an N-day simple moving average.
   `close > SMA_N → BULLISH`, `close < SMA_N → BEARISH`, `== → STAND_ASIDE`.
   Default `sma_window = 20` (matches the 20-day realized-vol window already computed).
2. **`"momentum"`:** sign of the trailing M-day return.
   `close_t / close_{t-M} - 1 > thresh → BULLISH`, `< -thresh → BEARISH`, else `STAND_ASIDE`.
   Default `mom_lookback = 10`, `mom_thresh = 0.0`.
3. **`"both"` (research mode):** ignore the signal; run *both* BULLISH and BEARISH on every
   cycle and report each leg-direction's stats separately. This is the honest baseline — it
   isolates the structure's cost/edge from any claimed directional skill.

The signal is computed by a **separate pure helper** (e.g. `direction_signal(closes, params) ->
"BULLISH"|"BEARISH"|"STAND_ASIDE"`) fed the trailing close window from `index_bars`; it does
**not** belong inside the leg builder. Direction is encoded in **which builder is called** —
`build_bull_put_spread` vs `build_bear_call_spread` etc. — not passed as a param to the
builder. The dispatcher (runner) reads the direction signal and selects the appropriate named
builder; each builder is direction-unambiguous and accepts no `direction` argument.

### 1b. Honesty caveats (must appear in the PR / go-no-go)
- **Directional edge is UNPROVEN.** SMA/momentum trend filters on a daily index have weak,
  regime-dependent, well-arbitraged edge. Treat any positive result as suspect until validated
  across multiple market regimes.
- Report the **`"both"` baseline alongside any signalled result.** If the structure is only
  profitable *with* the signal, the result rides entirely on unproven directional alpha and the
  go/no-go must say so.
- The signal is computed from the entry-day **close**, same close-not-open and close-not-FSP
  caveats as the condor (§5). A trend read off the close, entered next morning, can be stale.
- Optionally **stack the vol-gate as a co-filter:** prefer CREDIT verticals when the vol-gate
  says `SELL_PREMIUM` (you are also a net vol seller), DEBIT verticals when `BUY_PREMIUM`. This
  is a sizing/selection heuristic, not a direction — keep it optional (`use_vol_gate` flag).

---

## 2. Strike geometry (shared by all four)

All four spreads place the **near (primary) strike** at an offset from spot and the **far
(protection / financing) strike** `width` strikes away. Two knobs in `VerticalParams`:

- `move_mult: float` — offset of the near strike from ATM, in units of the implied move.
  Near-strike center = `spot ± move_mult · implied_move(spot, sigma, dte)`.
  `move_mult = 0.0` → at-the-money near strike. Default `0.5` (slightly OTM short for credit
  spreads → higher win-rate, lower credit).
- `width: int` — number of `step`s between near and far strike (the defined-risk width).
  Default `2` → `width_pts = width · step = 100`.

Helper (mirrors `build_condor`):

```python
def _near_strike(spot, sigma, dte, step, move_mult, side_sign) -> int:
    em = implied_move(spot, sigma, dte) or 0.0
    return _round_to_step(spot + side_sign * move_mult * em, step)
# side_sign = -1 for put-side (strikes below spot), +1 for call-side (above spot)
```

`width_pts = width * step`. For NIFTY, `step = 50`.

> Strike-grid note: after rounding, the realised `width_pts` is always an exact multiple of
> `step` (`far = near ± width·step`), so `max_loss` math below is exact in points.

---

## 3. The four spreads — full spec

Notation per unit (×`NIFTY_LOT` for ₹, ×`qty_lots` for multiple lots):
`SP/LP` = short/long put premium, `SC/LC` = short/long call premium, all from Black-76 at entry.
Slippage is adverse on **every** leg: a SOLD leg fills at `prem − slippage(prem)`, a BOUGHT leg
fills at `prem + slippage(prem)`. Use the slippage-adjusted premiums for `net` and `max_loss`,
consistent with the condor harness.

---

### 3.1 Bull Put Spread — CREDIT, BULLISH

> Sell the higher-strike put, buy the lower-strike put. Profit if spot stays **above** the
> short put. Bullish-to-neutral.

- **Near strike (short PE):** `K_short = _near_strike(spot, sigma, dte, step, move_mult, -1)`
  (at/just-below spot).
- **Far strike (long PE):** `K_long = K_short - width*step` (further OTM, lower).
- **Legs:**
  - `Leg("PE", "SELL", K_short, lot)`
  - `Leg("PE", "BUY",  K_long,  lot)`
- **Net credit (per unit):** `credit = (SP − slip(SP)) − (LP + slip(LP))`. Positive.
- **Max profit:** `credit` (kept in full if `expiry_spot ≥ K_short`).
- **Max loss (DEFINED):** `max_loss = width_pts − credit` (per unit; clamp `max(0, ·)`),
  realised when `expiry_spot ≤ K_long`.
- **Breakeven:** `K_short − credit`.
- **Return on margin:** `credit / max_loss`.

---

### 3.2 Bear Call Spread — CREDIT, BEARISH

> Sell the lower-strike call, buy the higher-strike call. Profit if spot stays **below** the
> short call. Bearish-to-neutral.

- **Near strike (short CE):** `K_short = _near_strike(spot, sigma, dte, step, move_mult, +1)`
  (at/just-above spot).
- **Far strike (long CE):** `K_long = K_short + width*step` (further OTM, higher).
- **Legs:**
  - `Leg("CE", "SELL", K_short, lot)`
  - `Leg("CE", "BUY",  K_long,  lot)`
- **Net credit:** `credit = (SC − slip(SC)) − (LC + slip(LC))`. Positive.
- **Max profit:** `credit` (kept if `expiry_spot ≤ K_short`).
- **Max loss (DEFINED):** `max_loss = width_pts − credit` (clamp `max(0, ·)`), realised when
  `expiry_spot ≥ K_long`.
- **Breakeven:** `K_short + credit`.
- **Return on margin:** `credit / max_loss`.

---

### 3.3 Bull Call Spread — DEBIT, BULLISH

> Buy the lower-strike call, sell the higher-strike call. Net debit paid; profit if spot rises
> toward/above the short call. Defined risk = the debit.

- **Near strike (long CE):** `K_long = _near_strike(spot, sigma, dte, step, move_mult, +1)`
  — for a debit spread, `move_mult` is usually small/0 (near-ATM long). The **long is the
  nearer strike, the short the further** (per brief).
- **Far strike (short CE):** `K_short = K_long + width*step` (higher, caps the upside).
- **Legs:**
  - `Leg("CE", "BUY",  K_long,  lot)`
  - `Leg("CE", "SELL", K_short, lot)`
- **Net debit (per unit):** `debit = (LC + slip(LC)) − (SC − slip(SC))`. Positive.
- **Max profit:** `width_pts − debit` (full when `expiry_spot ≥ K_short`).
- **Max loss (DEFINED):** `max_loss = debit` (entire debit lost when `expiry_spot ≤ K_long`).
- **Breakeven:** `K_long + debit`.
- **Return on margin:** `max_profit / max_loss = (width_pts − debit) / debit`.

---

### 3.4 Bear Put Spread — DEBIT, BEARISH

> Buy the higher-strike put, sell the lower-strike put. Net debit paid; profit if spot falls
> toward/below the short put. Defined risk = the debit.

- **Near strike (long PE):** `K_long = _near_strike(spot, sigma, dte, step, move_mult, -1)`
  (near-ATM long).
- **Far strike (short PE):** `K_short = K_long - width*step` (lower, caps the downside).
- **Legs:**
  - `Leg("PE", "BUY",  K_long,  lot)`
  - `Leg("PE", "SELL", K_short, lot)`
- **Net debit:** `debit = (LP + slip(LP)) − (SP − slip(SP))`. Positive.
- **Max profit:** `width_pts − debit` (full when `expiry_spot ≤ K_short`).
- **Max loss (DEFINED):** `max_loss = debit` (lost when `expiry_spot ≥ K_long`).
- **Breakeven:** `K_long − debit`.
- **Return on margin:** `(width_pts − debit) / debit`.

---

## 4. Margin (SPAN ≈ max_loss for defined-risk verticals)

For a defined-risk vertical, the broker's SPAN+exposure margin is, in practice, capped at the
**max loss** (the spread cannot lose more than `width_pts − credit`, resp. the debit). Phase-0
approximation:

```python
margin = max_loss            # per lot, in ₹ = max_loss_per_unit * NIFTY_LOT
return_on_margin = net / max_loss
#   net  = credit  for credit spreads (max profit kept)
#   net  = max_profit (= width_pts − debit) for debit spreads
```

This is the apples-to-apples capital denominator across all four structures and the figure the
go/no-go should report (alongside `return_on_capital` for a fixed allocated `capital`, as the
condor does). **Caveat:** real SPAN can exceed `max_loss` slightly (exposure margin, intraday
peaks); brokers also block until expiry. Phase-0 `margin = max_loss` is the optimistic floor —
flag it.

---

## 5. Resolution at expiry (intrinsic) + the close-not-FSP caveat

Resolve by leg intrinsic at `expiry_spot`, identical pattern to `resolve_condor`. For a 2-leg
spread the per-unit gross P&L is:

**Credit spreads** (writer keeps credit, pays out short-leg ITM beyond the long's protection):
```
# Bull put:
gross = credit − [ max(K_short − S, 0) − max(K_long − S, 0) ]
# Bear call:
gross = credit − [ max(S − K_short, 0) − max(S − K_long, 0) ]
```

**Debit spreads** (holder paid debit, collects long-leg ITM minus short-leg assignment):
```
# Bull call:
gross = −debit + [ max(S − K_long, 0) − max(S − K_short, 0) ]
# Bear put:
gross = −debit + [ max(K_long − S, 0) − max(K_short − S, 0) ]
```

Multiply per-unit gross by `NIFTY_LOT * qty_lots`. Net = gross − costs.

- **Costs:** call `condor_costs(legs, exercise_intrinsic=...)` with the 2 legs as
  `(fill_premium_per_unit, NIFTY_LOT*qty_lots, side)`. `exercise_intrinsic` = aggregate ITM
  intrinsic of legs assumed exercised/assigned at expiry (the standard STT-on-exercise term);
  pass `0.0` if you model square-off in the market instead. Be consistent and state which.
- **`expiry_spot` = NIFTY index daily CLOSE, NOT the NSE Final Settlement Price (FSP).** FSP is
  the 30-min VWAP of NIFTY futures 15:00–15:30 IST. The close-vs-FSP gap can flip a near-ATM
  short strike between ITM/OTM. Same caveat as `cycles_from_db`. A GO must be re-validated with
  real FSP before any live consideration.
- **Entry uses the boundary-day close, not next-morning open** (same daily-step approximation).
- **Single IV for both legs** (`sigma = straddle_iv` proxy, VIX/100 in Phase-0). Real verticals
  span skew: the further OTM leg trades at a different IV. Ignoring skew **understates credit**
  on put spreads (put skew) and is a known directional bias — flag it. This is conservative for
  credit-received but optimistic for debit-paid; note both.

---

## 6. Params dataclass

```python
@dataclass
class DirectionParams:
    signal: str = "sma"        # "sma" | "momentum" | "both"
    sma_window: int = 20       # for signal="sma"
    mom_lookback: int = 10     # for signal="momentum"
    mom_thresh: float = 0.0    # min |return| to take a side (momentum)

@dataclass
class VerticalParams:
    width: int = 2             # strikes between near and far leg (×step). width_pts = width*step
    move_mult: float = 0.5     # near-strike offset from ATM, in implied-move units
    lot: int = 1               # number of lots (qty_lots); units = lot * NIFTY_LOT
    step: int = 50             # NIFTY grid (kept here for completeness; engine also passes it)
    prefer_credit: bool = True # dispatcher: credit spread in trade direction vs debit
    use_vol_gate: bool = False # optional co-filter (SELL_PREMIUM→credit, BUY_PREMIUM→debit)
    direction: DirectionParams = field(default_factory=DirectionParams)
```

Notes for the implementer:
- `move_mult` defaults differ by intent: ~`0.5` for credit (OTM short, higher win-rate);
  consider `0.0`–`0.25` for debit (near-ATM long). Expose it; do not hard-code per strategy.
- Keep `width` as an integer count of `step`s so `max_loss` stays an exact grid multiple.

---

## 7. Feasibility

- **Single-expiry, fully backtestable** on the existing `cycles_from_db` weekly cycles — each
  cycle already provides `spot`, `straddle_iv`, `dte`, `expiry_spot`. The only *new* input is
  the trailing NIFTY close window for the direction signal, available from the same `index_bars`
  rows already loaded (`_build_bar_maps` returns a date→close map; extend it to keep an ordered
  close series, or compute SMA/momentum in a small pre-pass).
- Pure / deterministic: no network. Builders + resolvers are math-only; only the cycle loader
  and the direction-signal pre-pass touch the DB (lazy imports, mirroring existing modules).
- Reuses Black-76, slippage, and `condor_costs` unchanged — no new pricing or cost code.

---

## 8. Unit-test cases (inputs + direction → legs, credit/debit, max_loss)

Use `step=50`, `NIFTY_LOT=65`, `width=2` (→ `width_pts=100`) unless stated. Tolerate float
rounding on premiums; assert strikes/sides exactly. Pick `spot=20000`, `sigma=0.14`, `dte=7`
→ `implied_move ≈ 20000·0.14·√(7/365) ≈ 387.5 pts`; with `move_mult=0.5` the near offset
≈ 194 → rounds to 200.

1. **Bull Put — leg construction.** `spot=20000, move_mult=0.5` → `K_short=19800` (PE SELL),
   `K_long=19700` (PE BUY). Assert two legs, both PE, one SELL one BUY, `K_short − K_long = 100`.
2. **Bull Put — credit & max_loss.** Assert `credit = (SP−slip) − (LP+slip) > 0`,
   `max_loss = max(0, 100 − credit)`, `breakeven = K_short − credit`, `credit < width_pts`.
3. **Bear Call — leg construction.** `spot=20000, move_mult=0.5` → `K_short=20200` (CE SELL),
   `K_long=20300` (CE BUY). Assert CE/CE, SELL+BUY, `K_long − K_short = 100`,
   `breakeven = K_short + credit`.
4. **Bull Call — debit & max_loss.** `move_mult=0.0` → `K_long=20000` (CE BUY),
   `K_short=20100` (CE SELL). Assert `debit = (LC+slip) − (SC−slip) > 0`, `max_loss = debit`,
   `max_profit = 100 − debit`, `breakeven = K_long + debit`.
5. **Bear Put — debit & max_loss.** `move_mult=0.0` → `K_long=20000` (PE BUY),
   `K_short=19900` (PE SELL). Assert `max_loss = debit`, `max_profit = 100 − debit`,
   `breakeven = K_long − debit`.
6. **Resolution — credit spread max win.** Bull Put, `expiry_spot=20100 (≥ K_short)` →
   `gross = credit` (both puts expire worthless). Bear Call, `expiry_spot=20000 (≤ K_short)` →
   `gross = credit`.
7. **Resolution — credit spread max loss.** Bull Put, `expiry_spot=19600 (≤ K_long)` →
   `gross = credit − width_pts = −max_loss`. Confirms loss is capped at the defined width.
8. **Resolution — debit spread bounds.** Bull Call: `expiry_spot=20200 (≥ K_short)` →
   `gross = width_pts − debit = +max_profit`; `expiry_spot=19900 (≤ K_long)` →
   `gross = −debit = −max_loss`. Asserts both defined-risk bounds.
9. **Direction signal (`both` mode).** `signal="both"` → backtest emits a BULLISH and a BEARISH
   result row per cycle; assert both present and structure-correct independent of any signal.
10. **Direction signal (`sma`).** Closes trending up (last close > 20-SMA) → `BULLISH` →
    dispatcher picks Bull Put (credit) when `prefer_credit=True`; trending down → Bear Call;
    flat/equal → `STAND_ASIDE` (no trade). One assertion per branch.

---

## 9. Honest summary of caveats (for the go/no-go)

1. **Directional edge unproven** — the whole P&L hinges on a weak SMA/momentum signal; always
   report the `"both"` structural baseline beside any signalled run.
2. **Close-not-FSP** settlement and **close-not-open** entry — daily-step approximations that can
   flip near-ATM outcomes; a GO needs FSP re-validation.
3. **Single IV / no skew** — understates put-spread credit, biases debit pricing; flag direction.
4. **`margin = max_loss`** is an optimistic floor for SPAN; real margin can be higher.
5. **VIX/100 IV proxy** and **20-day backward realized vol** carry the same horizon-mismatch
   caveats inherited from the condor / vol-gate modules.

Net: these structures are conservative on cost and defined on risk; the *signal* is where the
result lives or dies, and the spec is explicit that the signal is the unproven variable.
