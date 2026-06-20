# 08 — Forward Paper-Trade + Staged (User-Gated) Live Path

**Status:** RESEARCH / PAPER ONLY. No live order paths in this spec.
**Scope:** how the F&O *orchestrator* runs in forward-paper mode off the daily collector's
**real** option chain, what it logs, the PAPER discipline it must obey, and the staged,
user-gated path that *could* eventually lead to a small defined-risk live deployment.

> **Why this spec exists.** The orchestrator's edge is **PRELIMINARY** (see `_CONTEXT.md` §HARD
> REALITIES): it rests on (a) VIX-as-weekly-IV proxy, (b) close-not-FSP settlement, (c)
> expiry-only resolution. Those make the historical "GO" numbers *directional evidence, not
> truth*. The **real-IV forward paper-log is the truth test.** Nothing in the live discussion
> happens until the forward log accumulates enough real-chain, real-resolution cycles to confirm
> (or refute) the backtest. This spec is the bridge between "backtest says GO" and "we have a
> live-worthy track record."

---

## 0. Where this sits

```
core/fno_collector.py  (daily EOD cron, trusted machine)
   1. append index + VIX bars
   2. refresh expiry calendar
   3. snapshot REAL option chain  → option_chain_snapshot   ← the only real-IV source
   4. recompute realized vol
   5. paper-log step  ──────────────────────────────────────┐
                                                             ▼
        core/fno_paper.py  (TODAY: single-strategy iron-condor)
          record_paper_entry()   — one gate-filtered condor per weekly cycle
          resolve_paper_trades()  — resolve matured OPEN rows at expiry
          paper_summary()
                                                             │
        ── THIS SPEC EXTENDS THE PATTERN ──────────────────▶│
                                                             ▼
        core/fno_orchestrator_paper.py  (NEW: multi-strategy, orchestrated selection)
          record_orchestrated_entry()  — orchestrator PICKS one GO strategy, logs the chosen one
          resolve_orchestrated_trades() — resolve at expiry per chosen strategy's leg set
          orchestrator_paper_summary() — per-strategy + blended ROM/win-rate
```

The orchestrator paper module is a **superset** of `fno_paper.py`'s proven shape, not a rewrite:
same EOD cadence, same "one position per (symbol, expiry) cycle" idempotency, same resolve-at-expiry
discipline, same fail-open wrapping in the collector. The only new thing is the **selection layer**
in front of the entry, and a **strategy column** in the log.

Lane note (`_CONTEXT.md` §Lanes): we **call** `research/backtest/fno_strategies.py` builders and
`ml/fno_vol_gate.py`; we do not edit their internals. The orchestrator router itself is the other
specs in this dir (regime → strategy selection); this spec is purely the **forward-paper harness +
logging + the gated live ladder** that wraps that router.

---

## 1. Forward-paper mode — how the orchestrator runs off the real chain

### 1.1 Cadence and trigger
Identical to today's paper-log: invoked at the **end of `core/fno_collector.main()`**, after the
real chain snapshot has landed in `option_chain_snapshot` and realized vol is recomputed. Order is
**resolve-then-enter** (resolve matured cycles first, then open the new nearest-weekly cycle), and
the whole step is wrapped in a `try/except` that logs but never breaks the collection (exactly as
lines 226–233 of `fno_collector.py` do today).

```python
# fno_collector.main(), replacing/extending the existing paper-log block:
try:
    from core.fno_orchestrator_paper import (
        record_orchestrated_entry, resolve_orchestrated_trades,
    )
    resolved = resolve_orchestrated_trades(symbol=args.symbol)
    entry    = record_orchestrated_entry(symbol=args.symbol)
    logger.info("orchestrator paper-log: resolved=%d entry=%s", resolved, entry)
except Exception:  # noqa: BLE001
    logger.exception("orchestrator paper-log step failed (collection data still written)")
```

The single-strategy `fno_paper.py` log may run **in parallel** during a burn-in window (its condor
is one of the GO candidates), giving a built-in A/B: "always-condor" vs "orchestrator-selected".
Decommission the standalone condor log only once the orchestrated log is trusted.

### 1.2 The selection step (what's new vs `fno_paper.py`)
`record_orchestrated_entry()` reuses `record_paper_entry()`'s real-chain plumbing verbatim —
find nearest future expiry → latest `snapshot_time` → load all `(strike, option_type, ltp, iv, spot)`
rows → build `ce`/`pe` dicts → derive ATM straddle IV → fetch `realized_vol_20d` from `index_bars`.
**Up to that point it is the existing code path.** Then, instead of hard-coding `build_condor`, it:

1. Builds the **regime feature vector** from the same real-chain inputs already in hand:
   `straddle_iv`, `realized_vol_20d`, the VRP ratio, IV level/rank (over the trailing window of
   prior snapshots), DTE, and any trend signal the router defines. **No new data fetch** — every
   input is already loaded for the gate decision.
2. Calls the **orchestrator router** (the selection layer specified in the sibling specs) →
   returns one of: a chosen GO strategy name **or** `STAND_ASIDE`.
   - GO universe is **defined-risk only** (`_CONTEXT.md`): `iron_condor`, `bull_put_spread`,
     `credit_put_spread`, `broken_wing_condor`. Undefined-risk strategies are **never** selectable
     in the paper log, so the forward track is structurally consistent with the eventual live
     constraint.
3. If `STAND_ASIDE` (or the vol-gate is not `SELL_PREMIUM`), it records **nothing** and returns a
   reason (the cycle is simply skipped — a stand-aside is itself a logged decision; see §2.3).
4. If a strategy is chosen, it calls the matching `research/backtest/fno_strategies.py` builder
   (`build_iron_condor`, `build_bull_put_spread`, `build_credit_put_spread`,
   `build_broken_wing_condor`) to get the leg set, then **prices every leg off the REAL chain**
   (`ltp` nearest available strike, same `_nearest_pe`/`_nearest_ce` logic, with the same
   bail-out: any missing/zero leg premium → no entry, to avoid inflated credit on incomplete
   quotes).
5. Computes per-unit `credit`, defined-risk `max_loss` (₹/lot), and `entry_costs` via
   `fno_costs.condor_costs` / the strategy's cost model, then **inserts one row** with
   `ON CONFLICT DO NOTHING` on `(symbol, expiry_date)` — exactly one orchestrated position per
   weekly cycle.

### 1.3 Index-agnostic from day one (forward-only for non-NIFTY)
NIFTY (scrip 13) is the **only** index with a real chain today (`_CONTEXT.md` §HARD REALITIES #1).
The paper harness takes `symbol` + an index registry (lot, strike step, expiry calendar, scrip id)
so the **same code** logs other indices the moment their chain ingestion lands — those indices are
**forward-only** (no historical backtest exists for them), so the forward paper-log *is* their only
evidence. Until ingested, only NIFTY produces rows.

---

## 2. What gets logged

### 2.1 Reuse the `fno_paper_trades` table, add a strategy discriminator
The existing table (Alembic **011**) already captures everything a *condor* needs. The orchestrator
needs only to record **which strategy was chosen** and to generalize the four named leg columns to
an arbitrary defined-risk leg set. Two non-destructive options (pick in the implementing spec/PR):

- **Preferred (additive migration 012):** add `strategy TEXT` (chosen GO strategy name) and
  `legs JSONB` (the full chosen leg set: each leg's `option_type`, `side`, `strike`, real entry
  premium, qty) to `fno_paper_trades`. Keep the four condor strike/prem columns nullable for
  back-compat; populate them when the chosen strategy *is* a condor. The `raw` JSONB already exists
  for the full entry context — but a typed `strategy` column is what every summary query will group
  by, so it earns its own column.
- The `gate_decision`, `k`, `straddle_iv`, `realized_vol_20d`, `credit`, `max_loss`, `entry_costs`,
  `status`, `expiry_spot`, `gross_pnl`, `exit_costs`, `net_pnl`, `win`, `resolved_at` columns are
  reused unchanged.

Follow the project memory **"capture full payload, use subset"**: store the **entire** chosen leg
set + the full regime feature vector that drove the selection in `raw`/`legs` JSONB, even though
summaries only project a few fields. This makes the selection auditable after the fact ("why did the
router pick a bull put spread on 2026-07-03?").

### 2.2 Per-trade record (one row per chosen position)
| Field | Meaning |
|---|---|
| `symbol`, `entry_date`, `expiry_date`, `lot` | cycle identity (UNIQUE on symbol+expiry) |
| **`strategy`** | the GO strategy the orchestrator selected (NEW) |
| `gate_decision`, `k` | vol-gate verdict + threshold used (always `SELL_PREMIUM` for a position) |
| `spot_entry`, `straddle_iv`, `realized_vol_20d` | real-chain entry context |
| `raw.regime` (JSONB) | full feature vector the router saw (VRP ratio, IV rank, DTE, trend…) — NEW |
| **`legs` (JSONB)** | full chosen leg set with **real** entry premiums per leg (NEW) |
| `short_*_k/_prem`, `long_*_k/_prem` | populated when the chosen strategy is a condor (back-compat) |
| `credit`, `max_loss`, `entry_costs` | per-unit credit; **defined-risk** ₹/lot max loss; entry costs |
| `status` | `OPEN` → `RESOLVED` |
| `expiry_spot`, `gross_pnl`, `exit_costs`, `net_pnl`, `win`, `resolved_at` | resolution outputs |

### 2.3 Stand-aside is also a decision worth logging
A `STAND_ASIDE` / non-`SELL_PREMIUM` cycle currently returns `{"recorded": False, ...}` and leaves
no DB trace. For honest forward evaluation we want to know **how often the router stands aside and
what the regime looked like when it did** (a router that never trades has no edge to measure).
Record stand-asides as lightweight rows (e.g. `status='SKIPPED'`, `strategy=NULL`, the regime vector
in `raw`, no legs/credit) — same `ON CONFLICT` per cycle so a skip is logged once. Summaries exclude
`SKIPPED` from P&L but count it for participation rate.

### 2.4 Resolution — ROM is the headline
`resolve_orchestrated_trades()` mirrors `resolve_paper_trades()`:
- settlement proxy = `index_bars` close on `expiry_date` (the known **close-not-FSP** approximation,
  flagged here as a source of error to revisit when FSP ingestion exists);
- gross P&L from the chosen strategy's resolver (`resolve_legs` / `resolve_condor`) per lot;
- exit costs = exercise STT on ITM long legs (cash-settled, no closing brokerage), same model as
  today;
- `net_pnl = gross_pnl − entry_costs − exit_costs`; `win = net_pnl > 0`;
- guarded `UPDATE ... WHERE status='OPEN'` to prevent double-resolve.

**ROM (return-on-SPAN-margin)** is the headline metric per `_CONTEXT.md` §3, not return-on-capital.
For each resolved row compute `ROM = net_pnl / span_margin` using
`fno_strategies.span_margin(...)` (the defined-risk SPAN path) on the chosen leg set. Store ROM in
the row (add `rom DOUBLE` in migration 012) so the summary doesn't recompute it. The summary then
reports, **per strategy and blended**: n_resolved, win_rate, total/mean `net_pnl`, mean `ROM`,
participation rate (positions ÷ cycles), and OPEN exposure (Σ `max_loss`).

### 2.5 Dashboard surfacing (read-only)
Expose `orchestrator_paper_summary()` via a read-only `/api/*` endpoint on `dhan-api` (same pattern
as the existing read-only handlers) so the forward track is visible without DB shells. Read-only:
the dashboard never enters or resolves trades — the EOD cron is the only writer.

---

## 3. PAPER discipline — the non-negotiables

These are the rules the forward-paper harness must obey, and the reasons live talk is gated.

1. **No live order paths. At all.** The forward-paper module imports **nothing** from
   `engine/execution.py` and constructs **no** `OrderIntent`. It reads `option_chain_snapshot` /
   `index_bars` and writes `fno_paper_trades`. There is no broker round-trip in either entry or
   resolution. (`PAPER_TRADING=true` is the platform default and stays true — Safety rule #1.)
2. **One Dhan session per account.** The collector already runs on the trusted machine using the
   cached token (`read_current_token()`); the paper harness adds **zero** new Dhan calls — it reuses
   the chain the collector already fetched. It must never open a second `DhanClient` or contend for
   the live session that `dhan-trader` owns.
3. **Never mint.** No synthetic/interpolated/model-priced fills in the forward log. Every leg
   premium is a **real LTP** from the snapshot; if a leg has no real quote (missing or ≤ 0), the
   cycle is **not entered** (the existing bail-out). We do not invent a price to force a trade. This
   is what makes the log a *truth test* rather than a dressed-up backtest.
4. **One position per (symbol, expiry) cycle**, enforced by the UNIQUE constraint + `ON CONFLICT
   DO NOTHING` — re-running the collector is idempotent and can never double-enter.
5. **Defined-risk only**, even in paper — the GO universe excludes undefined-risk strategies, so the
   forward track can never contain a position the eventual live constraint would forbid.
6. **Resolve-at-expiry only** (current proxy). Known limitations are documented in the log itself
   (close-not-FSP `expiry_spot`, exercise-STT approximation) so reviewers weigh the track honestly.
   Intra-cycle MTM / early-management is out of scope for the truth-test phase (it would add modeling
   assumptions on top of the very assumptions we are trying to validate).
7. **No commits to `main` / no infra changes from this lane.** Branch + PR; the trusted machine
   merges and deploys (`_CONTEXT.md` collaboration lanes, memory "always-branch-for-changes").

---

## 4. The truth test — exit criteria from forward-paper

Live is **not** a time-based graduation; it is **evidence-based**, mirroring the Kronos gate's
re-arm discipline (`CLAUDE.md` Kronos section: "n≥30 fresh, acc≥55% → then flip"). Proposed
forward-paper exit criteria, **per strategy actually traded** (and blended across the orchestrator):

- **Sample:** ≥ N real-IV, real-resolution cycles per selected strategy (weekly NIFTY ⇒ N≈25–30 is
  one to two quarters — set the exact N in the go/no-go PR; do not pre-commit to a number that the
  data can't support).
- **Consistency with backtest:** forward mean **ROM** within a sane band of the historical GO ROM
  (e.g. forward ROM ≥ ~60% of backtested ROM and **> 0** net of costs) — a large forward shortfall
  means the VIX-proxy / close-not-FSP assumptions were doing the work, i.e. the edge was an artifact.
- **Win-rate and tails:** forward win-rate ≥ the backtest win-rate minus a tolerance, **and** no
  defined-risk max-loss event larger than modeled (defined-risk caps loss, but the *frequency* of
  hitting it must match expectations).
- **Router adds value:** orchestrator-selected ROM ≥ always-condor ROM over the same window (the
  selection layer is the novelty — it must beat the naive single-strategy baseline, else just run
  the condor).
- **Stand-aside sanity:** participation rate is neither ~0% (no edge to harvest) nor 100% (gate not
  discriminating).

Failing these is a **NO-GO** and the honest, expected outcome to be prepared for — the forward log
exists precisely to catch a preliminary edge that doesn't survive reality.

---

## 5. Staged, USER-GATED path to (eventual) small live

Live is **out of scope to build** here. This is the ladder; **every rung is a hard stop requiring
explicit user approval** (memory "infra-confirm-before-executing"). No rung auto-advances.

**Stage 0 — Forward paper (THIS spec).** Orchestrator runs in `core/fno_collector` EOD, real chain,
no order paths. Accumulate the truth-test sample. *Output: the forward track + summary.*

**Stage 1 — Go/No-Go review (user-gated).** Once §4 criteria are met, present a written go/no-go: per
strategy and blended ROM/win-rate, forward-vs-backtest gap, router-vs-baseline, the still-standing
caveats (FSP, intra-cycle risk). **User decides** whether the edge is real enough to discuss live.
NO-GO ⇒ iterate the router / proxy, stay in paper.

**Stage 2 — FSP + intra-cycle fidelity (still paper).** Before risking ₹1, retire the two biggest
approximations: resolve against the **NSE Final Settlement Price** (not the index close) once FSP is
ingested, and add intra-cycle MTM so we know the **path**, not just the endpoint (max adverse
excursion within a defined-risk position, even though loss is capped). Re-confirm §4 on the
higher-fidelity log. **User-gated.**

**Stage 3 — Live readiness design (paper + infra design only).** Specify the live execution path for
defined-risk multi-leg options: IP-whitelisted order placement (live order placement is locked to the
agent EIP — `CLAUDE.md`), atomic multi-leg entry/abort (never end up with a naked short because one
long leg failed), the RiskEngine integration (it owns the kill-switch — Safety rule #2), and a
hard per-position defined-risk cap. **No live orders yet — design + tests only. User-gated.**

**Stage 4 — Tiny live, defined-risk only, user-flipped.** Smallest possible size (1 lot), **defined
risk only** (never an undefined-risk leg set, regardless of any future backtest), `PAPER_TRADING`
flipped to `false` **only** via `.env` edit + `ALLOW_LIVE_TOGGLE=true` + restart, **only by the
user** (Safety rule #1; never flip without explicit user intent). Live runs **alongside** the
continuing paper log so paper-vs-live slippage is measured directly. The kill switch
(`run/killswitch`) and RiskEngine daily-loss halt apply unchanged.

**Stage 5 — Scale (user-gated, evidence-gated).** Only after a clean tiny-live track does size or
index breadth increase — and only on the user's explicit say-so, one step at a time.

> **Standing constraints across every stage** (`CLAUDE.md` safety rules): defined-risk only for
> live; RiskEngine owns the kill-switch; Dhan has no sandbox (every non-paper call is production);
> one Dhan session per account; no secrets in the repo; branch+PR, trusted-machine deploys.

---

## 6. Build checklist (for the implementing PR — not this spec)
- [ ] `core/fno_orchestrator_paper.py` — `record_orchestrated_entry`, `resolve_orchestrated_trades`,
      `orchestrator_paper_summary` (extend `fno_paper.py` pattern; call the router + `fno_strategies`
      builders; never edit `fno_strategies.py`).
- [ ] Alembic **012** — additive: `strategy TEXT`, `legs JSONB`, `rom DOUBLE` on `fno_paper_trades`;
      `SKIPPED` status allowed; condor columns made nullable. No destructive change.
- [ ] Wire into `core/fno_collector.main()` (resolve-then-enter, fail-open), optionally alongside the
      existing condor log during burn-in.
- [ ] Read-only `/api/*` summary endpoint on `dhan-api`.
- [ ] Tests: pure selection→leg-pricing→credit/max_loss/ROM math DB-free (lazy DB imports like
      `fno_paper.py`); resolution + idempotency with a fake `now`; stand-aside logging.
- [ ] `pytest -q` + `ruff` green; branch + PR (agent merges per the lane rules, outside market hours).
