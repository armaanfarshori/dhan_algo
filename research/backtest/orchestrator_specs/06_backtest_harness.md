# 06 — Orchestrator Backtest Harness

**Status:** spec (implementation-ready) · **Scope:** historical weekly NIFTY · **Mode:** PAPER / research only
**Reads:** `_CONTEXT.md`, `research/backtest/fno_strategies.py`, `research/backtest/fno_condor.py`, `ml/fno_vol_gate.py`
**Builds:** `research/backtest/orchestrator.py` (new module; does NOT edit `fno_strategies.py` internals — it *calls* them)

---

## 0. What this harness answers

The orchestration engine (specs 01–05) is a **regime-aware router**: per cycle it PICKS one GO
strategy to deploy — or stands aside — based on the regime (vol-gate state, VRP, IV level, trend,
DTE). This harness is the **proof step**: run that router over the historical weekly NIFTY cycles
and report whether the *selection layer* beats the alternatives. The one question:

> Does routing-among-GO-strategies produce a better **ROM** (return-on-SPAN-margin) than (a) the
> single best GO strategy run alone, and (b) the same router with no vol-gate?

If the orchestrated row does not clearly beat the best single strategy on ROM, the selection layer
adds no edge and the router is not worth deploying. ROM is the headline; net / win / PF / Sharpe /
GO are reported alongside for context.

### Honesty caveats (carried verbatim from `fno_strategies` — never dropped)
Every report this harness prints/archives MUST repeat the Phase-0 caveats, because the orchestrated
result inherits all of them and adds one:
1. **VIX-as-weekly-IV proxy** — `straddle_iv = India VIX / 100`; weekly ATM IV (~7-DTE) trades above
   the 30-day VIX (term-structure bias). Regime-dependent.
2. **Close-not-FSP settlement** — `expiry_spot` is the NIFTY daily CLOSE, not the NSE Final
   Settlement Price (30-min futures VWAP 15:00–15:30 IST). GO is preliminary.
3. **EXPIRY-ONLY, tail-blind** — no intra-cycle path stop; undefined-risk legs (none of the GO set,
   but if any enters the router they carry the `_UNDEFINED_RISK_DISCLAIMER`).
4. **Single-σ Black-76** — no smile/skew; conservative for credit sellers, NOT for debit.
5. **NEW (selection-layer caveat):** the router is fit/selected on the SAME history it is scored on.
   Any per-regime selection rule with free parameters is **in-sample-optimistic**. The harness MUST
   report the **OOS (last-30%) ROM / Sharpe** for the orchestrated row, computed exactly like
   `sharpe_oos` in `run_strategy_backtest`, and the report must state that the headline OOS numbers —
   not the full-sample numbers — are the ones that gate a GO.

**ROM is the headline. Defined-risk only for any live consideration. PAPER only.**

---

## 1. Entrypoint / CLI

New module `research/backtest/orchestrator.py`, runnable as `python -m research.backtest.orchestrator`.
The CLI **mirrors `fno_strategies.main()`** (same flag names, same DB-init-after-parse discipline,
same printed table shape) so the two are operationally interchangeable.

```
python -m research.backtest.orchestrator \
    --index NIFTY \            # index key; NIFTY only today (see §6). Drives the per-index registry.
    --mode weekly \            # weekly | expiry_calendar — passed straight to cycles_from_db
    --gate both \              # vol | none | both  (default both) — gate applied INSIDE the router
    --router regime \          # router policy id (default "regime"); see spec 03/04 for policies
    --strategies all \         # GO-set to route among: "all" = the GO candidates; or csv subset
    --k 0.9 \                  # vol-gate threshold (DEFAULT_K) — same param as fno_strategies
    --capital 200000 \
    --span-pct <float> \       # optional override, forwarded as a param
    --param KEY=VAL \          # repeatable extra params (parsed float-if-possible), same as fno
    --compare all \            # what comparison rows to print: all | singles | ungated | orchestrated
    --upload-s3                # archive results to s3://.../kronos/m3/orchestrator/ (default: off)
```

Flag-by-flag mapping to `fno_strategies`:

| fno_strategies flag | orchestrator flag | behaviour change |
|---|---|---|
| `--strategy {name|all}` | `--strategies {csv|all}` | now a *candidate set* the router chooses among (not one run). `all` resolves to the GO set (§2), NOT every registry key. |
| `--gate {vol,none,both}` | `--gate {vol,none,both}` | identical semantics; `both` = run the router twice (gated vs ungated) for the A/B. `vol` is the default-of-record but `both` is the CLI default so the A/B always prints. |
| `--mode` | `--mode` | unchanged; forwarded to `cycles_from_db`. |
| `--k --capital --span-pct --param` | same | unchanged; forwarded into each `run_strategy_backtest` call. |
| — (new) | `--index` | selects the per-index registry row (lot/step/expiry-calendar/security-ids). |
| — (new) | `--router` | selects the router policy (the selection layer under test). |
| — (new) | `--compare` | selects which comparison rows render (default `all`). |
| — (new) | `--upload-s3` | mirrors the `--upload-s3` flag added to the finetune/M3 path. |

The CLI **never** edits the equity `__main__.py` or `fno_strategies.py`; it imports
`FNO_STRATEGIES`, `run_strategy_backtest`, `cycles_from_db`, and `gate_decision`.

---

## 2. The GO set (what the router chooses among)

`--strategies all` resolves to the defined-risk GO candidates from `_CONTEXT.md` (gated GO on the
preliminary backtest):

```python
GO_SET = ("iron_condor", "bull_put_spread", "credit_put_spread", "broken_wing_condor")
```

These are all `defined_risk=True, sell_premium=True` in `FNO_STRATEGIES`. The router is **defined-risk
only** — undefined-risk keys (`short_straddle`, `short_strangle`, `jade_lizard`, `ratio_spread`) and
directional/long-premium NO-GO keys are excluded by default. A user MAY pass a csv subset (including
non-GO names) for exploration; if any non-defined-risk name is included, the harness emits the
`_UNDEFINED_RISK_DISCLAIMER` on the orchestrated row and forces the headline to read "diagnostic only".

---

## 3. How it reuses `cycles_from_db` + the vol-gate (do NOT reimplement)

Load cycles ONCE, exactly like `fno_strategies.main()`:

```python
from config import get_config
from db import init_db
init_db(get_config().db_url)            # F&O tables live in dhan_trading (cfg.db_url), NOT dhan_clean
cycles = cycles_from_db(symbol=index, mode=args.mode,
                        nifty_id=reg.index_id, vix_id=reg.vix_id, timeframe="1d")
```

`cycles_from_db` is re-exported unchanged from `fno_strategies`. The same `cycles` list feeds every
row of the comparison so all rows see **identical entry dates, spot, IV proxy, expiry_spot, costs** —
the A/B is apples-to-apples (same discipline as the existing `--gate both`).

### The router is a per-cycle selection over `gate_decision`
The router does NOT re-price or re-resolve. For each cycle it decides WHICH builder (or none) to run,
then delegates pricing/resolution/costs/SPAN/metrics to the existing engine. The mechanics:

1. **Vol-gate state** comes from the *same* `gate_decision(realized_vol_20d, straddle_iv, k=k)` call
   the engine uses. Under `--gate vol` the router only deploys a SELL_PREMIUM strategy on
   `SELL_PREMIUM` cycles and stands aside on `BUY_PREMIUM`/`STAND_ASIDE`. Under `--gate none` the
   router may deploy on every cycle (the gate decision is still computed and recorded, never acted on).
2. **Regime features** (VRP = `straddle_iv − realized_vol_20d`, IV level, DTE, simple trend if the
   policy uses it) are derived from the cycle dict fields already present — no new DB reads.
3. The router policy (`--router`, defined in specs 03/04) maps (gate-state, regime) → one
   `GO_SET` member **or** STAND_ASIDE for that cycle.

### Building the orchestrated trade list without forking the engine
The engine's `run_strategy_backtest` filters cycles internally by gate AND prices the whole list. To
get a *router-selected* result while reusing that exact pricing/cost/SPAN/metrics path, the harness
runs each candidate over a **per-strategy sub-list of the cycles it was selected for**, then merges:

```python
# 1. Router assigns each cycle to at most one strategy (or None).
assignments = router.assign(cycles, k=k, gate=gate_mode)   # -> {strategy_name: [cycle, ...]}, plus stand_aside cycles

# 2. Run the UNCHANGED engine per assigned strategy, gate="none" because the router
#    already did the gating in step 1 (avoids double-gating; the engine still RECORDS
#    the gate decision per trade for the report).
per_strat = {
    name: run_strategy_backtest(FNO_STRATEGIES[name], sub_cycles, extra,
                                k=k, capital=capital, gate="none")
    for name, sub_cycles in assignments.items() if sub_cycles
}

# 3. Merge the per-strategy StrategyTrade lists into one chronological trade list and
#    recompute the SAME aggregate metrics the engine produces.
orchestrated = aggregate_orchestrated(per_strat, n_cycles=len(cycles), capital=capital, gate=gate_mode)
```

**Critical invariant:** step 2 passes `gate="none"` because the router (step 1) is the gate authority
under `--gate vol`; passing `gate="vol"` again would double-filter and silently drop the cycles the
router chose. Under `--gate none` the router itself does no gating, so the merged result is the
"ungated router" baseline. The gate decision label is still attached to every `StrategyTrade` by the
engine, so the report can show how many trades came from each regime.

### `aggregate_orchestrated` — reuse the engine's own metric math
To guarantee the orchestrated row is computed identically to a single-strategy row, `aggregate_orchestrated`
concatenates the per-strategy `trades` (each a `StrategyTrade`), sorts by `entry_date`, and reuses the
**same statistics** the engine uses (`_sharpe_from_pnls`, `_max_drawdown`, the 70/30 chronological
IS/OOS split, `return_on_margin = net_pnl / Σ span`, `mean_span`, `profit_factor`, `win_rate`) and the
**same `go_no_go`**. Implementation reuses the private helpers by import (`from research.backtest.fno_strategies
import _sharpe_from_pnls, _max_drawdown`) — no copy-paste of the formulas. The output dict has the SAME
key shape as `run_strategy_backtest` plus orchestrator-only keys (§4.2).

---

## 4. Comparison design + output schema

### 4.1 Rows produced (per gate mode)
For each gate mode in `{vol, none}` (or just the one selected), the harness produces:

| row kind | how computed | purpose |
|---|---|---|
| **single** (one per `GO_SET` member) | `run_strategy_backtest(spec, cycles, gate=gate_mode)` — the strategy run ALONE over all cycles | baseline (a): "best single GO strategy alone" |
| **orchestrated** | the router-merged result from §3 | the thing under test |
| **best_single** (derived) | the single row with the highest **ROM** | the bar the orchestrated row must clear |

The headline A/B is **`orchestrated@vol` vs `orchestrated@none`** (does the gate help the router?) and
**`orchestrated@vol` vs `best_single@vol`** (does selection beat the best one strategy?). `--compare`
selects which subset renders; `all` prints singles + orchestrated for both gate modes.

### 4.2 Per-row metric schema (the output table columns)
Mirrors the `fno_strategies` printed table (`strategy / gate / n / net_pnl / ROM / go?`) and EXTENDS it
so the selection-layer story is legible:

| column | source key | notes |
|---|---|---|
| `row` | — | `iron_condor`, …, or `ORCHESTRATED`, or `best_single` |
| `gate` | `gate` | `vol` / `none` |
| `n` | `n_trades` | trades actually placed (router stand-asides excluded) |
| `n_cycles` | `n_cycles` | total cycles offered (same for every row) |
| `net` | `net_pnl` | ₹ after costs |
| `ROM` | `return_on_margin` | **headline**, formatted `%` |
| `ROM_oos` | derived | net_pnl(OOS 30%) / Σ span(OOS) — the gating ROM for orchestrated |
| `win` | `win_rate` | `%` |
| `PF` | `profit_factor` | `∞` when no losses |
| `sharpe` | `sharpe` | annualised ×√52 |
| `sharpe_oos` | `sharpe_oos` | last-30% Sharpe |
| `GO` | `go_no_go[0]` | `GO`/`NO-GO` |
| `picks` | orchestrated-only | per-strategy deploy count + #stand-aside (e.g. `ic:11 bps:7 aside:6`) |

Orchestrated-only keys added to the result dict (beyond the standard engine shape):
`router` (policy id), `index`, `picks` (`{strategy_name: count}` + `stand_aside`), `rom_oos`,
`per_strategy` (the sub-result dicts for drill-down).

### 4.3 Printed table (stdout)
```
ORCHESTRATOR BACKTEST — index=NIFTY router=regime mode=weekly k=0.90 capital=₹200,000
caveats: VIX-as-weekly-IV proxy · CLOSE-not-FSP settlement · EXPIRY-ONLY (tail-blind) · single-σ B76 · selection in-sample-optimistic → read ROM_oos
n_cycles=46

row                  gate    n      net      ROM  ROM_oos    win     PF  sharpe  sh_oos    GO   picks
-------------------------------------------------------------------------------------------------------
iron_condor          vol    18   45,200   3.91%    3.10%   72.2%   1.85    1.40    0.90    GO   -
bull_put_spread      vol    14   38,900   7.19%    5.80%   78.6%   2.10    1.62    1.05    GO   -
credit_put_spread    vol    16   21,400   2.70%    1.90%   68.8%   1.40    0.95    0.40 NO-GO   -
broken_wing_condor   vol    17   24,800   2.67%    2.00%   70.6%   1.45    1.02    0.55    GO   -
best_single          vol    14   38,900   7.19%    5.80%   78.6%   2.10    1.62    1.05    GO   =bull_put_spread
ORCHESTRATED         vol    24   58,300   8.40%    6.70%   79.2%   2.30    1.80    1.20    GO   ic:9 bps:8 bwc:7 aside:22
-------------------------------------------------------------------------------------------------------
ORCHESTRATED         none   46   41,000   3.10%    2.40%   63.0%   1.35    0.80    0.30 NO-GO  ic:16 bps:15 bwc:15 aside:0

VERDICT: orchestrated@vol ROM_oos 6.70% vs best_single@vol 5.80% (Δ +0.90pp) → selection adds edge (PRELIMINARY)
         orchestrated@vol vs orchestrated@none ROM_oos 6.70% vs 2.40% → vol-gate adds edge inside the router
(numbers illustrative — schema only)
```
The `go_no_go` reason string for the orchestrated row is printed in full below the table (same as
`fno_strategies` does for the single-strategy case), including the undefined-risk disclaimer if any
non-defined-risk name was forced in.

### 4.4 The VERDICT line (decision rule)
Promote-the-router signal requires, on **OOS** numbers:
`orchestrated@vol.ROM_oos > best_single@vol.ROM_oos` **and** `orchestrated@vol.sharpe_oos > best_single@vol.sharpe_oos`
**and** `orchestrated@vol.go_no_go == GO`. The harness prints PRELIMINARY (never "promote") and reminds
that real-IV forward paper-log is the truth test. This is a research signal, not a deploy gate.

---

## 5. S3 archival layout

Mirrors the M3 / kronos S3 convention (`KRONOS_CHECKPOINT s3://…`, the `--upload-s3` flow). On
`--upload-s3`, results write under:

```
s3://<bucket>/kronos/m3/orchestrator/
  index=NIFTY/
    router=regime/
      <run_ts>/                         # run_ts = UTC ISO8601, e.g. 20260620T1530Z
        results.json                    # full machine-readable: every row dict (incl per_strategy, picks, go_no_go, caveats)
        summary.txt                     # the exact stdout table + VERDICT block (human-readable)
        cycles_meta.json                # {n_cycles, date_range, mode, k, capital, index_id, vix_id, fno_strategies git sha}
        manifest.json                   # {schema_version, created_utc, args, caveats[], code_sha, decision: PRELIMINARY}
      latest -> <run_ts>/               # "latest" pointer object (copy of manifest.json with resolved_run_ts)
```

- `<bucket>` resolves from the same config the kronos path uses (do NOT hardcode; read via config /
  env, fail closed with a clear error if unset). Region/profile per the existing AWS setup.
- Partitioning by `index=` and `router=` makes the multi-index / multi-policy future a no-op (§6):
  other indices and policies land as new prefixes, never overwriting NIFTY.
- `results.json` is the source of truth for downstream diffing (e.g. comparing two router policies);
  `summary.txt` is for human PR review. Both embed the caveats array so an archived result can never
  be read out of context.
- Upload is **fail-open for the backtest** (an S3 error logs + warns but does not crash the run — the
  stdout table is still the primary deliverable), matching the kronos gate's fail-open posture.

---

## 6. Index-parameterisation (NIFTY now, others when data exists)

Per `_CONTEXT.md` HARD REALITY #1, the harness is index-agnostic in shape but NIFTY-only in data.
`--index` resolves a registry row (lives in the engine spec's per-index registry — specs 01/02; this
harness only *consumes* it):

```python
@dataclass(frozen=True)
class IndexReg:
    key: str          # "NIFTY"
    index_id: str     # "13"  -> cycles_from_db(nifty_id=...)
    vix_id: str       # "21"  -> cycles_from_db(vix_id=...)  (None for indices w/o a vol index)
    lot: int          # NIFTY_LOT
    step: int         # 50
    symbol: str       # "NIFTY" -> cycles_from_db(symbol=...) for expiry_calendar mode
```

Today only `NIFTY` resolves (id 13 / VIX 21). Any other `--index` raises a clear
`NotImplementedError("no historical data for <index>; ingest first (see _CONTEXT.md #1)")` rather than
silently running on empty cycles. `lot`/`step` flow into `run_strategy_backtest(..., lot=reg.lot,
step=reg.step)`. The S3 `index=` partition is already keyed, so when BANKNIFTY/FINNIFTY data lands the
SAME harness runs unchanged: `python -m research.backtest.orchestrator --index BANKNIFTY …`.

---

## 7. Implementation checklist (build order)

1. `research/backtest/orchestrator.py` — module skeleton; import `FNO_STRATEGIES`, `run_strategy_backtest`,
   `cycles_from_db`, `gate_decision`, `_sharpe_from_pnls`, `_max_drawdown`, `go_no_go`, `NIFTY_LOT`.
2. `IndexReg` registry (NIFTY only) + `GO_SET`.
3. `Router` interface (`assign(cycles, *, k, gate) -> assignments`) — the `regime` policy per specs 03/04.
   Keep the policy pluggable behind `--router`.
4. `aggregate_orchestrated(per_strat, n_cycles, capital, gate)` — merge + reuse engine stats + `go_no_go`;
   add `rom_oos`, `picks`, `router`, `index`, `per_strategy`.
5. `run_comparison(cycles, gate_mode, …) -> list[row_dict]` — singles + best_single + orchestrated.
6. `print_table(rows)` + `verdict(rows)` — the §4.3 / §4.4 output incl. full go_no_go reason + caveats.
7. `archive_s3(rows, args, …)` — the §5 layout, fail-open, config-resolved bucket.
8. `main()` — CLI per §1, DB-init-after-parse, `--gate both` loop, optional `--upload-s3`.
9. Tests (pure, no DB): synthetic `cycles` list → assert orchestrated metrics equal a hand-merged
   single-strategy run when the router degenerates to one strategy (equivalence test, mirroring the
   condor-equivalence test); assert `gate="none"` in step 2 (no double-gating); assert OOS split matches
   the engine; assert non-GO name forces the disclaimer. CLI `--help` must not touch the DB.

---

## 8. Discipline summary (non-negotiable)
- Call `fno_strategies` / `fno_condor`; never re-implement pricing, resolution, costs, SPAN, or metrics.
- ROM is the headline; OOS ROM/Sharpe gate the PRELIMINARY verdict.
- All caveats (VIX-proxy, CLOSE-not-FSP, EXPIRY-ONLY/tail-blind, single-σ, selection-in-sample) printed
  AND archived on every run.
- Defined-risk GO set only by default; PAPER only; no live order path.
- NIFTY today; index-parameterised so other indices run unchanged once ingested.
