# 08 — Options Scalper: Forward Paper-Trade Harness (validation path)

**Status:** PLAN — research only, PAPER. No live order paths. One Dhan session.
**Branch (this spec's impl):** `feat/fno-options-strategies` (strategy logic) → `feat/scalper-forward-paper` (harness)
**Depends on (separate, trusted-machine PR):** intraday option-LTP subscription (`§2`).
**Audience:** the engineer wiring `strategies/options_scalper.py` to a live feed for forward validation.

---

## 0. Why forward paper-trade (not backtest)

`strategies/options_scalper.py` is a pure, synchronous `on_tick → ScalpDecision` engine. Its
exit logic (TP ladder / trailing / hard stop / time-stop) is driven by **`option_premium`** — the
LTP of the *held* ATM CE/PE. As `docs/fno_strategies/options_scalper.md §5` states verbatim:

> the DB has **NO intraday option premiums** and (today) **NO intraday NIFTY index 1-min bars**.
> A faithful historical backtest of this intraday scalper is *blocked on data that does not exist*.

We **must not** synthesise an intraday premium path from daily OHLCV (Black-76 over interpolated
or Brownian-bridged daily bars would manufacture the very premium moves the strategy trades — that
is fabrication, not a backtest). The only honest near-term validation is:

> **Run the unmodified `OptionsScalper` engine forward, in real time, on the LIVE feed.** Underlying
> NIFTY spot (1-min / tick) drives the signal + entry rungs; the **live LTP of the held ATM
> contract** drives every premium-based exit. Fills are simulated at the live LTP ± a modelled
> spread; the full cost stack is charged on every round-trip; every scalp + daily P&L is logged.

This uses *real* premiums and *real* spreads — exactly where this strategy's edge (or lack of it)
is decided. It is PAPER throughout: no order is ever placed, no live order path is touched, and
the platform `RiskEngine` / Dhan IP whitelist are never involved.

---

## 1. Scope split (two PRs, do not conflate)

| PR | Lane | Contents | Touches live order path? |
|---|---|---|---|
| **A — data subscription** (`§2`) | trusted machine | Intraday ATM CE/PE LTP source: resolve the ATM security_ids each minute as spot moves, subscribe them on the live WS feed alongside NIFTY spot, expose `get_option_ltp(strike, opt_type)`. | No (data only) |
| **B — forward-paper harness** (`§3`–`§6`) | trusted machine | Async loop that drives `OptionsScalper.on_tick`, simulates fills at live LTP ± spread, applies the daily governor, persists every scalp + daily P&L. | No (paper only) |

PR A is the prerequisite. The collector (`core/fno_collector.py`) is **EOD-only** — it does not
stream or poll intraday option LTPs. PR A is the new intraday source; it is *separate from the
strategy logic* and from this harness's accounting.

> One Dhan session constraint: the live `dhan-trader` already holds the single WS session on the
> agent. **The forward-paper harness must NOT open a second concurrent Dhan WS session.** Two
> options (decide in PR A): (a) run the harness **inside the existing `dhan-trader` process**,
> adding the ATM CE/PE + IDX_I `13` to the *existing* `LiveFeed.subscribe(...)` call; or (b) run it
> as a standalone process **only in a window where `dhan-trader` is stopped** (e.g. a dedicated
> research session). **(a) is preferred** — it reuses the one session, the proven `LiveFeed`
> reconnect/backoff, and `BarBuilder` as the single tick→1-min aggregator.

---

## 2. PR A — the intraday option-LTP subscription (data prerequisite)

### 2a. What the strategy needs from the feed
- **Underlying NIFTY 50 spot** — `IDX_I` security_id `"13"`, 1-min closed bars (close → `on_tick`'s
  `underlying_price`; bar high/low → `high`/`low` for the ATR filter). VWAP, momentum, ATR, ORB are
  all computed by the strategy from this single underlying stream — **no option chain needed for the
  signal.**
- **LTP of the currently-held ATM contract** — the ATM CE *or* ATM PE security_id, depending on the
  open ladder's `option_type` and `strike`. Needed every tick a position is open, to drive
  TP/stop/trail/time-stop. When flat, only the next-to-be-entered ATM strike's LTP is needed (for
  the entry fill price).

### 2b. ATM contract resolution (the moving target)
Spot moves across the 50-pt grid intraday, so "the ATM contract" changes. The harness fixes the
**strike at the first fill of a ladder** (`ScalpDecision.strike`, per spec §1d — you ladder the
*same* contract, never re-strike mid-ladder), but to *price an entry* and to *seed the next ladder*
it must know the live ATM CE/PE for the **current** spot.

- Pick the **nearest weekly NIFTY expiry** (the scalp is closed by EOD, so always use the nearest
  expiry for max liquidity / tightest spread; never the monthly unless it *is* the nearest).
- Map `(expiry, strike, CE|PE) → security_id` via the FNO instruments master
  (`core/fno_instruments` / `fno_instruments` table; see `core/fno_backfill.SYMBOL_SCRIP`). Build a
  small in-memory dict for the day: ATM ± a few strikes (e.g. ±3 × 50 = 7 CE + 7 PE) so a 150-pt
  intraday swing stays covered without re-querying the master.
- **Subscribe the whole ATM window** (those ~14 option security_ids) on `LiveFeed` at session start
  using the existing `subscribe({"NSE_FNO": [...], "IDX_I": [13]})` API. `SEG_TO_EX` already maps
  `NSE_FNO → MarketFeed.NSE_FNO`. **SecurityIds MUST be strings** (the live-feed code stringifies,
  but pass strings to be safe — ints silently never stream, per `CLAUDE.md`).
- Expose a helper on the feed adapter:
  ```python
  def get_option_ltp(strike: int, opt_type: str) -> Optional[float]:
      sid = atm_window_sids.get((strike, opt_type))   # built at session start
      return live_feed.get_ltp(sid) if sid else None   # 0.0/None ⇒ no live quote
  ```
  Return `None` (not `0.0`) when there is no fresh quote, so the strategy's stale-premium
  fail-safe (§6.6) engages instead of fabricating a stop.

### 2c. Freshness / staleness
- Use `LiveFeed.get_tick_age_s(sid)`. If the held contract's LTP is older than a small threshold
  (e.g. **5 s**), pass `option_premium=None` to `on_tick` → strategy suppresses premium-based exits
  (fail-safe) rather than acting on a stale quote. Session/EOD rules still fire.
- Far-OTM/ATM weeklies are liquid near NIFTY; staleness should be rare, but spreads widen near
  lunch — the spread model (§4b) and slippage handle the cost, the staleness gate handles the
  *gap*.

### 2d. Rate / session budget
- WS feed has no per-call rate limit (it streams). Adding ~15 instruments to the existing
  subscription is negligible. The one-time instruments-master lookup (PR A startup) uses a few REST
  calls — well within the 100K/day budget; do it once at session open, cache for the day.

---

## 3. PR B — the forward-paper harness (`research/scalper/forward_paper.py`)

A thin async driver. **All trading logic stays in `OptionsScalper`** — the harness only feeds it
ticks, simulates fills, charges costs, applies the governor, and logs.

### 3a. Tick loop (mirrors `engine/runner.py` cadence)
```
on each closed 1-min NIFTY bar (from BarBuilder.get_current("13") / last_closed):
    now        = IST timestamp of the bar
    underlying = bar.close
    high, low  = bar.high, bar.low

    # premium of the *currently held* contract (None if flat or stale)
    prem = None
    if scalper._position_lots() > 0:
        prem = option_ltp(ladder_strike, ladder_option_type)   # via §2b helper, staleness-gated
    elif <about to evaluate a new entry>:
        # for entry fill pricing we read the LTP of the ATM strike the signal would pick;
        # but on_tick decides the strike, so we price the fill AFTER the ENTER decision (§4a)
        prem = None

    decision = scalper.on_tick(now, underlying, option_premium=prem, high=high, low=low)
    if decision is not None:
        fill_premium = simulate_fill(decision, ...)      # §4
        scalper.notify_fill(decision.side, decision.lots, fill_premium, now)
        log_scalp(decision, fill_premium, now)           # §5
```

- Drive on **closed 1-min bars** (not every raw tick): the signal (VWAP/EMA/ATR/momentum) is bar-
  native and the scalper's window is 1-min bars. This also bounds the loop to ~1 evaluation/min/
  instrument, trivially cheap. (Premium-based exits on closed bars only means a stop can be up to
  ~1 min late — this is *conservative* for a paper track and matches the strategy's 1-min design.
  Note this assumption in the report.)
- Future-skew, session-reset, warm-up, trade-window, and unconditional EOD square-off are all
  enforced *inside* `on_tick` — the harness does not re-implement them.

### 3b. Fill order / pending-entry
The strategy sets `_pending_entry=True` after emitting `ENTER` and blocks further rungs until
`notify_fill` clears it. So the harness **must call `notify_fill` synchronously after each
decision** (paper fills are instant — there is no broker round-trip), exactly like the unit-test
pattern. EXIT decisions likewise feed `notify_fill(side="SELL", ...)` so the tranche book + realized
P&L + cooldown advance.

---

## 4. Fill simulation (the crux of an honest paper track)

A scalper lives or dies on per-round-trip cost + spread. Simulate **pessimistically**.

### 4a. Fill price = live LTP ± modelled half-spread (adverse)
For every leg, start from the **live LTP** of the relevant ATM contract and cross the spread
against us:
```
BUY (entry):  fill = ltp + half_spread + slippage(ltp, pct)
SELL (exit):  fill = ltp - half_spread - slippage(ltp, pct)
```
- **Entry pricing detail:** `on_tick` returns the `ENTER` decision *with* the chosen
  `option_type`/`strike`. Read that contract's live LTP via `get_option_ltp(decision.strike,
  decision.option_type)` *after* the decision, apply the adverse BUY adjustment, then call
  `notify_fill`. (The strategy records `entry_underlying` from `_last_rung_underlying`, set on the
  prior tick — fine.)
- If the entry contract has **no fresh LTP**, **skip the fill** (do not invent a premium): call
  `scalper.notify_flat()`-equivalent for that pending entry by simply *not* calling `notify_fill`
  and resetting `_pending_entry` — or better, drop the ENTER (log a "no-fill: stale entry quote").
  The strategy's `notify_fill` already refuses `premium <= 0` BUYs.

### 4b. Spread model
We do not have a reliable live bid/ask in the Quote packet by default, so model the half-spread as
a fraction of premium, **conservative for small-target scalping**:
- `half_spread = max(SPREAD_TICK/2, premium * SPREAD_PCT)` where `SPREAD_TICK = 0.05` (₹0.05 NSE
  option tick) and `SPREAD_PCT` default **0.005** (0.5% each side ⇒ ~1% round-trip), tunable up to
  1%+ for the pre-close/lunch illiquidity. If PR A *does* surface true bid/ask from a fuller packet,
  prefer the **real** half-spread `(ask-bid)/2` and keep the modelled value only as a floor.
- Plus `slippage(premium, pct=SLIP_PCT)` from `research/backtest/fno_costs.slippage`, default
  **0.005** but for scalping start at **0.01** (you cross the book both ways on small targets).

### 4c. Cost stack — charge on EVERY round-trip
Reuse `research/backtest/fno_costs.condor_costs` (the name is historical; it costs any leg list).
Each scalp round-trip = a BUY leg + a SELL leg:
```python
from research.backtest.fno_costs import condor_costs, NIFTY_LOT
legs = [(entry_fill, lots*NIFTY_LOT, "BUY"), (exit_fill, lots*NIFTY_LOT, "SELL")]
rt_costs = condor_costs(legs).total     # ₹40 brokerage (2 orders) + sell-STT 0.15% + exch + SEBI + stamp + GST
```
- This is **per tranche round-trip** — a 3-rung ladder taken out at 3 TP levels is **3 round-trips**
  = ₹120 brokerage alone, before STT/fees. Account each leg as the strategy actually fires it
  (entry rung → its eventual matching exit), FIFO, matching the strategy's `notify_fill` FIFO close.
- **No exercise STT** (intraday, always closed in the market before EOD; `exercise_intrinsic=0`).
- Net per-scalp P&L: `(exit_fill - entry_fill) * lots * NIFTY_LOT - rt_costs`.

### 4d. Feed the strategy's daily governor *net* numbers
The strategy's internal `_daily_realized_pnl` is computed **gross** (`(exit-entry)*lots*lot` in
`_update_realized_pnl`) and trips `daily_loss_cap` on gross. For the harness's authoritative
accounting, track a **net** daily P&L (gross − costs) in the harness and let the **harness governor**
(§6) be the binding one. Keep the strategy's internal cap as a secondary backstop. Report both, but
the **net** figure is the decision input.

---

## 5. Logging — `scalper_paper_trades` (intraday analogue of `fno_paper_trades`)

Persist every scalp and a daily roll-up. New Alembic migration (next head after `011`), table
`scalper_paper_trades`. Mirror the conventions of `core/fno_paper.py` (lazy DB import, pure math
testable, `raw` jsonb context).

### 5a. Per-scalp row (one per closed round-trip = one tranche entry→exit)
| column | type | source |
|---|---|---|
| `id` | bigserial PK | |
| `session_date` | date | `now.date()` |
| `ladder_id` | int | harness counter per ladder (groups rungs) |
| `rung` | int | rung index within the ladder |
| `signal` | text | `params.signal` (vwap_mom/ema/orb/momentum) |
| `direction` | text | LONG/SHORT |
| `option_type` | text | CE/PE |
| `strike` | int | `decision.strike` |
| `lots` | int | tranche lots |
| `entry_time` / `exit_time` | timestamptz | IST |
| `entry_underlying` / `exit_underlying` | numeric | NIFTY spot at each |
| `entry_ltp` / `exit_ltp` | numeric | raw live LTP (pre-adjust) |
| `entry_fill` / `exit_fill` | numeric | after spread+slippage (§4a) |
| `entry_reason` / `exit_reason` | text | `decision.reason` (TP[i]/trail/stop/time-stop/flip/EOD) |
| `gross_pnl` | numeric | `(exit_fill-entry_fill)*lots*NIFTY_LOT` |
| `costs` | numeric | `condor_costs(...).total` for the round-trip |
| `net_pnl` | numeric | `gross_pnl - costs` |
| `hold_min` | numeric | minutes held |
| `raw` | jsonb | spreads/slippage used, staleness flags, VWAP/ATR at entry |

### 5b. Per-session row (one per trading day) — `scalper_paper_daily`
`session_date` (PK), `n_ladders`, `n_scalps`, `gross_pnl`, `total_costs`, `net_pnl`,
`win_scalps`, `loss_scalps`, `max_concurrent_lots`, `daily_loss_cap_hit` (bool),
`squareoffs` (count), `params_hash` (so a parameter change is visible in the track), `raw` jsonb.

### 5c. Summary helper (mirror `paper_summary`)
`scalper_summary()` → aggregate over `scalper_paper_trades`: `n_sessions`, `n_scalps`,
`win_rate`, `avg_net_per_scalp`, `total_net_pnl`, `breakeven_target_pct` (the premium-move % a scalp
must clear to net zero at the assumed cost+spread — surface this **prominently**, per spec §5d),
`expectancy_per_scalp`, `net_per_session` mean/median, `max_drawdown` over the session-P&L curve.

---

## 6. Daily governor (harness-level, binding)

The strategy enforces per-day caps internally; the harness adds the *authoritative net* governor
and an operational kill, all **PAPER**:
1. **Net daily-loss stand-down** — when harness net daily P&L ≤ `-daily_loss_cap` (₹8000 default),
   stop feeding new entries for the session (emit no further ENTER; still let `on_tick` manage/exit
   open tranches and fire EOD square-off). This is the net analogue of the strategy's internal gross
   cap (§4d).
2. **Max ladders/day** — already in the strategy (`max_trades=8`); the harness just records it.
3. **Unconditional EOD square-off** — enforced inside `on_tick`; the harness must keep feeding ticks
   through `MARKET_CLOSE - squareoff_before_close_min` so the flatten EXIT is emitted and filled.
4. **No second Dhan session** (§1) and **PAPER_TRADING stays `true`** — the harness never imports an
   executor or touches `engine/execution.py`; it only calls `OptionsScalper.notify_*`.
5. **Fail-open** — any harness/feed error (stale quote, missing sid, DB write fail) logs and
   continues; it must never throw into the live `dhan-trader` event loop (if running embedded).

---

## 7. Validation decision criteria (the go/no-go)

The point of the track is a single honest answer: **is this scalper positive-EV net of real
costs and spread, with enough sample to trust it?** Decide ONLY on the **net** numbers.

### 7a. Minimum sample before any verdict
- **≥ 30 trading sessions** AND **≥ 200 net scalps** (round-trips). A scalper generates many
  trades/day, so the scalp count accrues fast; the *session* count guards against one lucky regime.
  Below this, status is **INSUFFICIENT DATA — keep running** (no decision).
- Span ≥ ~6 calendar weeks so multiple weekly-expiry cycles and at least one event week (RBI /
  budget / expiry-day) are represented. Tag event days in `raw` so they can be excluded/inspected.

### 7b. PROMOTE-to-next-stage criteria (all must hold, on net numbers)
1. **Positive expectancy per scalp:** `avg_net_per_scalp > 0` with a one-sided bootstrap/t 95% CI
   lower bound **> 0** (not merely a positive point estimate — small edges drown in cost noise).
2. **Clears the cost hurdle with margin:** `avg_net_per_scalp ≥ k × avg_round_trip_cost` for a
   margin `k` (start `k = 0.5`) — i.e. the average winning move must beat the full cost stack, not
   tie it. Report `breakeven_target_pct` vs the strategy's TP[0] (10%): **if breakeven ≥ TP[0], the
   first TP rung is structurally unprofitable → NO-GO regardless of a lucky run.**
3. **Profitable at the session level:** `net_per_session` median **> 0** and **≥ 55%** of sessions
   net-positive (a scalper that wins on a few outlier days and bleeds the rest is fragile).
4. **Risk-adjusted:** session-P&L **Sharpe-equivalent ≥ 1.0** (annualised over sessions) AND
   **max session-curve drawdown ≤ ~3 × daily_loss_cap** (no single bad streak threatens the bankroll
   the cap is meant to protect).
5. **Cost share sane:** `total_costs / |total_gross_pnl|` is reported; if costs eat **> ~60%** of
   gross even when net is positive, flag fragility (a small spread/cost mis-estimate flips it) —
   treat as **CONDITIONAL**, widen the spread/slippage assumption and re-evaluate before promoting.

### 7c. NO-GO / stop criteria
- `avg_net_per_scalp ≤ 0` (CI lower bound ≤ 0) after the minimum sample → **NO-GO**; the strategy
  does not beat costs on real spreads. Archive the track; do not tune-to-fit on the same sample.
- `breakeven_target_pct ≥ tp_ladder_pct[0]` → first scalp rung unprofitable → **NO-GO** (or redesign
  targets, then start a *fresh* track).
- Daily-loss cap hit on **> 20%** of sessions → the signal/governor combo is too loss-prone →
  **redesign**, don't promote.

### 7d. Honesty guards (state in every report)
- Fills are at **live LTP ± modelled spread**, not at observed bid/ask (unless PR A surfaces real
  bid/ask). State the `SPREAD_PCT` / `SLIP_PCT` used; show a **sensitivity row** at the next-worse
  cost assumption (e.g. 1% half-spread, 1% slippage) — if the verdict flips under modest worsening,
  it is **CONDITIONAL**, not PROMOTE.
- Stops/exits act on **closed 1-min bars** (≤ ~1 min latency) — conservative, but note it.
- This is forward PAPER on a *finite future window* — it cannot prove a past edge and cannot be
  re-run; protect the sample from over-fitting by **freezing `ScalperParams` for the whole track**
  (a `params_hash` change resets the sample count).
- The go-decision here only authorises **the next research stage** (e.g. a tiny-size live pilot
  under the real `RiskEngine`), never an automatic flip to live; `PAPER_TRADING=true` stays the
  default and live still requires the deliberate `.env` + `ALLOW_LIVE_TOGGLE` + restart steps.

---

## 8. Build order (for the coder)

1. **PR A** — intraday ATM CE/PE subscription + `get_option_ltp` (trusted machine; reuse `LiveFeed`,
   `core/fno_instruments`, the existing single WS session). Unit-test the ATM-window resolution with
   a fake instruments master + fake feed.
2. **PR B** — `research/scalper/forward_paper.py`: the tick loop (§3), fill simulator (§4), governor
   (§6). Unit-test fill simulation + cost accounting + governor with synthetic LTP sequences (no DB,
   no network) — same testability contract as `options_scalper.py` and `fno_paper.py`.
3. **Migration** (next head after `011`) for `scalper_paper_trades` + `scalper_paper_daily`; logging
   + `scalper_summary` (§5), DB-imports lazy, pure math unit-tested.
4. **Reuse, don't re-derive:** `condor_costs`, `slippage`, `NIFTY_LOT`, `LiveFeed`, `BarBuilder`,
   `core/fno_instruments`, `OptionsScalper`. Add **no** new live-order code.
5. Run forward (embedded in `dhan-trader` or a dedicated stopped-trader window), let the track accrue
   to the §7a minimum, then produce the go/no-go report against §7b–§7d.
