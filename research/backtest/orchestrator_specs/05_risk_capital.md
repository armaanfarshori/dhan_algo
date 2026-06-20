# 05 — Risk & Capital-Allocation Layer (F&O Orchestrator)

**Status:** PLAN · PAPER-ONLY · NIFTY-first, index-agnostic by construction
**Owns:** how the orchestrator budgets SPAN margin, caps concurrent risk, sizes per index,
enforces daily/weekly loss limits, and trips the kill-switch.
**Sits on top of:** `research/backtest/fno_strategies.py` (legs, SPAN model, ROM) + `ml/fno_vol_gate.py`
(regime gate). **Does NOT** edit those internals — it calls them.
**Mirrors:** the equity `engine/risk.py` discipline — *RiskEngine owns the kill-switch; exits are never
blocked; loss meters come from the DB, not process memory; halts persist across restarts.*

> Read `_CONTEXT.md` first. ROM (return-on-SPAN-margin) is the headline metric. **Defined-risk only for
> live.** Undefined-risk strategies are EXCLUDED from live and admitted to the diagnostic backtest only
> behind the EXPIRY-ONLY / tail-blind label.

---

## 0. Non-negotiables (inherited, never override)

1. `PAPER_TRADING=true` stays true. This layer governs paper + backtest sizing; it does not unlock live.
2. **The orchestrator RiskEngine owns the kill-switch.** Every entry routes through `check_intent`-equivalent.
   No strategy, router, or scheduler may place a position the risk layer has not cleared.
3. **Exits are never blocked** — not by a kill-switch, not by a loss halt, not by a margin cap. A halt that
   swallowed a square-off would leave naked option legs open. Closing/rolling legs of an existing position
   is always allowed (mirror `engine/risk.py` `is_exit` early-return).
4. **Loss meters are DB-derived and restart-proof.** Daily/weekly realized P&L is read from the trades
   ledger, never from in-process counters. A restart must not reset the loss meter.
5. **Loss halts persist** to disk for their scope (day/week) and require deliberate human reset — same as
   `run/halt_state.json` in the equity engine.
6. **Live = defined-risk only.** `defined_risk=True` AND `span_model="defined"` are *both* required for a
   strategy to be live/paper-eligible. `naked_short` and `spread_naked_mix` are backtest-diagnostic only.

---

## 1. Risk geometry — fractions of equity, denominated against SPAN

The equity engine sizes against stop-distance risk. Options have **no broker stop**; the bounded loss of a
defined-risk spread *is* its risk, and the capital actually committed is the **blocked SPAN margin**. So the
F&O risk geometry is anchored on SPAN, with ROM as the efficiency lens.

Two distinct quantities per position, both already produced by `fno_strategies.py`:

| Quantity | Source | Meaning |
|---|---|---|
| **SPAN** | `span_margin(spec, legs, entry_net, spot, lot, params)` | ₹ broker blocks (capital committed). |
| **Max-loss** | `_span_defined(...)` for defined-risk = `(wing_width − credit)·lot` (credit) or `abs(debit)·lot` (debit) | ₹ worst-case loss to expiry. **For defined-risk, max-loss = the "defined" SPAN.** |

For defined-risk spreads these coincide (the broker blocks the max theoretical loss), so a single number
serves as both *capital committed* and *risk committed*. That is exactly why **only defined-risk is live-eligible**:
SPAN-budget == loss-budget, with no tail beyond it.

All limits below are **fractions of current equity** (paper balance + all-time realized P&L from the DB), so
paper rehearses live and sizing shrinks in drawdown — identical philosophy to `RiskParams`.

```
equity = max(1, equity_base + realized_total_from_db)      # restart-proof, compounding/de-compounding
```

---

## 2. Parameters (`FnoRiskParams`)

Mirror `engine.risk.RiskParams`; everything is a fraction of equity unless suffixed `_pts`/`_inr`.

```python
@dataclass
class FnoRiskParams:
    equity_base: float = 1_000_000.0      # paper book size for the F&O sleeve

    # ── SPAN / capital budgeting ─────────────────────────────────────────
    max_total_span_pct: float   = 0.50    # Σ SPAN across ALL open positions ≤ 50% of equity
    max_span_per_trade_pct: float = 0.15  # one position's SPAN ≤ 15% of equity
    span_buffer_pct: float      = 0.20    # keep 20% of equity as un-blocked SPAN cushion
                                          # (VIX spikes inflate true SPAN intraday — §3.4 honesty)

    # ── Concurrent-risk caps ─────────────────────────────────────────────
    max_total_maxloss_pct: float = 0.06   # Σ defined max-loss across open positions ≤ 6% of equity
    max_open_positions: int      = 6      # hard cap on concurrent structures
    max_positions_per_index: int = 2      # diversify across indices, not stacked on one

    # ── Per-index allocation ──────────────────────────────────────────────
    # Fraction of the TOTAL span budget any single index may consume.
    # Defaults sum ≤ 1.0; absent indices' weight is unusable (no data → no trade).
    index_span_weight: dict[str, float] = {
        "NIFTY": 0.60, "BANKNIFTY": 0.25, "FINNIFTY": 0.10,
        "MIDCPNIFTY": 0.05, "SENSEX": 0.0, "BANKEX": 0.0,
    }

    # ── Loss limits (DB-derived, restart-proof) ──────────────────────────
    max_daily_loss_pct: float    = 0.02   # halt + flatten for the day
    max_weekly_loss_pct: float   = 0.05   # halt until next ISO week
    daily_dd_warn_pct: float     = 0.015  # soft warn → stand-aside new entries, keep exits

    # ── Live-eligibility gate ─────────────────────────────────────────────
    live_defined_risk_only: bool = True   # NEVER False outside a backtest
    live_risk_scale: float       = 0.10   # M8 training wheels; scales every fraction in live
    check_interval_seconds: int  = 10

    # ── Kill-switch / halt persistence (mirror engine/risk.py) ───────────
    killswitch_file: Path | None = None   # api/dashboard trips this
    halt_file: Path | None       = None   # loss halts persist here
    resume_file: Path | None     = None   # api drops this to clear a halt
```

Derived budgets (all properties, recomputed from live equity each cycle):

```
total_span_budget   = equity * max_total_span_pct
per_trade_span_cap  = equity * max_span_per_trade_pct
total_maxloss_budget= equity * max_total_maxloss_pct
daily_loss_budget   = equity * max_daily_loss_pct
weekly_loss_budget  = equity * max_weekly_loss_pct
index_span_cap(idx) = total_span_budget * index_span_weight[idx]   # 0 if no weight → no data → no trade
```

In live mode every `*_budget` is additionally multiplied by `live_risk_scale` (so an M8 tiny-live book risks
10% of the geometry paper rehearses), exactly as the equity engine scales fractions.

---

## 3. Pre-trade admission (`check_candidate`)

The router proposes, per cycle and per index, ONE candidate: `(index, strategy_name, legs, entry_net, span,
max_loss, gate_decision)`. The risk layer admits or rejects it. Order of checks — **first failure rejects**,
and the cheapest/most-fatal checks run first:

```
def check_candidate(cand) -> (ok: bool, reason: str):

  # (0) EXITS / ROLLS — never blocked. A roll = close existing legs + open new;
  #     the close half is always admitted. Open half is a fresh candidate (re-enters here).
  if cand.is_exit_or_close: return True, "OK — exit/roll never blocked"

  # (1) Halt / kill-switch
  if state.kill_switch or state.halted:
      return False, f"halted: {state.halt_reason or 'kill switch'}"

  # (2) LIVE-ELIGIBILITY — the defined-risk firewall (§4)
  if live and live_defined_risk_only:
      spec = FNO_STRATEGIES[cand.strategy]
      if not (spec.defined_risk and spec.span_model == "defined"):
          return False, f"{cand.strategy}: undefined-risk — EXCLUDED from live"

  # (3) Gate alignment — only trade cycles the vol-gate favours for this strategy
  want = SELL_PREMIUM if spec.sell_premium else BUY_PREMIUM
  if cand.gate_decision != want:        # STAND_ASIDE or wrong side
      return False, f"gate={cand.gate_decision} != want={want} — stand aside"

  # (4) Soft daily drawdown — past the warn line, no NEW risk (exits still flow)
  if day_total <= -daily_dd_warn_pct*equity:
      return False, "daily soft-drawdown reached — new entries paused"

  # (5) Position-count caps
  if open_count() >= max_open_positions:        return False, "max open positions"
  if open_count_for(cand.index) >= max_positions_per_index:
      return False, f"max positions for {cand.index}"

  # (6) Per-trade SPAN cap
  if cand.span > per_trade_span_cap + 1e-6:
      return False, f"SPAN ₹{cand.span:,.0f} > per-trade cap"

  # (7) Total SPAN budget (with buffer): committed + new ≤ (1 - span_buffer_pct)·equity·max_total_span_pct
  if committed_span + cand.span > total_span_budget*(1-span_buffer_pct) + 1e-6:
      return False, "total SPAN budget exhausted"

  # (8) Per-index SPAN allocation
  if committed_span_for(cand.index) + cand.span > index_span_cap(cand.index) + 1e-6:
      return False, f"{cand.index} SPAN allocation exhausted"

  # (9) Concurrent MAX-LOSS budget (the true risk cap — defines daily-loss headroom UP FRONT)
  #     committed defined max-loss + this trade's max-loss + today's realized losses
  #     must leave room within the daily loss budget BEFORE the loss can occur.
  consumed = committed_maxloss + max(0, -realized_today)
  if consumed + cand.max_loss > daily_loss_budget + 1e-6:
      return False, "daily risk budget exhausted (committed max-loss + realized losses + new)"

  return True, "OK"
```

Check (9) is the F&O analogue of the equity engine's "enforce the daily limit BEFORE the loss exists":
because every live structure is defined-risk, the sum of open max-losses is a hard upper bound on how much
*more* the book can lose today. The daily limit is therefore enforced at entry, not 10s after via the monitor.

### 3.1 Sizing (lots)

Lot count per candidate is the largest integer satisfying every per-position cap simultaneously:

```
qty_lots = min(
    floor(per_trade_span_cap        / span_per_lot),
    floor(remaining_total_span       / span_per_lot),
    floor(index_span_cap_remaining   / span_per_lot),
    floor(remaining_maxloss_budget   / maxloss_per_lot),
)
qty_lots = max(0, qty_lots)        # 0 ⇒ reject (no room)
```

`span_per_lot` / `maxloss_per_lot` come straight from `span_margin(...)` / `_span_defined(...)` for a 1-lot
build. Liquidity cap (the options analogue of ADV participation) is OOS for Phase-0 (NIFTY weeklies are deep);
when illiquid indices are ingested, add `qty_lots ≤ open_interest_participation_pct · ATM_OI`.

---

## 4. Undefined-risk EXCLUSION (the firewall)

`fno_strategies.py` already labels these. We enforce the live/backtest split on top:

| span_model | defined_risk | Live / Paper | Diagnostic backtest |
|---|---|---|---|
| `defined` | True | **ELIGIBLE** | yes |
| `naked_short` | False | **EXCLUDED** | yes — EXPIRY-ONLY / tail-blind label only |
| `spread_naked_mix` | False | **EXCLUDED** | yes — EXPIRY-ONLY / tail-blind label only |

Excluded strategies: `short_straddle`, `short_strangle`, `jade_lizard`, `ratio_spread` (= the engine's
`_UNDEFINED_RISK_STRATEGIES`), plus any future `defined_risk=False`.

Rules:
1. **Live/paper:** check (2) hard-rejects any candidate whose spec is not `defined_risk and span_model=="defined"`.
   `live_defined_risk_only` is set `True` and is NEVER flipped outside an offline backtest harness.
2. **Diagnostic backtest:** undefined-risk strategies MAY be run for research, but the orchestrator must
   propagate the engine's `_UNDEFINED_RISK_DISCLAIMER` ("UNDEFINED-RISK, EXPIRY-ONLY backtest — tail-blind
   … treat any GO as diagnostic only") into every report row and refuse to promote such a GO to the live
   strategy set. A tail-blind GO is **never** a live signal.
3. The `span_pct=0.12` naked SPAN proxy is *non-conservative in high-VIX regimes* (engine honesty ledger §4).
   Because undefined-risk is live-excluded anyway, this only ever biases the diagnostic ROM — flag it, don't
   trust it for capital.

---

## 5. Loss limits & the monitoring loop

DB-derived, restart-proof — identical structure to `engine/risk.py` `refresh_pnl` / `_evaluate`, sourced from
the F&O trades ledger (paper fills) keyed to IST trading day/week (`Asia/Kolkata`, not UTC `CURRENT_DATE`).

```
realized_today, realized_week, realized_total  ← SUM(net_pnl) over the F&O paper ledger (IST day/week filters)
unrealized                                     ← mark open structures to the real option chain when available;
                                                  in expiry-only backtest, unrealized = 0 (path-blind — stated)
day_total  = realized_today + unrealized
week_total = realized_week  + unrealized
```

Every `check_interval_seconds`:

```
1. Consume resume_file (clear halt) — but DO NOT early-return; loss checks re-run this tick, so resume
   cannot push past a still-breached budget (mirror engine/risk.py).
2. Consume killswitch_file → activate_kill_switch (fires flatten callbacks).
3. refresh_pnl() from DB.
4. if day_total  < -daily_loss_budget   → HALT scope="day"
   if week_total < -weekly_loss_budget  → HALT scope="week"
5. snapshot equity (best-effort).
```

**Halt scopes** (persisted to `halt_file`, re-entered on boot if still in scope — `load_persisted_halt`):
- `day` — cleared automatically next IST trading day OR by deliberate resume.
- `week` — cleared next ISO week OR by deliberate resume.
- `""` (kill-switch) — cleared only by deliberate resume.

**Soft drawdown (`daily_dd_warn_pct`, default 1.5%):** not a halt — `check_candidate` step (4) pauses NEW
entries while exits keep flowing. Gives a graceful glide path before the 2% hard halt.

---

## 6. Kill-switch (RiskEngine-owned)

Exactly the equity discipline — the F&O orchestrator must NOT hand-roll a second kill path:

- `run/fno_killswitch` file (written by POST `/api/fno/killswitch`, dashboard-token guarded) → monitor
  detects → `activate_kill_switch()` → fires `on_halt` callbacks.
- **`on_halt` flattens every open structure** by submitting close legs (which always pass check (0)).
  Closing an iron condor = buy back the two shorts + sell the two longs as a single intent set.
- Kill-switch state is set **eagerly** so `check_candidate` rejects immediately, before the async flatten task
  runs (mirror `activate_kill_switch` on both event-loop and sync paths).
- Resume is deliberate: `run/fno_resume` (POST `/api/fno/resume`) — and only sticks if the underlying loss
  budget is no longer breached.
- **One owner.** The router, scheduler, and per-index workers all read the same `FnoRiskEngine.state`; none
  may place an order without `check_candidate` clearing it.

---

## 7. Concrete defaults summary (₹1,000,000 paper book)

| Limit | Param | Value | ₹ at base equity |
|---|---|---|---|
| Total SPAN | `max_total_span_pct` | 50% | ₹500,000 |
| SPAN buffer (un-blocked) | `span_buffer_pct` | 20% | usable SPAN ≈ ₹400,000 |
| Per-trade SPAN | `max_span_per_trade_pct` | 15% | ₹150,000 |
| Total concurrent max-loss | `max_total_maxloss_pct` | 6% | ₹60,000 |
| Daily loss halt | `max_daily_loss_pct` | 2% | ₹20,000 |
| Daily soft-drawdown | `daily_dd_warn_pct` | 1.5% | ₹15,000 |
| Weekly loss halt | `max_weekly_loss_pct` | 5% | ₹50,000 |
| Max concurrent positions | `max_open_positions` | 6 | — |
| Max per index | `max_positions_per_index` | 2 | — |
| NIFTY SPAN allocation | `index_span_weight["NIFTY"]` | 60% of total | ₹300,000 |

Tune after the NIFTY backtest produces a real ROM/drawdown distribution. The 6% concurrent-max-loss budget
caps the *whole book's* worst-case single-day expiry loss at 3× the daily halt — deliberately tight while the
edge rests on the VIX-as-weekly-IV proxy and close-not-FSP settlement (preliminary).

---

## 8. Index-agnostic notes

- `index_span_weight` is the per-index allocation registry — a key with weight 0 (or absent) is structurally
  un-tradable, which matches reality: BANKNIFTY/FINNIFTY/etc. have **no data** until ingested, so they never
  produce a candidate regardless of weight.
- Lot size, strike step, and SPAN `span_pct` are per-index (NIFTY lot from `fno_costs.NIFTY_LOT`); the risk
  layer reads them from the same per-index registry the router uses — it does not hard-code NIFTY.
- ROM and all budgets are index-normalised by construction (SPAN ₹ and max-loss ₹ are absolute), so adding an
  index needs only its registry row + data, no risk-layer code change.

---

## 9. Open questions / honesty ledger

1. **True SPAN** is approximated (`defined` = max-loss; `naked` = 12% notional). Defined-risk live-eligibility
   makes the approximation *conservative for live* (broker blocks ≥ max-loss). Replace with the NSE daily SPAN
   parameter file before any real-money consideration.
2. **Unrealized marking** needs the real option chain; expiry-only backtest is path-blind, so the monitor's
   intra-cycle halt cannot fire in pure backtest — only realized-at-expiry losses trip it. Forward paper-log
   with live chain quotes is the truth test (per `_CONTEXT.md` §2).
3. **Correlation across indices** is ignored — `max_total_maxloss_pct` treats positions as independent, but
   NIFTY/BANKNIFTY co-move. Until multi-index data exists this is moot; revisit with a correlation haircut on
   the total-max-loss budget when indices come online.
4. PAPER stays true. None of these defaults unlock live; live additionally requires the M8 path + the
   `defined_risk_only` firewall verified.
