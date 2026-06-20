# 02 — Strategy → Regime Routing Policy (orchestrator core)

**Status:** plan / implementation-ready. v1 = TRANSPARENT RULES (no ML).
**Owner lane:** orchestration layer (this repo, `feat/fno-orchestrator`). Calls
`research/backtest/fno_strategies.py` builders + `ml/fno_vol_gate.py`; does NOT edit either.
**Scope:** picks ONE action per cycle per index from the GO set. Defined-risk, vol-gated,
premium-selling only. PAPER. NIFTY-now / index-agnostic interface.

---

## 0. What this document is

The orchestrator's job is **selection**: given a regime read for a cycle, choose exactly one of

```
{ iron_condor, bull_put_spread, credit_put_spread, broken_wing_condor, STAND_ASIDE }
```

This is the only GO set. It is fixed by the backtest evidence in `_CONTEXT.md`:
undefined-risk (short straddle/strangle, jade_lizard, ratio), directional debit, and
long-premium structures are **excluded by construction** — they are never selectable, so no
rule can route to them. Every routing rule below maps a regime onto a member of the GO set.

> This spec depends on the regime-signal definitions in `01_regime_signals.md`. The signals
> it consumes are named here exactly as that doc must define them; if a name differs there,
> that doc is the source of truth and the binding in §6 is updated to match.

---

## 1. Evidence the policy is built on (from `_CONTEXT.md` + condor report)

GO candidates, vol-gated, weekly NIFTY, **ROM = return-on-SPAN-margin** (headline metric):

| strategy | gated ROM (GO) | structure | directional posture | builder + default params |
|---|---|---|---|---|
| **bull_put_spread**    | **7.19%** | put credit spread, far OTM (`move_mult≈0.5`) | bullish lean (profits if spot ≥ short put) | `build_bull_put_spread` `{move_mult:0.5, width:2}` |
| **iron_condor**        | **3.91%** (ungated 2.22%) | symmetric short PE+CE w/ wings | neutral / range-bound | `build_iron_condor` `{move_mult:1.5, wing_strikes:2}` |
| **credit_put_spread**  | **2.70%** | put credit spread, ATM-er short (`short_mult≈1.0`) | mild bullish, more aggressive than bull_put | `build_credit_put_spread` `{short_mult:1.0, width_strikes:2}` |
| **broken_wing_condor** | **2.67%** | asymmetric condor (skewed wings) | neutral with directional skew | `build_broken_wing_condor` `{move_mult:1.5, base_wing:100, skew:1.5}` |

Key facts that drive the rules:

1. **The vol-gate is the master switch.** `ml/fno_vol_gate.gate_decision(realized, implied, k)`
   returns `SELL_PREMIUM` only when `realized_vol < k·implied_vol` (k≈0.9). Every GO strategy is
   a premium *seller* (`sell_premium=True` in the registry). **If the gate is not `SELL_PREMIUM`,
   nothing in the GO set has an edge** → STAND_ASIDE. The condor report measured the VRP that
   powers this: India VIX > NIFTY realized on 100% of SELL days, mean edge +3.8 vol pts. The gate
   *is* where the +1.69 pts of condor ROM (3.91 vs 2.22 ungated) comes from. The router must
   never deploy when the gate says BUY_PREMIUM or STAND_ASIDE — there is no defined-risk
   long-vol GO product to route a BUY into.

2. **bull_put_spread has ~2× the ROM of any other GO strategy** (7.19%), but it is a *directional*
   (bullish) bet that only its strike asymmetry hides. It earns its ROM only when the trend is not
   down. The policy must NOT default to it blindly: in a down/falling-trend regime the short put
   is the wrong side and the high historical ROM does not transfer.

3. **iron_condor is the neutral workhorse** — symmetric, no directional dependence, second-best
   ROM and the most robust (it survives ungated). It is the policy's **default deployment** when
   the gate fires and trend is genuinely flat.

4. **credit_put_spread and broken_wing_condor are conditional refinements**, not first-choice
   actions: credit_put_spread is bull_put's more-aggressive sibling (closer short = bigger credit,
   thinner cushion → lower ROM, used when we want bullish exposure but bull_put's far-OTM short
   collects too little in a low-IV cycle); broken_wing_condor is iron_condor with a directional
   skew, used when the regime is neutral-but-tilted (one side carries more breach risk).

---

## 2. Regime signals consumed (inputs per cycle)

Per `01_regime_signals.md`. Names used by the binding in §6:

| signal | type | meaning | source |
|---|---|---|---|
| `gate_state` | enum `{SELL_PREMIUM, BUY_PREMIUM, STAND_ASIDE}` | VRP gate verdict | `fno_vol_gate.gate_decision` |
| `vrp` | float (vol pts) | `implied_vol − realized_vol` (positive = sell edge) | derived from gate inputs |
| `iv_rank` | float `[0,1]` | percentile of current implied_vol in trailing window | `01_regime_signals` |
| `iv_level` | float | current annualised implied_vol (fraction) | `option_atm_iv` / VIX proxy |
| `trend` | enum `{UP, FLAT, DOWN}` | underlying trend classification | `01_regime_signals` |
| `trend_strength` | float `[0,1]` | confidence/magnitude of the trend label | `01_regime_signals` |
| `dte` | int | days to expiry of the cycle | cycle metadata |

If `01_regime_signals.md` exposes only a subset (e.g. no `trend_strength`), the policy degrades
gracefully: missing `trend_strength` → treat as `1.0` (full strength) only when `trend≠FLAT`,
else the FLAT branch is taken. Any missing required signal (`gate_state`, `trend`, `dte`) →
STAND_ASIDE (fail-safe, mirrors the gate's fail-open-to-flat contract).

---

## 3. The policy — decision order (precedence)

Evaluated top-to-bottom; **first matching rule wins**. This ordering IS the tie-break (§5).

```
INPUT: gate_state, vrp, iv_rank, iv_level, trend, trend_strength, dte, params P

R0  GATE MASTER SWITCH
    if gate_state != SELL_PREMIUM:                      -> STAND_ASIDE
        # BUY_PREMIUM / STAND_ASIDE: no GO product has edge.

R1  DTE WINDOW
    if dte < P.dte_min or dte > P.dte_max:              -> STAND_ASIDE
        # outside the weekly window the backtest validated (gamma/pin risk if too
        # short; IV not yet a clean weekly read if too long).

R2  MINIMUM EDGE
    if vrp < P.vrp_min:                                 -> STAND_ASIDE
        # gate fired but the premium cushion is too thin to clear costs (≥0.5%
        # slippage stack). The gate uses a ratio (k); this is an absolute-edge floor.

R3  IV FLOOR
    if iv_level < P.iv_floor:                           -> STAND_ASIDE
        # ultra-low IV → credit collected won't beat the fixed ₹20/order + STT stack
        # regardless of VRP ratio. Premium selling is uneconomic.

    # --- past here the gate is GO, DTE is in-window, edge & IV clear floors ---

R4  STRONG DOWNTREND  (premium selling on the put side is hazardous)
    if trend == DOWN and trend_strength >= P.trend_strong:
        if iron_condor allowed:                         -> iron_condor
            # neutral symmetric structure; do NOT sell a bullish put spread into a
            # falling market. Condor's call side also benefits from the move away.
        else:                                           -> STAND_ASIDE

R5  CLEAR UPTREND  (the bull_put regime — highest ROM)
    if trend == UP and trend_strength >= P.trend_strong:
        if iv_rank >= P.iv_rank_aggressive:             -> credit_put_spread
            # high IV + bullish: take the closer (ATM-er) short for a bigger credit;
            # the elevated IV pays for the thinner cushion.
        else:                                           -> bull_put_spread
            # the headline 7.19% ROM regime: bullish, far-OTM put credit.

R6  NEUTRAL-WITH-SKEW  (mild trend, not strong enough for a directional spread)
    if trend != FLAT and trend_strength >= P.trend_skew:
        -> broken_wing_condor   (skew TOWARD the trend; see §4)
            # neutral core but tilt the wings to give room on the trend side.

R7  NEUTRAL DEFAULT  (range-bound — the workhorse)
    -> iron_condor
        # symmetric, directionally-robust, best risk-adjusted GO product.
```

### Decision table (collapsed, for the gate-GO branch)

| gate | dte/vrp/iv floors | trend | trend_strength | iv_rank | → action |
|---|---|---|---|---|---|
| ≠SELL_PREMIUM | — | — | — | — | **STAND_ASIDE** |
| SELL_PREMIUM | any floor fails | — | — | — | **STAND_ASIDE** |
| SELL_PREMIUM | pass | DOWN | ≥ `trend_strong` | — | **iron_condor** |
| SELL_PREMIUM | pass | UP | ≥ `trend_strong` | ≥ `iv_rank_aggressive` | **credit_put_spread** |
| SELL_PREMIUM | pass | UP | ≥ `trend_strong` | < `iv_rank_aggressive` | **bull_put_spread** |
| SELL_PREMIUM | pass | UP or DOWN | `trend_skew` ≤ s < `trend_strong` | — | **broken_wing_condor** (skew to trend) |
| SELL_PREMIUM | pass | FLAT (or weak) | < `trend_skew` | — | **iron_condor** |

---

## 4. Rationale per branch (tied to ROM evidence)

- **R0 gate master switch.** Every GO product sells premium. The +1.69 ROM pts the gate adds to
  the condor (3.91 vs 2.22 ungated) and the move of bull_put from NO-GO (ungated) to 7.19% GO are
  *entirely* the gate's doing. Outside SELL_PREMIUM the measured edge does not exist, and there is
  no defined-risk long-vol member of the GO set to route a BUY into → only honest action is to sit out.

- **R4 downtrend → iron_condor (not a put spread).** bull_put / credit_put are bullish: their
  short put is the losing side in a falling market, and the 7.19% / 2.70% ROM were *not* measured
  conditional on a downtrend. The symmetric condor has no directional dependence; its call wing
  even benefits as spot moves down toward/through the put-side cushion built at ±1.5×move. We route
  the safest neutral structure, not the highest-ROM one. If condor itself is disabled (§6
  `enabled`), stand aside rather than sell a bullish spread into weakness.

- **R5 uptrend → bull_put_spread (headline regime), credit_put_spread when IV is rich.**
  bull_put's 7.19% ROM is a *directional* number; it transfers only when the trend is up. Default
  to bull_put (far-OTM short, `move_mult≈0.5`, the biggest cushion). When `iv_rank` is high the
  market is paying up for vol, so the ATM-er credit_put_spread (`short_mult≈1.0`) harvests a larger
  credit and the elevated IV compensates for its thinner cushion — that is the only situation where
  credit_put's lower *average* ROM (2.70%) is the right marginal call over bull_put.

- **R6 neutral-with-skew → broken_wing_condor.** When trend is present but too weak to justify a
  one-sided put spread, the symmetric condor leaves equal room on both sides — wasteful, since one
  side carries more breach risk. broken_wing_condor (2.67% ROM) tilts the wings: **widen the wing
  on the trend side, tighten the opposite**, i.e. for UP skew the call-side room out / put-side in;
  for DOWN skew the reverse. Implemented via the builder's `skew` param (default 1.5) with the
  sign/orientation set by `trend` — see §6 `skew_orientation`.

- **R7 neutral → iron_condor.** Range-bound is the condor's home: 3.91% gated ROM, the most robust
  GO product (only one positive *ungated*), zero directional dependence. It is the default and the
  catch-all so the policy is total (every gate-GO regime resolves to an action).

**Why not the highest-ROM strategy everywhere?** Because 7.19% is conditional on a bullish regime.
Routing bull_put unconditionally would sell puts into downtrends where that ROM was never
observed and the tail is against us. The policy trades *some* average ROM for regime-honesty and
defined-risk discipline — exactly the selection edge the orchestrator exists to capture.

---

## 5. Tie-breaks

The rule list is **strictly ordered**, so at most one action is ever produced — there is no genuine
multi-match to break. Where two regimes are adjacent, the precedence resolves it deterministically:

1. **Risk-off beats yield.** R4 (downtrend → condor) precedes R5 (uptrend → bull_put). A
   contradictory signal set can't both be UP and DOWN, but if `trend_strength` is ambiguous the
   *more conservative* branch is reached first by construction (DOWN check before UP).
2. **Directional spread beats skewed condor beats symmetric condor** when trend strength is on a
   boundary: `trend_strong` (R5) > `trend_skew` (R6) > FLAT default (R7). Raising `trend_strong`
   pushes more cycles into the safer condor family; raising `trend_skew` pushes more into the plain
   symmetric condor. Both are tunable (§6).
3. **All floors beat all strategy picks.** R1–R3 (dte / vrp / iv) sit above every strategy branch,
   so a marginal cycle is stood aside regardless of how attractive its trend label looks.
4. **Disabled-strategy fallback** (§6 `enabled`): if a rule's chosen strategy is disabled, fall
   through to the next rule that yields an *enabled* strategy; if none, STAND_ASIDE. This makes the
   per-index registry (some indices may not support all structures) safe without changing the table.

---

## 6. Parameters (tunable / backtestable)

All thresholds are policy parameters, not constants — exposed as a single config object so the
policy can be swept and the chosen point recorded with the backtest (provenance). Defaults are
seeded from the condor report + builder defaults; **they are starting points to be tuned by the
sweep, not validated values.**

```python
@dataclass(frozen=True)
class RoutingParams:
    # --- gate / floors (R0–R3) ---
    gate_k: float          = 0.90    # passed to fno_vol_gate.gate_decision; condor report k≈0.898
    dte_min: int           = 1       # avoid expiry-day pin/gamma
    dte_max: int           = 7       # weekly window the backtest validated
    vrp_min: float         = 0.0     # absolute vol-pt edge floor; report mean edge ≈ +3.8 pts
    iv_floor: float        = 0.0     # min annualised IV (fraction) for premium selling to pay
    iv_rank_window: int    = 252     # lookback for iv_rank percentile (defined in 01_regime_signals)

    # --- trend thresholds (R4–R7) ---
    trend_strong: float    = 0.60    # >= this => directional branch (R4/R5)
    trend_skew:   float    = 0.30    # [trend_skew, trend_strong) => broken_wing (R6); below => flat
    iv_rank_aggressive: float = 0.70 # uptrend + iv_rank>=this => credit_put_spread instead of bull_put

    # --- per-strategy enable + builder params (forwarded to fno_strategies builders) ---
    enabled: frozenset = frozenset({
        "iron_condor", "bull_put_spread", "credit_put_spread", "broken_wing_condor"})
    iron_condor_params:       dict = field(default_factory=lambda: {"move_mult": 1.5, "wing_strikes": 2})
    bull_put_params:          dict = field(default_factory=lambda: {"move_mult": 0.5, "width": 2})
    credit_put_params:        dict = field(default_factory=lambda: {"short_mult": 1.0, "width_strikes": 2})
    broken_wing_params:       dict = field(default_factory=lambda: {"move_mult": 1.5, "base_wing": 100.0,
                                                                    "skew": 1.5, "wing_in_move_units": False})
    skew_orientation: str = "toward_trend"  # broken_wing: widen wing on the trend side
```

**Tuning / sweep notes**

- `gate_k`, `vrp_min`, `iv_floor` move the **stand-aside frequency** (selectivity vs deployment
  count). The condor report calibrated `k` to a ~70% SELL pass-rate via
  `fno_vol_gate.calibrate_threshold`; reuse that to seed `gate_k` per index/era, then sweep
  `vrp_min`/`iv_floor` for the cost-clearing floor.
- `trend_strong`, `trend_skew`, `iv_rank_aggressive` move the **mix** across the four strategies.
  Sweep these on a 2D grid and report ROM + max-DD per cell; pick the point that maximises blended
  ROM subject to max-DD < 15% of margin (the existing `go_no_go` discipline).
- `enabled` + builder-param dicts make the policy **index-agnostic**: a per-index registry supplies
  step/lot/expiry-calendar and may drop unsupported structures; the table is unchanged.
- All builder-param dicts are passed through verbatim to the `fno_strategies` builders — the policy
  never re-implements strike math. It only *selects*.

---

## 7. Interface (implementation-ready)

```python
def route(signals: RegimeSignals, params: RoutingParams) -> RoutingDecision:
    """Pure function. Returns exactly one action from the GO set, with rationale.

    RoutingDecision = {
        action: str,            # one of GO_SET ∪ {"STAND_ASIDE"}
        reason: str,            # which rule fired (e.g. "R5/uptrend->bull_put")
        strategy_params: dict,  # builder params to forward (empty for STAND_ASIDE)
        signals: dict,          # echo of the regime read, for provenance/logging
    }
    No I/O, no DB, never raises (fail-safe -> STAND_ASIDE).  The caller (cycle loop /
    backtest harness) maps action -> STRATEGY_REGISTRY[action].build(...) and prices it.
    """
```

Properties the implementation must hold (also the test matrix):

1. **Totality** — every `(gate_state, trend, …)` combination yields exactly one action.
2. **Closure** — `action ∈ {iron_condor, bull_put_spread, credit_put_spread, broken_wing_condor,
   STAND_ASIDE}`; never an excluded (undefined-risk / directional-debit / long-premium) strategy.
3. **Gate dominance** — `gate_state != SELL_PREMIUM ⇒ STAND_ASIDE` (R0), unconditionally.
4. **Determinism** — same inputs → same output; no randomness, no clock, no global state.
5. **Fail-safe** — any missing required signal (`gate_state`/`trend`/`dte`) → STAND_ASIDE.
6. **Disabled-safe** — a disabled strategy is never returned (§5.4 fallback).

---

## 8. v1 boundaries / out of scope (handed to later specs)

- **No ML.** v1 is a transparent ordered-rules policy by mandate. A learned router is a possible v2,
  but only once the forward real-IV paper log (the truth test in `_CONTEXT.md` §HARD REALITIES 2)
  gives labelled regime→outcome data.
- **No sizing / portfolio interaction.** This policy answers *which* strategy, not *how big* or how
  many concurrent positions. Sizing, margin budget, and concurrency live in a separate spec.
- **No intra-cycle management** (rolls, early exits, adjustments). Selection is at cycle entry.
- **ROM figures are PRELIMINARY** (VIX-as-weekly-IV proxy, close-not-FSP settlement, expiry-only —
  see condor report §5). The *ordering* of the strategies by ROM, which is what the policy relies
  on, is more robust than the absolute levels, but the whole table must be re-confirmed on real
  per-expiry ATM IV + FSP settlement before live. PAPER only until then.
```