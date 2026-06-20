# 10 — Build Plan: synthesis + ordered PR sequence for the F&O Orchestration Engine

> Synthesis spec. Reads `_CONTEXT.md` and the nine facet-specs and assembles them into a
> dependency-ordered set of PRs with tests. Defines what is **buildable + backtestable NOW**
> (NIFTY, existing data) vs **blocked-on-data** (other indices) vs **blocked-on-live** (forward
> paper, eventual live), and specifies the **MVP first PR** that ships a working NIFTY orchestrator
> + backtest.
>
> This spec plans only. No code here. It is the contract the facet-specs assemble against.

---

## 0. Facet-spec map (the nine inputs)

| # | Spec file | Owns | Build artifact (module) |
|---|-----------|------|--------------------------|
| 01 | `01_regime_signals.md` | Regime feature vector: vol-gate state, VRP, IV rank/level, trend, DTE | `regime.py` (pure features) |
| 02 | `02_routing_policy.md` | Selection layer: regime → {GO strategy \| stand-aside} | `router.py` (pure policy) |
| 03 | `03_architecture.md` | Engine shape, dataclasses, cycle loop, how router + fno engine compose | `orchestrator.py` (glue) |
| 04 | `04_index_registry.md` | Per-index lot / step / expiry-calendar / data-source registry | `index_registry.py` |
| 05 | `05_risk_capital.md` | ROM headline, SPAN-margin budget, capital allocation, defined-risk guard | `risk_alloc.py` (pure) |
| 06 | `06_backtest_harness.md` | Multi-cycle / multi-strategy driver, ROM report, no-look-ahead contract | `orchestrator_backtest.py` + report |
| 07 | `07_data_ingestion.md` | Dhan API per-index ingestion; which indices have history vs forward-only | ingestion scripts (out of MVP scope) |
| 08 | `08_forward_paper.md` | Real-IV forward paper-log harness (the truth test) | forward-paper logger (blocked-on-live) |
| 09 | `09_validation.md` | Acceptance gates, honesty ledger, GO/NO-GO criteria, OOS split | validation checklist + report fields |

All nine sit **ON TOP** of the existing, frozen fno layer — they call it, never edit it:

- `research/backtest/fno_strategies.py` — `run_strategy_backtest(spec, cycles, …)`, the `build_*`
  constructors (`build_iron_condor`, `build_bull_put_spread`, `build_credit_put_spread`,
  `build_broken_wing_condor`), `StrategySpec`/`Leg`/`StrategyTrade`, `cycles_from_db(mode=…)`.
- `ml/fno_vol_gate.py` — `gate_decision(...)`, `compute_vrp_stats(...)`, `calibrate_threshold(...)`,
  `samples_from_db(...)`.
- `research/backtest/fno_costs.py` — Indian intraday cost stack (delegated, not re-implemented).
- Data: `index_bars`, `option_atm_iv` (and chain tables) in **`dhan_trading` (cfg.db_url)**, NIFTY
  (id 13) + India VIX (id 21) only. Schema = Alembic 009/010/011.

**Lane rule (from `_CONTEXT.md`):** fno owns `fno_strategies.py` + data foundation + real-IV
validation. We own the orchestration layer + index-agnostic generalization. Call fno; do not edit
its internals.

---

## 1. Now vs Blocked matrix

| Capability | Status | Why | Gating dependency |
|---|---|---|---|
| Regime feature vector on NIFTY (`regime.py`) | **NOW** | Derives from existing `index_bars`/`option_atm_iv`/VIX + vol-gate | spec 01, existing data |
| Routing policy (`router.py`) — pure regime→strategy map | **NOW** | Pure function over the regime vector; no new data | specs 01, 02 |
| Index registry (`index_registry.py`) | **NOW** (NIFTY populated; others stubbed) | Static config; agnostic shape buildable today, only NIFTY row is live | spec 04 |
| Risk/capital + ROM allocation (`risk_alloc.py`) | **NOW** | Reuses `span_margin` from fno; pure math | spec 05 |
| Orchestrator glue (`orchestrator.py`) | **NOW** | Composes router + `run_strategy_backtest` over NIFTY cycles | specs 02, 03 |
| Orchestrated backtest + ROM report on NIFTY | **NOW** | `cycles_from_db(mode=…)` gives NIFTY weekly cycles today | spec 06 |
| Validation gates / honesty ledger / OOS split | **NOW** (criteria); evidence accrues over PRs | Date-split + ROM headline computable on NIFTY now | spec 09 |
| Multi-index backtest (BANKNIFTY/FINNIFTY/…) | **BLOCKED-ON-DATA** | No `index_bars`/chains for those ids | spec 07 ingestion; historical chains likely **unavailable** → those indices **forward-only** |
| Continuous-futures / true-IV regime inputs | **BLOCKED-ON-DATA** | VIX-as-weekly-IV is a proxy; real IV needs ingestion | spec 07; F&O Open Q#2 (continuous futures) |
| Forward paper-log (real-IV truth test) | **BLOCKED-ON-LIVE** | Needs live option-chain capture on the trusted machine (Dhan EIP) | spec 08; PAPER only, runs on agent |
| Live order routing | **BLOCKED-ON-LIVE** (out of scope) | Post-M3 decision; defined-risk only; not in this engine yet | platform M7/M8, separate effort |

**One-line rule:** the **selection layer + NIFTY backtest is fully buildable now**; everything that
needs *another index* is blocked-on-data; everything that needs *real (not proxy) IV truth* is
blocked-on-live (forward paper).

---

## 2. Dependency graph

```
                 frozen fno layer (fno_strategies.py, fno_vol_gate.py, fno_costs.py, data)
                              │ (call only)
        ┌─────────────────────┼──────────────────────────────┐
   index_registry (04)   regime (01)                     risk_alloc (05)
        │                     │                                │
        └──────────►  router (02)  ◄───────────────────────────┘
                              │
                       orchestrator (03)   ← composes router + run_strategy_backtest
                              │
                  orchestrator_backtest + ROM report (06)
                              │
                       validation gates (09)
                              │
        ┌─────────────────────┴──────────────────────┐
   data ingestion (07)                          forward paper (08)
   → unlocks multi-index backtest               → real-IV truth test (blocked-on-live)
   (blocked-on-data)
```

Build order falls straight out of the graph: **leaves first** (registry, regime, risk) → router →
orchestrator → backtest/report → validation → (parallel, later) ingestion & forward-paper.

---

## 3. Ordered PR plan

Each PR: one focused module + its tests, pure/deterministic where possible, `pytest -q` + `ruff`
green, branch + PR (never to main), agent merges on green CI outside market hours. Mirror the
platform test conventions (`tests/test_fno_*.py`).

### Phase A — NIFTY orchestrator MVP (buildable NOW)

- **PR-1 — MVP: NIFTY orchestrator + backtest (the first PR; see §5).** Minimal end-to-end:
  thin regime read + minimal router + orchestrator glue + NIFTY orchestrated backtest with ROM
  headline. Ships a runnable `python -m research.backtest.orchestrator_backtest`. Specs 01/02/03/06
  in their thinnest viable form. *This is the slice that proves the whole stack works.*

- **PR-2 — Index registry (`index_registry.py`), NIFTY-only row.** Full spec 04: per-index lot/
  step/expiry-calendar/data-source. NIFTY populated; other indices present but flagged
  `data_available=False`. PR-1's hardcoded NIFTY constants (lot 75/step 50) refactored to read the
  registry. Tests: registry lookups, NIFTY values match fno constants, unknown index raises.

- **PR-3 — Full regime feature vector (`regime.py`).** Promote PR-1's thin regime to the complete
  spec 01 vector: vol-gate state, VRP (VIX vs realized), IV rank/level, trend, DTE-bucket. Pure
  function `regime_features(cycle, history) -> RegimeVector`. **No look-ahead**: features use only
  data ≤ cycle entry. Tests assert each feature + a look-ahead guard (future bars never read).

- **PR-4 — Routing policy (`router.py`), full.** Spec 02: regime vector → one of the four GO
  strategies or STAND_ASIDE, honoring the vol-gate (only SELL_PREMIUM side traded). Defined-risk
  only — NO-GO families (short straddle/strangle, jade_lizard, ratio, debit/long) are
  unselectable. Tests: each regime bucket maps to expected strategy; NO-GO families never emitted;
  determinism.

- **PR-5 — Risk/capital + ROM allocation (`risk_alloc.py`).** Spec 05: SPAN-margin budget per
  cycle (reuse `span_margin`), capital cap, defined-risk guard, ROM as headline. Tests: ROM math,
  margin budget never exceeded, defined-risk invariant.

- **PR-6 — Backtest harness + ROM report (`orchestrator_backtest.py` + report).** Spec 06:
  multi-cycle driver wiring registry+regime+router+risk over `cycles_from_db`, ungated-vs-gated
  A/B, date-split IS/OOS (never random on time series), ROM headline + per-strategy attribution.
  Report mirrors `fno_condor_report.md` shape. Tests: A/B both run, OOS split chronological, ROM
  reproduces single-strategy numbers when router is pinned to one strategy.

- **PR-7 — Validation gates + honesty ledger (`09`).** Spec 09: codify GO/NO-GO acceptance
  (ROM threshold, OOS Sharpe not collapsing, defined-risk only), carry forward the honesty ledger
  (single-IV/no-smile, close-not-FSP, VIX-as-weekly-IV proxy, expiry-only tail-blindness). Output
  a machine-checkable verdict block in the report. Tests: a known-good and known-bad metrics dict
  produce GO / NO-GO respectively.

### Phase B — Multi-index generalization (BLOCKED-ON-DATA)

- **PR-8 — Data ingestion plan + scripts (`07`).** Dhan API per-index ingestion (index_bars +
  option chains where available). Per index: mark historical-available vs forward-only. Lands as
  ingestion CLI + Alembic if new tables needed (extend 009-style). Backtest on a new index is
  unlocked **only after its data lands** on the trusted machine (Dhan EIP). NIFTY engine unchanged.

- **PR-9 — Multi-index backtest activation.** Flip `data_available=True` per ingested index in the
  registry; run the PR-6 harness per index. Forward-only indices skip the backtest, go straight to
  forward-paper. No engine change — just config + data.

### Phase C — Forward paper truth test (BLOCKED-ON-LIVE)

- **PR-10 — Forward paper-log harness (`08`).** Real-IV (not VIX-proxy) live capture + paper
  decisions logged per cycle, PAPER only, on the agent. This is the truth test that re-validates
  the preliminary backtest edge against real IV + FSP settlement. No live order paths.

- **(Future, out of this plan) — live decision.** Defined-risk only, post forward-paper validation,
  gated by the platform's live-trading rules (M7/M8). Not built here.

---

## 4. Test strategy (mirror the platform)

- **Framework:** `pytest -q`, files `tests/test_orchestrator_*.py` alongside the existing
  `tests/test_fno_*.py`. CI (Py3.12, x86+ARM, coverage, ruff) gates every PR.
- **Purity:** every new module except the backtest driver is pure + deterministic (no DB, no
  network), exactly like `fno_strategies.py`. DB access isolated to the driver's thin loader, which
  re-exports `cycles_from_db` rather than re-querying.
- **No look-ahead (load-bearing):** the regime vector and router must consume only data at-or-before
  cycle entry. Each feature gets an explicit look-ahead guard test (inject a future bar; assert the
  feature is unchanged). The OOS split is **chronological** (first 70% IS / last 30% OOS) — never
  random on a time series.
- **ROM headline:** ROM (return-on-SPAN-margin) is the headline metric asserted in tests and the
  report, not return-on-capital. Tests check ROM math + that the margin budget is never exceeded.
- **Defined-risk invariant:** a property test asserts the router can never emit an undefined-risk /
  NO-GO family, across all regime inputs.
- **Determinism:** same cycles + same seed ⇒ identical metrics dict (reproduce the fno engine's
  determinism guarantee).
- **A/B parity:** when the router is pinned to a single GO strategy with gate="vol", the
  orchestrated backtest must reproduce that strategy's standalone `run_strategy_backtest` numbers —
  proves the orchestration layer adds no accidental edge/leakage.
- **Honesty ledger carried forward:** every GO verdict in the report repeats the proxy caveats
  (single-IV/no-smile, close-not-FSP, VIX-as-weekly-IV, expiry-only).

---

## 5. The MVP first PR (PR-1) — working NIFTY orchestrator + backtest

**Goal:** smallest slice that runs end-to-end on existing NIFTY data and prints a ROM headline,
proving the selection layer composes over the frozen fno engine. Everything else is refinement.

**Scope (thin versions of specs 01/02/03/06):**

1. `research/backtest/orchestrator.py`
   - `RegimeVector` dataclass (thin): just the **vol-gate decision** + **DTE** for MVP
     (full feature vector deferred to PR-3).
   - `regime_for_cycle(cycle) -> RegimeVector` — derives the gate decision via
     `ml.fno_vol_gate.gate_decision` on the cycle's existing fields (VIX/100 IV proxy +
     persistence realized vol). Pure; no new data.
   - `route(regime) -> str | None` — minimal policy: if gate == SELL_PREMIUM → `"iron_condor"`
     (the strongest gated GO, 3.91% ROM); else stand aside (`None`). Hardcoded single mapping;
     the four-way policy lands in PR-4.
   - `run_orchestrated_backtest(cycles, …) -> dict` — loops cycles, calls `route`, and for the
     chosen strategy calls `build_iron_condor(...)` + `run_strategy_backtest(spec, [cycle], …)`,
     aggregates net P&L and Σ SPAN → **ROM headline**, IS/OOS chronological Sharpe.

2. `research/backtest/orchestrator_backtest.py` (CLI)
   - `python -m research.backtest.orchestrator_backtest --mode <fast|full>` → loads NIFTY cycles
     via `cycles_from_db(mode=…)`, runs `run_orchestrated_backtest`, prints the ROM report
     (mirrors `fno_condor_report.md`): n_cycles, n_traded, net P&L, mean SPAN, **ROM**,
     sharpe_is/oos, plus the honesty-ledger caveat block.

3. `tests/test_orchestrator_mvp.py`
   - Router: SELL_PREMIUM regime → "iron_condor"; non-SELL → None.
   - Determinism: same synthetic cycles ⇒ identical metrics dict.
   - No look-ahead: regime derived only from cycle-entry fields (no future bar read).
   - A/B parity: orchestrator pinned to iron_condor reproduces standalone
     `run_strategy_backtest(build_iron_condor(...), cycles)` ROM (sanity that glue adds no edge).
   - ROM headline present and equals net_pnl / Σ span.

**Hardcoded-now, refactored-later (explicit debt, each has a follow-up PR):**
- NIFTY lot/step constants inline → PR-2 registry.
- Thin 2-field regime → PR-3 full vector.
- Single-strategy mapping → PR-4 four-way router.
- Margin/capital from fno's `span_margin` directly → PR-5 allocator.

**Out of scope for PR-1:** multi-index, ingestion, forward paper, live, the full four-strategy
router. Defined-risk only throughout; NO-GO families never reachable. PAPER posture only — no order
paths, no live infra touched.

**Definition of done:** `pytest -q` + `ruff` green; `python -m research.backtest.orchestrator_backtest`
prints a NIFTY ROM headline on existing data; report carries the honesty ledger; branch +
PR, agent merges on green CI outside 09:15–15:30 IST.

---

## 6. Sequencing summary

```
PR-1 (MVP NIFTY orchestrator+backtest)   ← ships first, proves the stack
  ├─ PR-2 index_registry (NIFTY row)
  ├─ PR-3 regime full vector
  ├─ PR-4 router full (4 strategies)
  ├─ PR-5 risk_alloc / ROM budget
  ├─ PR-6 backtest harness + report (A/B, OOS)
  └─ PR-7 validation gates + honesty ledger
        │  ── Phase A complete: full NIFTY orchestrator, backtested, validated ──
        ├─ PR-8 data ingestion (per index)         [BLOCKED-ON-DATA]
        ├─ PR-9 multi-index backtest activation     [BLOCKED-ON-DATA]
        └─ PR-10 forward paper-log (real-IV truth)  [BLOCKED-ON-LIVE]
              └─ (future) live decision, defined-risk only  [out of this plan]
```

PRs 2–7 are largely **parallelizable** after PR-1 lands (small focused agent tasks, one module
each) and re-converge at PR-6/PR-7. Phase B/C wait on data and live respectively and do not block
the Phase-A NIFTY deliverable.
