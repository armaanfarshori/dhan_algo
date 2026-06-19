# `_ENGINE.md` — Multi-Leg Options Backtest Engine (design spec)

**Target module:** `research/backtest/fno_strategies.py`
**Status:** engineering spec — implement directly from this, no further research required.
**Scope:** generalises the condor-specific `research/backtest/fno_condor.py` into a
strategy-agnostic, N-leg options engine for NIFTY weekly cycles. PAPER / research only.
Does NOT touch the equity engine (`research/backtest/engine.py`, `__main__.py`) or
`fno_condor.py` (left intact; this module *reuses* its primitives).

This module is **pure + deterministic** (no DB, no network) except for the thin
`cycles_from_db` re-export and the optional `__main__` CLI. Pricing/resolution/cost/metrics
functions are unit-testable without a database.

---

## 0. What we reuse vs. what is new

**Reuse verbatim (import, do not re-implement):**

| From | Symbol | Use |
|---|---|---|
| `research/backtest/fno_condor.py` | `black76_call(F,K,T,sigma)`, `black76_put(...)` | historical per-leg premium |
| `research/backtest/fno_condor.py` | `cycles_from_db(...)` | the cycle set (weekly / expiry_calendar) — **unchanged** |
| `research/backtest/fno_condor.py` | `go_no_go(metrics, capital=...)` | promotion gate — **unchanged** |
| `research/backtest/fno_condor.py` | `_round_to_step(value, step)` | strike-grid rounding (or re-derive locally) |
| `research/backtest/fno_costs.py` | `condor_costs(legs, exercise_intrinsic=...)`, `slippage(premium, pct=...)`, `NIFTY_LOT`, `OPTION_EXERCISE_STT_PCT` | costs — **do not bypass** |
| `core/fno_derived.py` | `implied_move(spot, straddle_iv, dte)` | short-strike placement |
| `ml/fno_vol_gate.py` | `gate_decision(rv, iv, k)`, `SELL_PREMIUM`, `BUY_PREMIUM`, `STAND_ASIDE`, `DEFAULT_K` | regime gate |

`condor_costs` is misnamed — it is already a **generic N-leg** cost function
(`legs: list[(premium_per_unit, qty_units, side)]`). It needs no change. (A thin alias
`leg_costs = condor_costs` MAY be added in `fno_strategies.py` for readability — optional.)

**New in `fno_strategies.py`:** `Leg`, `StrategySpec`, the builder functions, `price_legs`,
`price_legs_from_chain`, `resolve_legs`, `span_margin`, `run_strategy_backtest`,
`FNO_STRATEGIES`, and an optional `__main__` / `main()` CLI.

---

## 1. Leg model & strategy abstraction

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Literal

OptionType = Literal["CE", "PE"]
Side       = Literal["BUY", "SELL"]

@dataclass(frozen=True)
class Leg:
    """One option leg of a multi-leg strategy.

    strike is in index points (already snapped to the strike grid by the builder).
    qty_lots is a positive integer count of lots (always >= 1); BUY/SELL carries
    the direction — qty_lots is NEVER signed. Units = qty_lots * lot_size.
    """
    option_type: OptionType   # "CE" | "PE"
    side: Side                # "BUY" | "SELL"
    strike: float
    qty_lots: int = 1

    def signed(self) -> int:
        """+1 for SELL (premium received), -1 for BUY (premium paid)."""
        return +1 if self.side == "SELL" else -1
```

A **strategy builder** is a pure function with this exact signature:

```python
StrategyBuilder = Callable[..., list[Leg]]

def builder(
    spot: float,
    atm_strike: int,
    step: int,
    dte: int,
    sigma: float,            # annualised IV fraction (straddle_iv), used for implied_move
    params: dict[str, Any],  # per-strategy knobs (move_mult, wing_strikes, width_strikes, ...)
) -> list[Leg]:
    ...
```

The builder ONLY decides strikes/sides/types. It never prices, never reads the DB.
It receives `atm_strike` precomputed (`_round_to_step(spot, step)`) and `step` so it can
place strikes deterministically. `implied_move(spot, sigma, dte)` (already imported) gives
the expected move when a builder needs it (e.g. short straddle uses ATM; iron condor uses
`±move_mult·implied_move`).

Each strategy is registered with metadata, not just the builder:

```python
@dataclass(frozen=True)
class StrategySpec:
    name: str
    build: StrategyBuilder
    defined_risk: bool          # True => max-loss is bounded (spreads); False => naked short
    sell_premium: bool          # True => premium-selling (gate must say SELL_PREMIUM)
    needs_multi_expiry: bool     # True => requires term structure (INFEASIBLE historically)
    default_params: dict[str, Any] = field(default_factory=dict)
    span_model: str = "defined"  # "defined" | "naked_short" | "spread_naked_mix"
```

---

## 2. Pricing — two modes, one leg interface

A leg's **entry premium per unit** is obtained one of two ways. The engine never mixes
modes within a run.

### 2a. HISTORICAL mode (`mode="hist"`, the backtest path)

Black-76 undiscounted, single IV for all legs (skew ignored — same as the condor):

```python
def price_leg_hist(leg: Leg, spot: float, sigma: float, dte: int) -> float:
    """Black-76 premium per UNIT for one leg. F ≈ spot. T = dte/365."""
    T = dte / 365.0
    if leg.option_type == "CE":
        return black76_call(spot, leg.strike, T, sigma)
    return black76_put(spot, leg.strike, T, sigma)
```

- `sigma` source = the cycle's `straddle_iv` (which in the historical `cycles_from_db`
  `weekly` path is **India VIX/100** — a 30-day implied proxy for a ~5–10-DTE weekly).
- **HONEST CAVEAT (copy from condor, state in the module docstring):** a *single* IV for
  every strike ignores the volatility smile/skew — OTM puts trade richer, OTM calls cheaper.
  For premium-SELLERS this **understates** the short-leg credit (conservative — good for a
  GO verdict). For DEBIT/long-vol strategies (long straddle, long calendars) the same
  single-IV assumption is NOT conservative — it can overstate entry edge — so a GO on a
  debit strategy under historical Black-76 is **weaker** than a GO on a credit strategy and
  must be flagged in the strategy's own `.md`. Black-76 here is regime-dependent and is a
  screening tool, not a fill model.

### 2b. FORWARD / PAPER mode (`mode="chain"`, mirrors `core/fno_paper.py`)

Premium = the **real LTP** from `option_chain_snapshot` for the exact (expiry, strike,
option_type), nearest-strike fallback if the exact strike has no quote:

```python
def price_leg_chain(leg: Leg, ce: dict[int, float], pe: dict[int, float]) -> tuple[int, float]:
    """Return (actual_strike_used, ltp_per_unit) from the real chain.
    ce/pe map int(strike) -> ltp. Nearest-strike fallback when exact missing
    (mirror core.fno_paper._nearest_strike). Caller BAILS the whole cycle if any
    leg ltp is None or <= 0 (incomplete chain), exactly like record_paper_entry."""
```

`ce`/`pe` are built exactly as in `core/fno_paper.record_paper_entry` (load latest
`snapshot_time` for the nearest future expiry; `iv` in `option_chain_snapshot` is PERCENT —
`/100` for a fraction; `straddle_iv` for the gate is the mean of ATM CE/PE IV fractions).

> The forward-paper *driver* for these strategies is OUT OF SCOPE for this module — it lives
> with `core/fno_paper.py` (which currently hard-codes the condor). This spec defines
> `price_leg_chain` so that a future `core/fno_paper.py` generalisation can call the same
> builder + pricer. The backtest path (`run_strategy_backtest`) is `mode="hist"` only.

### 2c. Slippage (both modes)

Apply `slippage(premium, pct)` adversely **per leg** before computing credit/debit:
- SELL leg fills at `premium - slippage(premium)` (receive less).
- BUY leg fills at `premium + slippage(premium)` (pay more).

```python
def fill_price(leg: Leg, mid: float, slip_pct: float = 0.005) -> float:
    s = slippage(mid, slip_pct)
    return mid - s if leg.side == "SELL" else mid + s
```

### 2d. Net entry credit/debit

```python
def net_premium_per_unit(legs, fills) -> float:
    """Σ leg.signed() * fill_price  (>0 = net CREDIT received, <0 = net DEBIT paid)."""
    return sum(leg.signed() * f for leg, f in zip(legs, fills))
```

---

## 3. Resolution at expiry (intrinsic, index-close proxy)

Each leg settles to **intrinsic value at the index settlement price** `S` (the
`expiry_spot` from the cycle = NIFTY daily close — **NOT** the NSE Final Settlement Price /
FSP; keep the condor's FSP caveat verbatim). Cash-settled index options.

```python
def leg_intrinsic(leg: Leg, S: float) -> float:
    """Per-unit intrinsic at expiry (buyer's value, always >= 0)."""
    if leg.option_type == "CE":
        return max(S - leg.strike, 0.0)
    return max(leg.strike - S, 0.0)

def resolve_legs(legs, entry_net_per_unit, expiry_spot, lot=NIFTY_LOT) -> dict:
    """Gross P&L at expiry, per the WHOLE position (all lots).

    For each leg, the writer's (our) P&L from holding to expiry is:
        SELL leg:  +entry_fill  - intrinsic     (kept premium minus payout)
        BUY  leg:  -entry_fill  + intrinsic     (paid premium recovered as intrinsic)
    Aggregating premium into entry_net_per_unit (Σ signed*fill) and intrinsic
    separately:
        gross_per_unit = entry_net_per_unit
                         - Σ leg.signed() * leg_intrinsic(leg, S)
        gross_pnl      = Σ_legs  gross_per_unit_for_leg * leg.qty_lots * lot
    """
```

**Per-leg lot weighting matters** — unlike the symmetric condor (all legs same qty), a
general strategy may have unequal `qty_lots` per leg (e.g. ratio spreads). Compute P&L
**leg-by-leg** then sum:

```
net_pnl_per_position =
   Σ_legs  leg.signed() * (entry_fill_leg - leg_intrinsic(leg, S)) * leg.qty_lots * lot
```

This reduces to `resolve_condor` when all four legs have qty_lots=1 and sides are the condor
pattern — assert this equivalence in a unit test (see §10, T9).

> **Caveat (copy verbatim):** settlement uses the NIFTY index daily CLOSE, not the NSE FSP
> (30-min VWAP of NIFTY futures 15:00–15:30 IST). A GO verdict is preliminary and must be
> re-validated with FSP before any live consideration.

---

## 4. Cost integration (every leg pays, no bypass)

Build the cost-leg list from the **executed fills** and call `condor_costs`:

```python
entry_cost_legs = [(fill_price(leg, mid), leg.qty_lots * lot, leg.side) for leg, mid in ...]
entry_costs = condor_costs(entry_cost_legs).total
```

Exit (held-to-expiry cash settlement, mirror `core/fno_paper.resolve_paper_trades`):
- No closing market order → **no brokerage** on exit.
- STT is still levied on the intrinsic of **ITM LONG legs** (exercise STT). Short legs
  expire worthless / are assigned with SELL-side STT already charged at entry.

```python
exercise_intrinsic = sum(
    leg_intrinsic(leg, S) * leg.qty_lots * lot
    for leg in legs if leg.side == "BUY" and leg_intrinsic(leg, S) > 0
)
exit_costs = OPTION_EXERCISE_STT_PCT * exercise_intrinsic   # direct, as in fno_paper
total_costs = entry_costs + exit_costs
net_pnl = gross_pnl - total_costs
```

`condor_costs` already charges, per leg: ₹20 brokerage × n_legs, 0.15% SELL-side STT on
premium turnover, NSE exchange ≈0.03553%, SEBI ₹10/crore, 0.003% stamp on BUY turnover, 18%
GST on (brokerage+exchange+SEBI). Do not re-derive — call it.

---

## 5. THE DENOMINATOR — return on SPAN margin (the profitability metric)

Notional-based ROC is the **wrong** denominator for options. The correct metric is
**return on SPAN+exposure margin** (the capital the broker actually blocks). We approximate
SPAN per strategy:

```python
def span_margin(spec: StrategySpec, legs, entry_net_per_unit, spot, lot=NIFTY_LOT,
                params=None) -> float:
    """Approximate the broker-blocked margin (₹) for the whole position."""
```

### 5a. Defined-risk (spreads: iron condor, vertical/credit spread, iron fly, butterfly)
`span_model="defined"`. The broker blocks **max theoretical loss** of the spread:

```
# widest adjacent same-type spread width, in points:
wing_width_pts = max over (long,short) pairs of |long_k - short_k|
max_loss_per_unit = max(0, wing_width_pts - entry_net_per_unit)   # entry_net>0 = credit
span = max_loss_per_unit * lot
```

For a **debit** spread (long vertical, long fly) entry_net_per_unit < 0; max loss = the net
debit paid, so `span = abs(entry_net_per_unit) * lot`. Use:
`span = max(net_debit_paid, wing_width - net_credit) * lot`, whichever the structure implies.
Caveat: exact SPAN ≤ max-loss for some defined-risk spreads (SPAN can be marginally lower),
so this is a **conservative (slightly high) denominator → understates ROM** — safe for a GO.

### 5b. Undefined-risk shorts (short straddle, short strangle, naked short)
`span_model="naked_short"`. No max loss exists. Approximate the SPAN+exposure block as a
**percentage of underlying notional per short lot**, plus the premium credit it earns back:

```
notional_per_lot = spot * lot
short_lots = Σ qty_lots over SELL legs
span_pct = params.get("span_pct", 0.12)   # ~12% of notional — NIFTY index SPAN+exposure
                                           # is empirically ~10–15%; PARAMETER, document it
span = span_pct * notional_per_lot * short_lots
```

- `span_pct` is a **named parameter** (default 0.12) so it is tunable and auditable.
  Rationale: SEBI/NSE SPAN+exposure for NIFTY short options runs ~10–15% of notional
  depending on VIX; 0.12 is a defensible mid. **This is an approximation, not a SPAN
  replication** — state in the module docstring that a true SPAN file (NSE SPAN parameters)
  would refine it, and that higher-VIX cycles block more margin than this flat % captures
  (so ROM is slightly OPTIMISTIC in high-vol regimes — the one place this denominator is
  NOT conservative; flag it).

### 5c. Mixed (e.g. ratio spreads with a naked tail)
`span_model="spread_naked_mix"`: `span = defined_part_max_loss + naked_part(span_pct·notional)`.

### 5d. The metric
```
return_on_margin = net_pnl / span        # per trade
portfolio ROM    = Σ net_pnl / mean(span across trades)   # report both
```

`return_on_margin` is the headline profitability number reported alongside the legacy
`return_on_capital`. **It is THE metric** the strategy `.md` files compare on.

---

## 6. Backtest loop

```python
def run_strategy_backtest(
    spec: StrategySpec,
    cycles: list[dict[str, Any]],   # from cycles_from_db (UNCHANGED loader)
    params: dict[str, Any] | None = None,
    *,
    k: float = DEFAULT_K,
    capital: float = 200_000.0,
    lot: int = NIFTY_LOT,
    step: int = 50,
    slip_pct: float = 0.005,
) -> dict[str, Any]:
    """Run `spec` over `cycles`. Returns the SAME metrics dict shape as
    fno_condor.run_backtest PLUS return-on-margin fields. Reuses go_no_go."""
```

Per cycle (same guards as `run_backtest`):
1. Skip if any of `spot/straddle_iv/realized_vol_20d/dte/expiry_spot` is None.
2. **Gate** (see §9): for `spec.sell_premium` strategies require
   `gate_decision(rvol, iv, k) == SELL_PREMIUM`; for debit/long-vol require `BUY_PREMIUM`;
   skip otherwise.
3. `atm_strike = _round_to_step(spot, step)`; `legs = spec.build(spot, atm_strike, step, dte,
   straddle_iv, {**spec.default_params, **(params or {})})`.
4. Price each leg (`mode="hist"`: `price_leg_hist`), apply `fill_price` adversely.
5. `entry_net = net_premium_per_unit(legs, fills)`.
6. `gross = resolve_legs(legs, entry_net, expiry_spot, lot)`; costs per §4; `net`.
7. `span = span_margin(spec, legs, entry_net, spot, lot, params)`.
8. Append a `StrategyTrade` record (analogue of `CondorTrade`, generalised to `legs:
   list[Leg]`, `fills: list[float]`, `entry_net`, `span`, plus diagnostics).

**Metrics dict — identical keys to `run_backtest`** (`trades`, `n_cycles`, `n_trades`,
`win_rate`, `profit_factor`, `sharpe` [×√52 weekly], `max_drawdown` [negative ₹],
`net_pnl`, `return_on_capital`) **PLUS**:
- `return_on_margin` — `net_pnl / sum(span over trades)` (0.0 if no trades).
- `mean_span` — average blocked margin per trade (₹).
- `sharpe_is` / `sharpe_oos` — Sharpe on an in-sample / out-of-sample **date split** (split
  cycles 70/30 chronologically — NEVER random on a time series; reuse the condor's per-trade
  Sharpe formula on each half; if a half has <2 trades, that Sharpe is 0.0).
- `strategy` — `spec.name`.

`metrics["go_no_go"] = go_no_go(metrics, capital=capital)` — **reused unchanged**. (go_no_go
reads `net_pnl/profit_factor/sharpe/max_drawdown/n_trades>=30`; ROM is informational in the
report, not yet a go_no_go criterion — note this so a future revision can add an ROM floor.)

Empty-trades branch returns the same zero-filled dict shape (mirror `run_backtest`).

---

## 7. Registry + selector

```python
FNO_STRATEGIES: dict[str, StrategySpec] = {
    "short_straddle":  StrategySpec("short_straddle",  build_short_straddle,
                          defined_risk=False, sell_premium=True,  needs_multi_expiry=False,
                          span_model="naked_short", default_params={"span_pct": 0.12}),
    "short_strangle":  StrategySpec("short_strangle",  build_short_strangle,
                          defined_risk=False, sell_premium=True,  needs_multi_expiry=False,
                          span_model="naked_short",
                          default_params={"move_mult": 1.0, "span_pct": 0.12}),
    "iron_condor":     StrategySpec("iron_condor",     build_iron_condor,
                          defined_risk=True,  sell_premium=True,  needs_multi_expiry=False,
                          span_model="defined",
                          default_params={"move_mult": 1.5, "wing_strikes": 2}),
    "iron_fly":        StrategySpec("iron_fly",        build_iron_fly,
                          defined_risk=True,  sell_premium=True,  needs_multi_expiry=False,
                          span_model="defined", default_params={"wing_strikes": 4}),
    "credit_put_spread": StrategySpec("credit_put_spread", build_credit_put_spread,
                          defined_risk=True,  sell_premium=True,  needs_multi_expiry=False,
                          span_model="defined",
                          default_params={"short_mult": 1.0, "width_strikes": 2}),
    "long_straddle":   StrategySpec("long_straddle",   build_long_straddle,
                          defined_risk=True,  sell_premium=False, needs_multi_expiry=False,
                          span_model="defined"),   # debit paid = max loss
    "calendar_spread": StrategySpec("calendar_spread", build_calendar_spread,
                          defined_risk=True,  sell_premium=True,  needs_multi_expiry=True,
                          span_model="defined"),   # INFEASIBLE historically — see §8
}
```

Builders (concrete, deterministic; `M = implied_move(spot, sigma, dte)`):
- `build_short_straddle` → `[SELL CE @atm, SELL PE @atm]`.
- `build_short_strangle` → shorts at `atm ± round(move_mult·M)` (CE up, PE down).
- `build_iron_condor` → the existing `build_condor` mapping → 4 `Leg`s
  (`SELL PE short_put_k, BUY PE long_put_k, SELL CE short_call_k, BUY CE long_call_k`).
  Reuse `build_condor` internally, convert the dict to legs.
- `build_iron_fly` → short straddle @atm + long wings at `atm ± wing_strikes·step`.
- `build_credit_put_spread` → `SELL PE @ atm-round(short_mult·M)`,
  `BUY PE @ (that strike - width_strikes·step)`.
- `build_long_straddle` → `[BUY CE @atm, BUY PE @atm]`.
- `build_calendar_spread` → SELL near-expiry + BUY far-expiry, same strike (needs 2 expiries).

**Selector:** an optional `main()` / `if __name__ == "__main__":` block in *this* module
(never edit equity `__main__.py`). Use `argparse`:
`--strategy <name>` (key into `FNO_STRATEGIES`), `--mode weekly|expiry_calendar`,
`--k`, `--capital`, `--span-pct`, plus any `--param key=val` passthrough. It calls
`cycles = cycles_from_db(mode=...)`, `run_strategy_backtest(FNO_STRATEGIES[name], cycles,
params, ...)`, prints the metrics dict + the `go_no_go` reason. `--strategy all` loops the
registry and prints a comparison table.

---

## 8. Feasibility matrix (be honest)

| Strategy | Data need | Historical backtest? | Notes |
|---|---|---|---|
| short_straddle | single-expiry close + IV proxy | ✅ YES | naked_short SPAN approx |
| short_strangle | single-expiry | ✅ YES | naked_short SPAN approx |
| iron_condor | single-expiry | ✅ YES | the validated reference path |
| iron_fly | single-expiry | ✅ YES | defined risk |
| credit_put_spread | single-expiry | ✅ YES | defined risk |
| long_straddle | single-expiry | ⚠️ YES but WEAK | debit under single-IV Black-76 is NOT conservative (§2a) → screening only |
| **calendar_spread** | **TWO expiries / term structure** | ❌ **INFEASIBLE historically** | our `cycles_from_db` carries ONE IV per cycle (VIX/100); we have no historical per-expiry term structure → cannot price near-vs-far honestly. **FORWARD-PAPER ONLY** via the real chain (multiple `expiry_date` rows exist live in `option_chain_snapshot`). Mark `needs_multi_expiry=True`; `run_strategy_backtest` must **refuse** (raise/return a no-go) any `needs_multi_expiry` strategy in `mode="hist"`. |
| diagonal_spread | TWO expiries + strikes | ❌ INFEASIBLE historically | same as calendar → forward-paper only |

`run_strategy_backtest` guard: `if spec.needs_multi_expiry: return <no-go metrics with reason
"requires term structure — historically infeasible; forward-paper only">`.

---

## 9. Vol-gate integration

`ml/fno_vol_gate.gate_decision(realized_vol, implied_vol, k)` returns
`SELL_PREMIUM` / `BUY_PREMIUM` / `STAND_ASIDE` (fail-open). Routing per strategy:

- **Premium-selling** (`spec.sell_premium=True`: short straddle/strangle, iron
  condor/fly, credit spreads, calendar [short near leg]): trade ONLY when
  `gate_decision == SELL_PREMIUM`. Implied is rich vs. expected realized → harvest VRP.
- **Directional / debit / long-vol** (`spec.sell_premium=False`: long straddle, long
  vertical): trade ONLY when `gate_decision == BUY_PREMIUM` (realized expected to exceed
  implied → long vol pays). A debit strategy taken in a SELL_PREMIUM regime is buying
  expensive vol — explicitly skip it.
- `STAND_ASIDE` → never trade (either side).

`k` defaults to `DEFAULT_K=0.9`; `calibrate_threshold` (existing) can supply a per-run k.
The gate consumes the cycle's `realized_vol_20d` (NIFTY spot persistence proxy) and
`straddle_iv` (VIX/100 historically, real ATM-IV fraction forward) — **same inputs the
condor uses**; no change to the gate.

---

## 10. Unit-test cases (inputs → expected)

All pure; `lot=65`, `step=50`, slippage default 0.5% unless stated. Premiums via the
real `black76_*` (assert with `pytest.approx`, rel=1e-3).

1. **`Leg.signed`** — `Leg("CE","SELL",24000).signed()==+1`; `Leg("PE","BUY",24000).signed()==-1`.

2. **`build_short_straddle`** — `spot=24013, atm=24000` ⇒ exactly
   `[Leg("CE","SELL",24000,1), Leg("PE","SELL",24000,1)]` (2 legs, both SELL, both @atm).

3. **`build_iron_condor` ≡ `build_condor`** — for `spot=24000, M=200, move_mult=1.5,
   wing_strikes=2`, expect shorts at 24300/23700, longs at 24400/23600, sides
   `SELL PE 23700, BUY PE 23600, SELL CE 24300, BUY CE 24400` (4 legs).

4. **`leg_intrinsic`** — `Leg("CE",_,24000)` at `S=24250` ⇒ 250; `Leg("PE",_,24000)` at
   `S=24250` ⇒ 0; at `S=23800` PE⇒200, CE⇒0.

5. **`net_premium_per_unit` sign** — short straddle with CE fill 120, PE fill 110 ⇒
   `+230` (credit). Long straddle same fills ⇒ `-230` (debit). (Builder side flips sign.)

6. **`resolve_legs` short straddle, max win** — shorts @24000, `S=24000` (pin),
   entry_net=+230/unit ⇒ gross = `230*65 = 14950` (both legs expire worthless, keep credit).

7. **`resolve_legs` short straddle, loss** — shorts @24000, `S=24500`, entry_net=+230 ⇒
   CE intrinsic 500, PE 0; per-unit = `230 - (signed_CE*500 + signed_PE*0)` =
   `230 - (+1*500) = -270` ⇒ gross = `-270*65 = -17550`.

8. **`resolve_legs` unequal qty (ratio)** — `SELL 1×CE@24000`, `BUY 2×CE@24200`,
   `S=24500`, entry fills CE_short=300, CE_long=180. Verify leg-by-leg lot weighting:
   short leg `(+1)*(300 - 500)*1*65`, long leg `(-1)*(180 - 300)*2*65`; assert the sum
   matches the documented formula (catches the qty-weighting bug the condor never exercises).

9. **Condor equivalence** — `resolve_legs` on the 4 iron-condor legs (all qty_lots=1) with a
   given entry_net and `expiry_spot` equals `resolve_condor(strikes, entry_net, expiry_spot)`
   `["gross_pnl"]` to within float tolerance.

10. **`span_margin` defined (condor)** — wing_width 100 pts, entry credit 40/unit ⇒
    `max(0, 100-40)*65 = 3900`. Debit fly net_debit 30/unit ⇒ `30*65 = 1950`.

11. **`span_margin` naked_short** — short straddle, `spot=24000, lot=65, span_pct=0.12`,
    1 short CE + 1 short PE ... define short_lots as **per-position** (NIFTY straddle blocks
    one combined SPAN, not double): assert the chosen convention in the test (recommend
    `short_lots = max qty_lots among SELL legs = 1` for a straddle ⇒
    `0.12*24000*65*1 = 187200`). Document this convention in `span_margin`.

12. **`run_strategy_backtest` gate routing + ROM** — 3 synthetic cycles: one SELL_PREMIUM
    (`rv=0.10, iv=0.15`), one BUY_PREMIUM (`rv=0.20, iv=0.15`), one STAND_ASIDE
    (`rv=0.14, iv=0.15, k=0.9`). For `iron_condor` (sell_premium) expect `n_trades==1`
    (only the SELL_PREMIUM cycle); metrics dict has `return_on_margin` + `mean_span` keys;
    `go_no_go` present. For `long_straddle` (sell_premium=False) expect `n_trades==1` (only
    BUY_PREMIUM cycle).

13. **Multi-expiry refusal** — `run_strategy_backtest(FNO_STRATEGIES["calendar_spread"],
    cycles, mode/hist)` returns `n_trades==0` and a `go_no_go` reason citing
    "requires term structure / forward-paper only" (does NOT raise).

(Tests 1–11 are pure-math and fast; 12–13 use small in-memory `cycles` lists — no DB.)

---

## 11. Honesty ledger (must appear in the module docstring)

1. Black-76 single-IV ignores skew → conservative for credit, **non-conservative for debit**.
2. Settlement = index daily close, not NSE FSP → re-validate any GO with FSP.
3. `straddle_iv` historically = VIX/100 (30-day) used for ~7-DTE weeklies → term-structure bias.
4. `span_pct` naked-short denominator is a flat ~12% approximation; true SPAN scales with VIX
   → ROM is slightly optimistic in high-vol regimes (the one non-conservative denominator).
5. Multi-expiry strategies are historically infeasible → forward-paper only.
6. ROM is the headline metric but is NOT yet a `go_no_go` criterion (informational for now).
