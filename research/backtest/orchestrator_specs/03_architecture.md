# 03 — Orchestrator Architecture (module layout, interfaces, data flow)

> Read `_CONTEXT.md` first. This spec defines the **selection layer** that sits ON TOP of
> `research/backtest/fno_strategies.py`. The orchestrator **picks** a strategy per cycle (or
> stands aside); it **never** builds, prices, or resolves legs — that is `fno_strategies`' job.
> Index-agnostic by construction (all index-specific values arrive via an `IndexParams`).
>
> Lane discipline (`_CONTEXT.md` §Lanes): **do NOT edit `fno_strategies.py` internals.** The
> orchestrator imports it and calls its public surface. Everything below is additive.

---

## 1. Design principle — strict separation of concerns

| Concern | Owner | Module |
|---|---|---|
| Regime measurement (VRP gate, IV level/rank, trend, DTE) | regime layer | `core/fno_derived.py`, `ml/fno_vol_gate.py` (existing) |
| **Strategy SELECTION** (which GO strategy, or stand aside) | **orchestrator (NEW)** | `research/backtest/fno_orchestrator.py` |
| Leg CONSTRUCTION + PRICING + RESOLUTION + SPAN + costs | strategy engine | `research/backtest/fno_strategies.py` (existing, call only) |
| Cycle hydration from DB | data foundation | `fno_condor.cycles_from_db` (existing, re-exported) |

The orchestrator is a **router**: regime signals in → a routing decision out. It is the only
new "brain". Construction/pricing stays where the honesty ledger already lives (so every
GO/NO-GO caveat from `fno_strategies` is preserved untouched).

**Hard invariant:** the orchestrator emits a *choice* (a strategy name + params, or stand-aside).
It does NOT emit `Leg` objects. Leg construction happens exclusively inside
`spec.build(...)` called by `run_strategy_backtest`. This keeps a single source of truth for
strikes/pricing and means any future change to a builder needs zero orchestrator edits.

---

## 2. Module layout (file plan)

```
research/backtest/
├── fno_orchestrator.py        # NEW — the selection layer (this spec)
├── fno_strategies.py          # EXISTING — call, never edit (construction/pricing/registry)
├── fno_condor.py              # EXISTING — cycles_from_db, go_no_go (re-exported via fno_strategies)
└── orchestrator_specs/
    ├── _CONTEXT.md            # planning context
    ├── 01_*.md                # (sibling) regime-signal spec
    ├── 02_*.md                # (sibling) routing-policy spec
    └── 03_architecture.md     # THIS FILE

ml/
└── fno_vol_gate.py            # EXISTING — gate_decision / DEFAULT_K (call only)

core/
└── fno_derived.py             # EXISTING — implied_move, realized_vol (call only)

tests/
└── test_fno_orchestrator.py   # NEW — unit tests (pure; no DB)
```

**One new production file: `research/backtest/fno_orchestrator.py`.** Plus one new test file.
No edits to existing modules. The routing *policy* (the actual scoring/priority rules) is
defined by spec `02`; this file defines the *machinery* that applies whatever policy `02`
specifies, via a small pluggable `RoutingPolicy` protocol (§5) so the two specs compose
without coupling.

---

## 3. Data models (new, in `fno_orchestrator.py`)

All `@dataclass(frozen=True)` — pure, hashable, no DB. Mirrors the dataclass style already
used in `fno_strategies.Leg` / `StrategySpec`.

### 3.1 `IndexParams` — the index-agnostic registry entry

Everything index-specific that selection or downstream construction needs. NIFTY is the only
populated entry today; BANKNIFTY/FINNIFTY/etc. are added here once their data is ingested
(`_CONTEXT.md` §HARD-REALITY-1). **No NIFTY constants are hard-coded in orchestrator logic** —
they all flow from here.

```python
@dataclass(frozen=True)
class IndexParams:
    symbol: str                # "NIFTY"
    nifty_id: str              # index_bars security_id, e.g. "13"
    vix_id: str | None         # implied-vol proxy security_id, e.g. "21" (None → source="atm")
    lot: int                   # NIFTY_LOT today; per-index lot from fno_costs registry
    step: int                  # strike grid spacing (50 for NIFTY)
    iv_source: str             # "vix" | "atm" — which implied baseline cycles_from_db / gate use
    expiry_mode: str           # "weekly" | "expiry_calendar"  (passed to cycles_from_db.mode)
    has_history: bool          # False → forward-only (no historical option chains)

INDEX_REGISTRY: dict[str, IndexParams] = {
    "NIFTY": IndexParams("NIFTY", "13", "21", NIFTY_LOT, 50, "vix", "weekly", has_history=True),
    # BANKNIFTY/FINNIFTY/... : add when ingested; has_history likely False (forward-only).
}
```

`lot`/`step` are passed straight through to `run_strategy_backtest(..., lot=..., step=...)`
(both already accept them). `NIFTY_LOT` is imported from `research.backtest.fno_costs` exactly
as `fno_strategies` does — single source of truth.

### 3.2 `RegimeSignals` — the measured state of one cycle

The orchestrator's **input**. Computed by `regime_from_cycle()` (§4.2) from a raw cycle dict
(the same dict shape `cycles_from_db` yields). Pure: derived only from the cycle + `k`.

```python
@dataclass(frozen=True)
class RegimeSignals:
    entry_date: date
    expiry_date: date
    dte: int
    spot: float
    realized_vol: float        # cycle["realized_vol_20d"]
    implied_vol: float         # cycle["straddle_iv"] (annualised fraction)
    vrp: float                 # implied_vol - realized_vol   (signed VRP edge)
    iv_ratio: float            # realized_vol / implied_vol    (gate ratio; lower = richer IV)
    gate_label: str            # SELL_PREMIUM | BUY_PREMIUM | STAND_ASIDE (ml.fno_vol_gate)
    implied_move: float        # core.fno_derived.implied_move(spot, implied_vol, dte) or 0.0
    # Optional/extensible (filled if available; policy may ignore):
    iv_rank: float | None = None   # percentile of implied_vol in trailing window (regime spec 01)
    trend: float | None = None     # directional signal in [-1, 1] if a trend source exists
```

`gate_label` comes from `ml.fno_vol_gate.gate_decision(realized_vol, implied_vol, k=k)` — the
orchestrator does NOT re-derive the regime rule; it *consumes* the existing gate. `iv_rank` /
`trend` are the additional regime signals spec `01` defines; they are optional so the
orchestrator runs today with only the VRP gate and gains discrimination as `01` lands.

### 3.3 `RoutingDecision` — the orchestrator's output

```python
@dataclass(frozen=True)
class RoutingDecision:
    entry_date: date
    stand_aside: bool
    strategy: str | None             # registry key in FNO_STRATEGIES, None if stand_aside
    params: dict[str, Any] | None    # per-cycle param overrides for the builder, None if aside
    reason: str                      # human-readable rationale (logged + persisted)
    signals: RegimeSignals           # the regime snapshot the decision was made from
```

This is the **clean handoff boundary**: `strategy` is a *name* + `params` is a *dict*, both of
which are exactly what `run_strategy_backtest(FNO_STRATEGIES[strategy], cycles, params, ...)`
consumes. The orchestrator never constructs a `Leg`. A stand-aside is a first-class, explicit
outcome (not an empty list) — discipline for `_CONTEXT.md` (route OR stand aside).

---

## 4. Public interface

### 4.1 The orchestrator class

```python
class FnoOrchestrator:
    """Regime-aware strategy router. Selection only — never builds/prices legs.

    Composes: a RoutingPolicy (which strategy?) + the IndexParams registry +
    the existing vol-gate. Dispatches the chosen strategy to
    fno_strategies.run_strategy_backtest (the engine), one position per
    index per cycle.
    """

    def __init__(
        self,
        policy: "RoutingPolicy",
        *,
        index: IndexParams,
        k: float = DEFAULT_K,               # from ml.fno_vol_gate
        capital: float = 200_000.0,
        slip_pct: float = 0.005,
        allowed_strategies: frozenset[str] | None = None,  # default: the GO set (§7)
    ) -> None: ...

    # ---- pure selection (no DB, no engine) --------------------------------
    def decide(self, signals: RegimeSignals) -> RoutingDecision: ...
        """Apply the policy to ONE cycle's regime → a RoutingDecision.
        Deterministic, side-effect-free, unit-testable without a DB."""

    # ---- dispatch one chosen strategy to the engine -----------------------
    def dispatch(
        self, decision: RoutingDecision, cycle: dict[str, Any]
    ) -> dict[str, Any] | None: ...
        """If decision.stand_aside → return None. Otherwise call
        run_strategy_backtest(FNO_STRATEGIES[decision.strategy], [cycle],
        decision.params, k=self.k, capital=..., lot=index.lot,
        step=index.step, slip_pct=..., gate='none') and return its
        metrics dict for this single cycle.

        gate='none' is deliberate: the orchestrator has ALREADY made the
        regime decision in decide(); we must NOT let the engine's internal
        vol-gate veto a strategy we chose (that would double-gate and silently
        drop cycles). The engine still records the gate label per trade for
        analysis. This is the one subtlety of composing with the engine."""

    # ---- full backtest loop over a cycle list -----------------------------
    def run(self, cycles: list[dict[str, Any]]) -> "OrchestratorResult": ...
        """Per cycle: regime_from_cycle → decide → dispatch (or skip).
        Enforces one-position-per-index-per-cycle (§6). Aggregates the chosen
        per-cycle trades into a single portfolio-level result + per-strategy
        attribution. Pure except for nothing — cycles are passed in (the DB
        round-trip is the caller's, via cycles_from_db)."""
```

### 4.2 Free functions (pure, module-level)

```python
def regime_from_cycle(cycle: dict[str, Any], *, k: float = DEFAULT_K,
                      iv_rank: float | None = None,
                      trend: float | None = None) -> RegimeSignals: ...
    # Builds RegimeSignals from a raw cycle dict. Calls gate_decision +
    # core.fno_derived.implied_move. iv_rank/trend injected by the caller
    # (regime spec 01 computes these from a trailing window; the orchestrator
    # itself stays single-cycle-pure).

def cycles_for_index(index: IndexParams) -> list[dict[str, Any]]: ...
    # Thin convenience wrapper: cycles_from_db(symbol=index.symbol,
    # nifty_id=index.nifty_id, vix_id=index.vix_id, mode=index.expiry_mode).
    # The ONLY DB touch in this module; lazy-imports like cycles_from_db does.
```

### 4.3 Result container

```python
@dataclass
class OrchestratorResult:
    index: str
    n_cycles: int
    n_traded: int                       # cycles where a strategy was deployed
    n_stand_aside: int
    decisions: list[RoutingDecision]    # one per cycle (audit trail)
    trades: list[Any]                   # StrategyTrade objects, the chosen ones, chronological
    per_strategy: dict[str, dict]       # strategy name -> metrics dict (attribution)
    # Portfolio-level rollup (reuse fno_strategies' own helpers — do NOT re-derive):
    metrics: dict[str, Any]             # net_pnl, win_rate, sharpe, ROM, max_dd, go_no_go
```

`metrics` is computed by feeding the chosen `StrategyTrade` list through the **same** aggregation
math `run_strategy_backtest` uses. To avoid copy-pasting that block (and re-introducing its
honesty caveats), §8 specifies the reuse mechanism.

---

## 5. Composing with the routing policy (`02` plugs in here)

The orchestrator does not encode *which* strategy wins a given regime — that is policy, owned by
spec `02`. It defines a minimal protocol so `02` is swappable and independently testable:

```python
class RoutingPolicy(Protocol):
    def select(
        self, signals: RegimeSignals, allowed: frozenset[str]
    ) -> tuple[str | None, dict[str, Any] | None, str]: ...
        # returns (strategy_name_or_None, params_or_None, reason)
        # None strategy => stand aside.
```

- `allowed` is the GO-strategy whitelist (`§7`), so a policy can never route to a NO-GO
  undefined-risk strategy by construction (defence in depth, on top of the policy's own logic).
- A trivial **default policy** ships in this module for wiring/tests:
  `VrpDefaultPolicy` — if `gate_label == SELL_PREMIUM` route to `iron_condor` (the strongest GO),
  else stand aside. This is a placeholder so the architecture is runnable before `02` lands; the
  real multi-signal policy (VRP × IV-rank × trend × DTE) replaces it via the same protocol.

This is the composition seam: **orchestrator = machinery + discipline; policy (02) = the rules;
engine (fno_strategies) = construction/pricing.** Three files, three responsibilities.

---

## 6. One-position-per-index-per-cycle discipline

Enforced in `FnoOrchestrator.run()`, structurally — not by convention:

1. `run()` iterates cycles **for a single `IndexParams`**. Multi-index = run the orchestrator
   once per index (or a thin `MultiIndexOrchestrator` that holds one `FnoOrchestrator` per
   index and concatenates results — §9). Within one index loop there is exactly one
   `decide()` → one `dispatch()` per cycle.
2. `decide()` returns **exactly one** `RoutingDecision` per cycle (one strategy or stand-aside).
   The `RoutingPolicy.select` contract returns a single strategy name, never a list — so the
   type system makes "two strategies in one cycle" unrepresentable.
3. `dispatch()` runs `run_strategy_backtest` on a **single-element** `[cycle]` list, yielding at
   most one `StrategyTrade`. That trade (if any) is the cycle's one position.
4. Cycles are non-overlapping windows (`cycles_from_db` pairs consecutive boundaries), so
   "one position per cycle" == "no concurrent positions for that index". No position-state
   machine is needed in the backtest; the cycle boundary IS the position lifecycle.

Result: the invariant holds by data shape (single decision, single-cycle dispatch), not by a
runtime guard that could be bypassed.

---

## 7. The GO-strategy whitelist (defined-risk only)

Default `allowed_strategies` (overridable via constructor), sourced from `_CONTEXT.md` GO list,
intersected with `defined_risk=True` strategies in `FNO_STRATEGIES`:

```python
DEFAULT_ALLOWED = frozenset({
    "iron_condor",        # gated 3.91% GO
    "bull_put_spread",    # gated 7.19% GO
    "credit_put_spread",  # gated 2.70% GO
    "broken_wing_condor", # gated 2.67% GO  (flag: skew-blind under single-σ — see fno_strategies §1)
})
```

The orchestrator asserts at construction that every name in `allowed_strategies` exists in
`FNO_STRATEGIES` **and** has `defined_risk=True` (refuse undefined-risk: short straddle/strangle,
jade_lizard, ratio_spread — `_CONTEXT.md` NO-GO). This is the live-safety gate: even a buggy
policy cannot route to an unbounded-loss structure.

---

## 8. Reusing `fno_strategies` aggregation (no re-derivation)

The portfolio rollup must use the **same** Sharpe / drawdown / ROM / `go_no_go` math the engine
already owns, so the orchestrator inherits every honesty caveat for free. Two clean options;
**prefer (A)**:

**(A) Re-aggregate via the engine itself.** After collecting the chosen `StrategyTrade` list,
the orchestrator does NOT recompute metrics by hand. Instead it relies on the fact that each
`dispatch()` call already returned a full metrics dict for its single cycle, and the
portfolio-level rollup is obtained by a thin call path:

- Group chosen trades by `strategy` → per-strategy attribution is just the union of those
  per-cycle metrics dicts (the engine produced them).
- Portfolio-level `metrics`: call the engine's existing helpers — `_sharpe_from_pnls`,
  `_max_drawdown`, and `go_no_go` (all importable from `fno_strategies` / re-exported from
  `fno_condor`) — on the concatenated chronological pnl list. ROM = `Σnet_pnl / Σspan` exactly
  as the engine computes it. **Import these helpers; do not copy their bodies.**

**(B) (fallback) Single batched dispatch per strategy.** If per-cycle dispatch proves noisy,
group the *chosen* cycles by strategy and call `run_strategy_backtest(spec, chosen_cycles,
..., gate='none')` once per strategy, then merge. This still routes selection through the
orchestrator (only chosen cycles reach the engine) and reuses the engine's aggregation
verbatim. Trade-off: loses strict per-cycle decision interleaving in the trade order; (A)
preserves it.

Either way, **zero metrics math is reimplemented** in the orchestrator. The `go_no_go` verdict,
the undefined-risk disclaimer machinery, and the ROM caveat all originate in `fno_strategies`.

---

## 9. Data flow (end to end)

```
                          ┌─────────────────────────────────────────────┐
 caller (CLI / test)      │  INDEX_REGISTRY["NIFTY"] → IndexParams        │
        │                 └─────────────────────────────────────────────┘
        ▼
 cycles_for_index(index) ──► fno_condor.cycles_from_db(symbol, nifty_id,  [ONLY DB touch]
        │                       vix_id, mode=index.expiry_mode)
        ▼  list[cycle dict]
 ┌──────────────────────────  FnoOrchestrator.run(cycles)  ──────────────────────────┐
 │  for each cycle:                                                                    │
 │    1. regime_from_cycle(cycle, k)  ─► RegimeSignals                                 │
 │         └─ ml.fno_vol_gate.gate_decision   (regime, NOT re-derived)                 │
 │         └─ core.fno_derived.implied_move                                            │
 │    2. policy.select(signals, allowed)  ─► (strategy, params, reason)   [SPEC 02]    │
 │         └─ decide() wraps into RoutingDecision (stand_aside OR strategy)            │
 │    3. if stand_aside: record decision, continue   ◄── one-position discipline       │
 │       else: dispatch(decision, cycle)                                               │
 │            └─ run_strategy_backtest(FNO_STRATEGIES[strategy], [cycle], params,      │
 │                 k, capital, lot=index.lot, step=index.step, gate='none')  [ENGINE]  │
 │                  └─ spec.build(...)  ── leg CONSTRUCTION  (engine owns this)        │
 │                  └─ price_leg_hist / fill_price / resolve_legs / span_margin        │
 │            └─ collect the (≤1) StrategyTrade                                        │
 │  aggregate chosen trades via engine helpers (§8)  ─► OrchestratorResult            │
 └────────────────────────────────────────────────────────────────────────────────────┘
        │
        ▼
 OrchestratorResult { decisions[], trades[], per_strategy{}, metrics{ go_no_go } }
```

Arrows crossing into `[ENGINE]` are the only place legs exist. The orchestrator's surface area
is `RegimeSignals → RoutingDecision`. Everything index-specific entered at the top via
`IndexParams`.

---

## 10. CLI (optional, additive — mirrors `fno_strategies.main()`)

`python -m research.backtest.fno_orchestrator --index NIFTY --k 0.9 [--policy default]`

- Loads `INDEX_REGISTRY[args.index]`, hydrates cycles, runs the orchestrator, prints a table:
  `strategy | n_cycles | n_traded | n_aside | net_pnl | ROM | go?` plus the portfolio `go_no_go`
  reason and per-cycle decision log. Initializes the DB only after arg-parse (so `--help` never
  touches it), exactly like `fno_strategies.main()`.

---

## 11. Testing plan (`tests/test_fno_orchestrator.py`, pure — no DB)

1. `regime_from_cycle` maps a known cycle dict to the expected `RegimeSignals` (gate label,
   vrp, iv_ratio, implied_move) — assert it calls the real `gate_decision` (no re-derivation).
2. `decide()` determinism: same `RegimeSignals` → same `RoutingDecision`.
3. One-position discipline: `run()` over N cycles yields ≤ N trades and exactly one decision
   per cycle; stand-aside cycles produce zero trades.
4. Whitelist guard: constructing with an undefined-risk strategy in `allowed_strategies`
   raises; default `allowed` contains only `defined_risk=True` GO names.
5. `dispatch()` uses `gate='none'` (assert the chosen strategy is not silently dropped by the
   engine's internal gate when the regime says SELL but the engine's own gate disagrees).
6. Index-agnostic: a synthetic second `IndexParams` (different `lot`/`step`) flows through to
   `run_strategy_backtest` (assert `lot`/`step` are forwarded; strikes snap to the new `step`).
7. Aggregation parity: orchestrator portfolio `metrics` for a single-strategy route equals the
   engine's `run_strategy_backtest` metrics over the same chosen cycles (proves §8 reuse).

All seven run without a DB (cycles are hand-built dicts) — the module's only DB touch is the
lazy `cycles_for_index`, which tests bypass by passing cycles directly to `run()`.

---

## 12. What this spec deliberately does NOT do

- Does not define the routing *rules* (priority/scoring across VRP, IV-rank, trend, DTE) — that
  is spec `02`, plugged in via `RoutingPolicy`.
- Does not define how `iv_rank` / `trend` are computed — that is spec `01` (regime signals),
  injected into `regime_from_cycle`.
- Does not add intra-cycle stops or path-dependent exits (engine is expiry-only; tail-blindness
  caveat from `fno_strategies` is inherited unchanged).
- Does not touch live order paths, `ml/kronos_gate.py`, or any equity code. PAPER / research only.
- Does not edit `fno_strategies.py`, `fno_condor.py`, or `fno_vol_gate.py` — call-only.

---

## 13. Implementation checklist (ready to build)

- [ ] `IndexParams` + `INDEX_REGISTRY` (NIFTY populated; `lot`/`step`/`iv_source`/`expiry_mode`).
- [ ] `RegimeSignals`, `RoutingDecision`, `OrchestratorResult` dataclasses.
- [ ] `RoutingPolicy` Protocol + `VrpDefaultPolicy` placeholder.
- [ ] `regime_from_cycle()` (calls `gate_decision` + `implied_move`).
- [ ] `cycles_for_index()` (lazy DB, wraps `cycles_from_db`).
- [ ] `FnoOrchestrator`: `__init__` (whitelist assert), `decide`, `dispatch` (`gate='none'`),
      `run` (one-position discipline + §8 aggregation via imported engine helpers).
- [ ] Optional `main()` CLI mirroring `fno_strategies.main()`.
- [ ] `tests/test_fno_orchestrator.py` (7 cases, no DB).
- [ ] `ruff` clean; `pytest -q` green; no edits to existing F&O modules.
```
