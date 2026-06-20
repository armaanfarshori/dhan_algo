# 09 — Validation Rigor for the F&O Orchestrator (don't fool ourselves)

**Scope.** This spec defines how the *orchestration engine* (the regime-aware router that picks
which GO strategy to deploy per cycle — see `_CONTEXT.md`) is validated *as a routing policy*, not
just as a bag of legs. The single-strategy condor harness already has fidelity caveats
(`research/backtest/fno_condor_report.md` §5); the router ADDS its own failure mode — **selection
overfitting** — on top of the data biases it inherits. This spec is the truth-test before any
result is quoted as an "edge."

**Standing constraints.** PAPER only. Defined-risk only for live. NIFTY-only data (BANKNIFTY/etc.
forward-only once ingested). ROM (return-on-SPAN-margin) is the headline; return-on-capital is the
honesty check. Nothing here authorises a live order.

---

## 0. The core risk in one sentence

The router is tuned on the SAME ~166–233 weekly cycles it is then scored on, using IV/settlement
proxies whose bias is **regime-dependent and not reliably conservative** — so a strong in-sample
ROM can be **two layers of self-deception** (proxy-optimism × policy-overfit) rather than edge.
Both layers must be bounded before the headline is believed.

---

## 1. Bias list (inherited + router-specific)

Each bias gets: source, **direction** (optimistic = inflates our result), and how we **bound** it
(turn an unknown into a quantified worst case), not just acknowledge it.

### Inherited from the single-strategy harness (apply to EVERY strategy the router can pick)

| # | Bias | Direction | Bound / mitigation |
|---|---|---|---|
| B1 | **VIX (30d) as weekly (4–7 DTE) straddle IV.** Real 4-DTE ATM IV was 0.75× VIX on a calm day (2026-06-19 snapshot). | **REGIME-DEPENDENT — optimistic in calm/contango (most of 2022–24), conservative only in stress/backwardation.** This is the dominant risk because the router sells premium *most often exactly in calm regimes*. | (a) **IV-haircut sensitivity sweep**: re-run the full router at `straddle_iv ∈ {1.00, 0.90, 0.80, 0.75}× VIX` and report ROM at each. (b) **Regime-conditional haircut**: apply the haircut only when term-structure is in contango (VIX above realized). (c) Replace with **real per-expiry ATM IV** as the forward collector accrues it — the only true fix. The router is not believed until its edge survives the 0.80× haircut. |
| B2 | **Settlement = index daily CLOSE, not NSE FSP** (15:00–15:30 VWAP of futures). | **Ambiguous** — flips near-the-money weeks either way. Matters MORE for the router because it changes which strategy *looks* best in marginal weeks (selection sensitivity). | **FSP-jitter stress test**: perturb `expiry_spot` by ±N index points (N ∈ {10, 25, 50}) and re-route; report how often the router's per-cycle pick *changes* and the ROM spread. A router whose pick is fragile to ±25 pts is overfit to close-noise. Real fix: NSE FSP. |
| B3 | **Entry at prior-expiry CLOSE**, not next-morning open. | **Mildly optimistic** (close is smoother than next-open gap). | Model a **next-open slippage** add-on to entry credit (overnight gap proxy from index OHLC). Report ROM with and without. |
| B4 | **EXPIRY-ONLY resolution — tail-blind / no intra-cycle path.** No mid-week stop, no gamma-blowout, no early breach. | **Optimistic for UNDEFINED-risk** (already excluded — short straddle/strangle/ratio/jade_lizard are NO-GO). For **defined-risk** spreads the max loss is capped by the wings, so expiry-only mis-states *timing/PF*, not *max loss*. | Defined-risk only — this is the structural reason undefined-risk is excluded from live regardless of its backtest net. For the defined-risk set, add a **mid-cycle max-adverse-excursion** check (intra-week index path vs short strikes) to estimate how often a managed trader would have been stopped — informational, not a gate. |
| B5 | **realized_vol_20d (20d backward) vs VIX (30d forward)** horizon mismatch in the gate. | **Indeterminate** — gate fires slightly more/less than truth. | Sensitivity: re-run gate with realized_vol_30d; report pass-rate and ROM delta. |
| B6 | **Sharpe on absolute ₹ P&L** (scale-dependent; meaningless across capital/lot sizes). | N/A — comparison hazard. | Lock ONE capital + lot parameterisation for all router comparisons; never compare Sharpe across configs. Prefer **ROM** and **return-distribution** stats for cross-config comparison. |
| B7 | **Synthetic ISO-week cycles**, not actual expiry days; rare fully-closed week → >7-DTE cycle. | Negligible at regime-screen level; small. | Re-validate on `mode="expiry_calendar"` once forward expiries accrue. Flag any cycle with DTE > 8 and report count. |

### Router-specific (NEW — these do not exist for a single strategy)

| # | Bias | Direction | Bound / mitigation |
|---|---|---|---|
| R1 | **Selection overfitting** — the routing policy (gate k, IV-rank/VRP/trend/DTE thresholds, strategy-priority ordering) is fit on the same cycles it is scored on. With 4 GO strategies × a handful of regime features, the policy has enough degrees of freedom to "explain" historical noise. | **Optimistic** (in-sample ROM > true). The headline killer. | **Walk-forward / OOS by cycle** (§2). The router is believed only on its **aggregated OOS** ROM, never in-sample. |
| R2 | **Multiple-comparisons / strategy-cherry-picking.** Picking the best of 4 strategies per cycle inflates max ROM purely by selection (max of N noisy draws). | **Optimistic.** | Report **"always-condor" baseline** and **"random-eligible-pick" baseline** alongside the router. The router must beat *both* OOS, by a margin larger than the OOS noise band, to claim the *selection layer* adds value (vs. just being long the best single strategy). |
| R3 | **Per-cycle margin denominator drift.** ROM = return / SPAN margin, but margin differs per strategy and per regime (vol-scaled). Routing into low-margin weeks can inflate aggregate ROM. | **Optimistic if naively averaged.** | Report **margin-weighted ROM** (Σ pnl / Σ margin), not the mean of per-cycle ROMs. Also report **return-on-capital on a fixed book** so a high ROM on tiny deployed margin can't masquerade as portfolio edge (the honesty check, §4). |
| R4 | **Survivorship — instrument & regime.** NIFTY index is survivorship-safe (it's an index, not a basket of delisted names), BUT the *option-chain availability* and the *gate-calibration window* are not regime-representative: 2022–24 was largely calm/contango (the regime where B1 is optimistic), so the cycle sample over-weights the regime that flatters us. | **Optimistic** (sample skewed to favorable regime). | Report **per-regime ROM** (split cycles by VIX terciles / contango-vs-backwardation). Require the router to be **non-catastrophic in the stress tercile** (negative but bounded), not just great in calm. Note explicitly that BANKNIFTY/etc. are absent → no cross-index survivorship claim is permitted. |
| R5 | **Small sample.** n ≈ 166–233 weekly cycles total; after an OOS split each fold's test set is ~30–80 cycles. Sharpe/PF confidence intervals are wide. | Neither — but invites false precision. | Attach **bootstrap CIs** (§3) to every headline metric; quote ranges, not point estimates. Enforce **n ≥ 30 per OOS test fold** (mirrors the existing `n_trades ≥ 30` guard). |
| R6 | **Look-ahead in features.** Any router input (IV rank, realized vol, VRP, trend) must use data available at the entry timestamp only. Easy to leak a full-history percentile (e.g. IV-rank computed over the *whole* sample). | **Optimistic.** | IV-rank / percentiles computed on a **trailing expanding window only**; assert in the harness that no feature reads a row with `date > entry_date`. |

---

## 2. Walk-forward / OOS design for the routing policy

**Unit of split = the CYCLE (weekly), in chronological order.** Never random-shuffle a time series
(per `CLAUDE.md` Kronos rule). The policy is *fit* (gate k, thresholds, priority ordering) on past
cycles and *scored* only on strictly-future cycles it never saw.

### 2a. Primary method — anchored (expanding-window) walk-forward

```
Cycles sorted ascending by entry_date: C[0..N-1]
Initial train window: first ~40% of cycles (must contain ≥1 stress tercile if possible)
Step (test fold) size: ~20–30 cycles (≈ 6 months of weeklies)

for each fold f:
    TRAIN = C[0 .. train_end_f]              # everything up to the fold
    TEST  = C[train_end_f+1 .. train_end_f+step]   # the next block, unseen
    fit routing policy on TRAIN only:
        - calibrate gate k via calibrate_threshold(samples from TRAIN window)
        - fit any regime thresholds / strategy-priority on TRAIN
    freeze policy; run router on TEST; collect per-cycle pnl, margin, picked-strategy
    train_end_f += step   # expand the anchor
Concatenate all TEST results → the OOS track record (this is THE result).
```

- The **headline ROM is the concatenated OOS track**, never any in-sample number.
- Report **per-fold** ROM/PF/win% so we can see if the edge decays over time (regime drift).
- **No peeking:** k and every threshold are re-fit per fold from TRAIN only (R6).

### 2b. Secondary — purged/embargoed split for the gate calibration

The vol-gate uses `realized_vol_20d` (20-day lookback) and a 30-day-ish IV horizon, so a TEST cycle
within ~30 days of the TRAIN boundary shares overlapping vol windows → leakage. **Embargo** the
first ~6 trading days (≈ one weekly cycle) of each TEST fold's features from any TRAIN-derived
percentile, and **purge** any TRAIN cycle whose realized-vol window overlaps the TEST entry.

### 2c. Honesty baselines (run on the SAME OOS folds)

1. **Always-condor** (no routing) — the incumbent single-strategy result.
2. **Random-eligible-pick** — uniform random among GO strategies that pass the gate that cycle
   (seeded, averaged over ≥100 seeds → distribution).
3. **Oracle (in-sample best per cycle)** — *upper bound only, never quotable* — shows the ceiling
   the router is chasing and how far OOS sits below it (the overfit gap).

The **selection layer earns its keep only if OOS router ROM > both (1) and the 95th pct of (2)**,
by more than the bootstrap noise band (§3). Otherwise we are just long the best single strategy and
the orchestrator is unjustified complexity.

### 2d. Stability / robustness checks

- **Parameter neighborhood:** perturb each router threshold ±1 step; OOS ROM should degrade
  gracefully, not cliff. A knife-edge optimum = overfit.
- **Leave-one-regime-out:** train excluding the stress tercile, test on it (and vice versa) — does
  the policy generalise across regimes or only within the calm regime it was born in (R4)?

---

## 3. Confidence intervals & sample honesty

- **Block bootstrap** (block = contiguous run of cycles, length ≈ 4–6 to respect vol
  autocorrelation) over the OOS pnl series → 95% CIs for net ROM, PF, Sharpe, win%, max DD.
- Quote every headline as **point [lo, hi]**, e.g. `OOS ROM 3.9% [1.1%, 6.4%]`.
- **Deflated metrics:** because we tried 4 strategies + a routing policy, apply a multiple-testing
  haircut to Sharpe (deflated Sharpe ratio, or at minimum report the number of configs tried).
- Enforce **n ≥ 30 per OOS test fold**; if a fold is short, merge it with the adjacent fold.

---

## 4. ROM-vs-return-on-capital honesty

ROM is the right *strategy-efficiency* metric but is a **misleading portfolio metric** if quoted
alone: a 7% ROM on margin that is only deployed 30% of weeks, on 20% of book capital, is not a 7%
portfolio return. Every result table MUST carry BOTH columns, computed the same way for router and
baselines:

| Metric | Definition | Why |
|---|---|---|
| **Margin-weighted ROM** | Σ net_pnl / Σ SPAN_margin (NOT mean of per-cycle ROMs) | Strategy efficiency without small-denominator inflation (R3) |
| **Return on fixed book** | Σ net_pnl / fixed_capital over the OOS window, annualised | What the user actually earns; penalises standing aside / low deployment |
| **Capital deployment %** | mean(SPAN_margin / fixed_capital) across cycles incl. stand-aside weeks | Exposes "great ROM, barely invested" |
| **Max DD on fixed book** | peak-to-trough of cumulative net_pnl / fixed_capital | The 15%-of-capital gate denominator |

Stand-aside weeks count as a 0-return cycle in return-on-book (they don't in ROM) — this is the
point: the router must justify standing aside by *avoiding losses*, visible only on the book metric.

---

## 5. Decision gates (orchestrator-specific)

Sequential. Each gate is binary; failing one stops the project there. All numbers are on the
**concatenated OOS track (§2)** unless stated.

### G1 — Fully-costed + bias-bounded (the "is this even real?" gate)

ALL must hold:
- Full post-Apr-2026 options cost stack applied to every leg of every routed strategy
  (`research/backtest/fno_costs.py`), incl. adverse slippage ≥ 0.5%.
- **Bias bounds reported, not just acknowledged:** B1 IV-haircut sweep, B2 FSP-jitter, B3 next-open
  add-on, R6 look-ahead assertion all RUN.
- **Survives the B1 0.80× IV haircut**: OOS net ROM still > 0 after costs at `straddle_iv = 0.80×VIX`.
- **No look-ahead**: harness assertion passes (no feature reads `date > entry_date`).
- **n ≥ 30 per OOS fold**, ≥ 3 folds.

→ Pass = "the result is plausibly real, not a proxy/leak artifact." Fail = back to data fidelity
(real ATM IV / FSP), no further claims.

### G2 — Real risk-adjusted edge AND selection value (the "does the router earn its complexity?" gate)

ALL must hold, on OOS:
- net P&L > 0, **margin-weighted ROM > 0**, **return-on-fixed-book > 0**, PF > 1, Sharpe > 0
  (mirrors `go_no_go`, but on the OOS concatenation and on both ROM and book return).
- **Max DD on fixed book < 15% of capital** (the existing hard limit, on the honesty denominator).
- **Selection value:** OOS router ROM > always-condor baseline AND > 95th pct of random-eligible
  baseline, **by more than the bootstrap CI half-width** (§3). (If it ties the baselines, ship the
  single strategy, not the router.)
- **Regime robustness (R4):** stress-tercile OOS ROM is bounded (not catastrophic); leave-one-
  regime-out does not collapse to negative.
- **Stability (§2d):** ±1-step threshold perturbation keeps OOS ROM positive (no knife-edge).
- Bootstrap 95% CI lower bound on net ROM is **> 0** (not just the point estimate).

→ Pass = "there is a real, router-attributable, regime-robust edge after honest accounting."
Fail = de-scope to the best single defined-risk strategy, or NO-GO.

### G3 — User-gated live (the "human owns the trigger" gate)

NOT automatable. ALL of:
- G1 + G2 passed and written into `fno_condor_report.md` / a new orchestrator report with the OOS
  tables, CIs, and bias bounds.
- **Re-validation on REAL per-expiry ATM IV + NSE FSP** has reproduced the OOS edge (the proxy
  layer removed, not just bounded) — at minimum the forward real-IV paper-log trend agrees in sign
  and rough magnitude.
- **Defined-risk only** confirmed for every routable strategy (undefined-risk stays excluded).
- **Forward paper-log** of the live orchestrator (real chain snapshots, real IV) shows positive
  expectancy over a meaningful number of *forward* cycles — the only fully out-of-sample test.
- **Explicit user authorisation** (per `CLAUDE.md` safety rules: `PAPER_TRADING=true` is default;
  live needs `.env` edit + `ALLOW_LIVE_TOGGLE=true` + restart, on the trusted machine only).

→ Pass = user, not the agent, flips live with eyes open. The agent never auto-promotes past G2.

---

## 6. What "fooling ourselves" looks like (anti-patterns to reject in review)

- Quoting the in-sample (whole-history-fit) router ROM as the result. → Use OOS only.
- A great mean-of-per-cycle ROM on tiny deployed margin. → Margin-weighted ROM + book return.
- Edge that exists only in the calm tercile (where B1 is optimistic). → Per-regime + leave-one-out.
- Router beats nothing once the IV haircut is applied. → G1 0.80× gate.
- Picking `move_mult`/k/thresholds that maximise the headline, then reporting that headline. →
  Walk-forward re-fit per fold; report the parameter-neighborhood degradation.
- Treating expiry-only PF on undefined-risk as edge. → Defined-risk only; B4.

---

## 7. Minimal harness checklist (so this is buildable, not aspirational)

- [ ] Cycle-ordered walk-forward splitter (anchored expanding window, embargo+purge).
- [ ] Per-fold policy re-fit (k via `calibrate_threshold` on TRAIN; thresholds on TRAIN).
- [ ] OOS concatenator → metrics (reuse `run_backtest`/`go_no_go` math, fed OOS cycles).
- [ ] IV-haircut sweep + FSP-jitter + next-open add-on as harness flags.
- [ ] Always-condor + random-eligible + oracle baselines on identical folds.
- [ ] Block-bootstrap CI util over the OOS pnl series.
- [ ] Both ROM (margin-weighted) and return-on-fixed-book columns in every table.
- [ ] Look-ahead assertion (`entry_date` cutoff) in feature construction.
- [ ] Per-regime (VIX tercile / term-structure) breakdown.

> This spec governs *interpretation*, not just code: a router result is not an "edge" until it is
> on the OOS track, bias-bounded (G1), router-attributable and regime-robust (G2). Everything before
> that is "worth validating," not "validated."
