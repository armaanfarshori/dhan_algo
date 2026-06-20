# 09 — Adversarial Risk / Edge-Case Review (Options Scalper)

**Status:** REVIEW — research only, PAPER. Pessimist's pass over `strategies/options_scalper.py`
+ `docs/fno_strategies/options_scalper.md`.
**Premise:** the scalper *claims* "bulletproof." It is not. This document enumerates every way it
blows up and the **concrete guard** the hardened version must carry. Read it as a checklist the
implementation must satisfy before any forward-paper run — and certainly before any live wiring.

The strategy class today is a **pure synchronous signal/ladder engine** (ticks in, decisions out).
That is exactly why most of the blow-ups below are **NOT visible in unit tests** — they live in the
seam between the engine, the LTP feed, the executor, the broker, and the process lifecycle. Several
guards therefore belong in the **wiring layer** (runner / executor / RiskEngine), not only in the
class. Each item flags WHERE the guard lives.

Legend for "where": **[ENGINE]** in `options_scalper.py` · **[FEED]** intraday ATM LTP source
(§5b PR, not built) · **[EXEC]** executor/runner · **[RISK]** platform `RiskEngine` ·
**[FEAS]** backtest harness only.

---

## A. The core asymmetry you cannot design away

A long single option is a **bounded-upside, time-decaying, gap-exposed, spread-taxed** instrument
traded for *small* targets. Every edge case below is a variation on the same theme:

- **Premium can move much faster than the underlying** (gamma + IV). A `stop_pct = 0.20` premium
  stop is **not** a 0.20 underlying stop — a 1% adverse underlying move on a high-gamma ATM near
  expiry can blow through −20% premium in one tick.
- **The premium path is the only thing the exits see**, and that path is exactly the data we do
  **not** have intraday (§5 of the spec). Every TP/stop/trail is only as good as the LTP feed.
- **Costs are paid on every round-trip** and the targets are tiny. A guard that prevents one
  bad day is worth more than the entire month's scalp edge.

If any guard below is unimplemented, the honest status is **"not bulletproof — known blow-up
open."**

---

## B. Blow-up scenarios → required guards

### B1. Overnight / open gap (you are flat, but the *next* entry is into a gap)
**Scenario.** The engine is flat by EOD (good). But on the next session, a gap-open (global cue,
RBI, war headline) means VWAP/momentum warm-up runs on a violently repricing tape; the first
qualifying signal buys an option whose IV has already exploded and will crush as the day calms.
You buy the top of the IV spike.
**Why current design fails.** Warm-up is purely time/bar-count based (`warmup_minutes=15`,
`bars_seen >= required`). It has **no gap-day awareness** and **no IV-level awareness**.
**Guards.**
- **[ENGINE]** Gap-day stand-down knob: if `|open − prev_close| / prev_close > gap_pct`
  (e.g. 1.5%), require an **extended warm-up** (e.g. 30 min) and/or **skip the first ladder** of
  the day. Default conservative.
- **[ENGINE/FEED]** **IV-spike entry filter:** block new ladders when India VIX (id `21`) is above
  a session threshold or has jumped > X% vs prev close — long premium bought into a VIX spike is
  buying expensive theta that will crush. Spec already ingests VIX daily; surface it to the engine.
- **[ENGINE]** Already partially covered: trade window blocks the first `no_trade_open_min` — keep
  it, but it is **not** sufficient on a gap day.

### B2. Underlying / option circuit limits & price freeze (cannot exit at all)
**Scenario.** Sharp move trips an underlying circuit or the **option hits its own circuit band**;
LTP is pinned and **no counterparty** is reachable at your price. Your stop "fires" in the engine
but the SELL **cannot fill**. You are trapped long into the move.
**Why current design fails.** Engine emits an EXIT and assumes it executes. There is **no circuit
awareness** anywhere in the class. The scrip master *does* carry `upper_circuit`/`lower_circuit`
per instrument (`core/fno_instruments.py`) — the engine ignores it.
**Guards.**
- **[EXEC]** Before/while sending the SELL, read the option's `upper_circuit`/`lower_circuit` and
  current LTP. If pinned at the **lower** band (for a long you are trying to sell into), mark the
  exit **unfilled-but-attempted**, alert CRITICAL, and **keep retrying** — never silently drop it.
- **[ENGINE]** Treat an LTP frozen at the circuit band as **stale** for TP purposes (don't book a
  TP off a one-sided frozen print).
- **[RISK]** A circuit-trapped position is a kill-switch-grade event: notify, do not pretend flat.

### B3. Liquidity hole — no fill / monstrous spread (the scalper's natural habitat)
**Scenario.** Lunch lull, far strike after spot drifted, or just a thin minute. Quoted spread is
₹4 on a ₹120 premium (3.3% one way). Your +10% target nets *negative* after crossing twice. Or
the SELL simply doesn't fill at the modeled price and you chase it down.
**Why current design fails.** Engine has **no spread input at all** — it sees a single
`option_premium` (LTP), treats it as executable, and books TP/stop off it. LTP ≠ tradeable bid for
a sell. The spec's §5d warns costs dominate but the **engine enforces nothing**.
**Guards.**
- **[FEED]** Feed **bid AND ask** (not just LTP). Exits price off the **bid** (you sell into bid),
  entries off the **ask**. Booking TP off mid/LTP is a lie the backtest must not tell either.
- **[ENGINE]** **Max-spread entry gate:** block a new ladder/add when
  `(ask − bid)/mid > max_spread_pct` (e.g. 1.5–2%). A scalper must never open into a spread wider
  than its first TP rung — that is structurally unwinnable.
- **[ENGINE]** **Spread-aware target sanity:** if `tp_ladder_pct[0]` (10%) < expected round-trip
  cost+spread, refuse the trade (or widen targets). The "break-even target %" from §5d should be a
  **runtime gate**, not just a report line.
- **[EXEC]** Use **limit-with-reprice / IOC** logic, not blind market sells, with a max chase; on
  failure, escalate (don't keep firing identical rejects).

### B4. Fast adverse move straight through the stop (gap-through, no fill at stop)
**Scenario.** Premium goes ₹120 → ₹118 → ₹70 in two ticks (underlying flushes, gamma + IV down for
a long CE on a drop). Your −20% stop at ₹96 is **gapped through**; the realized loss is −42%, not
−20%. The "hard stop" was a fiction.
**Why current design fails.** Stop is evaluated **on observed premium ticks** (`option_premium <=
stop_price`) and assumes the SELL fills *at* the stop. Real fills are worse; between ticks is a
blind spot. On a 1-min feed the blind spot is **60 seconds** of move.
**Guards.**
- **[ENGINE]** **Per-tranche hard-rupee max loss** as a second stop independent of %: cap absolute
  ₹ loss per tranche (`max_loss_per_tranche_rs`) so a gap-through still bounds size, and size the
  ladder so even a full gap-through < `daily_loss_cap`.
- **[ENGINE]** **Underlying-based stop** in *addition* to premium-based: if the underlying moves
  `stop_underlying_pts` against the open ladder, exit regardless of (possibly stale) premium. The
  underlying tick is more reliable/liquid than the option LTP.
- **[FEED]** Move toward **tick (not 1-min)** premium for the held contract to shrink the blind
  spot; 1-min sampling is too coarse for a −20% stop on a gamma instrument.
- **[FEAS]** Backtest must apply slippage **on the stop leg specifically** (fills are worse in the
  exact scenario the stop triggers) — never fill stops at the trigger price.

### B5. Expiry-day pin & IV crush (the long scalper's slaughterhouse)
**Scenario.** On expiry day, ATM theta is enormous, IV crushes through the afternoon, and the
underlying **pins** to a max-pain strike — chop with no follow-through. The scalper buys ATM,
gets no move, time-stops out repeatedly, each one a small loss; theta + crush make even "right"
directional calls lose. Worst single-day bleed.
**Why current design fails.** No expiry awareness whatsoever. `time_stop_min=12` mechanically exits
stagnant scalps — good — but it does **nothing to stop you re-entering** the same pin all day until
`max_trades=8` or `daily_loss_cap` is hit. 8 expiry-day scalps × small losses = a guaranteed bad
day.
**Guards.**
- **[ENGINE]** **Expiry-day mode:** detect DTE = 0 (need the contract's expiry, currently not fed)
  and apply a stricter regime: lower `max_trades`, **wider** `min_atr_pts` / `mom_thresh` (only
  trade real moves), require ITM (`strike_offset = -1`) to cut theta fraction, or **disable
  entries entirely** after a configurable IST time (afternoon pin/crush window).
- **[ENGINE]** **Theta-aware time-stop scaling:** on expiry day shrink `time_stop_min` (stagnation
  costs far more per minute).
- **[FEED]** Pass `expiry`/`dte` into the engine so it can know it's expiry day at all — today it
  cannot.
- **[ENGINE]** Tighten the pre-close window (`no_trade_close_min`) on expiry — the §3.9 note even
  cites "expiry-pin" but the value is a flat 20 min; make it expiry-aware.

### B6. Scheduled-event volatility spike (RBI, budget, Fed, results, election count)
**Scenario.** 14:00 RBI policy. Spot whipsaws ±150 pts in minutes; spreads blow out; IV spikes then
crushes. Momentum + ATR filters *light up* (lots of "activity") and the scalper fires straight into
the event — into the widest spreads and the IV top.
**Why current design fails.** The ATR/momentum filters **reward** event volatility; there is **no
event calendar**. The engine actively wants to trade the worst moment.
**Guards.**
- **[ENGINE/EXEC]** **Event-window blackout:** an event calendar (the post-M3 "scheduled-event
  calendar filter" already on the platform roadmap) → block new ladders for ±N min around known
  events; optionally flatten before. Reuse, do not re-derive, the equity calendar work.
- **[ENGINE]** During a flagged window, **invert** the activity logic: high ATR + wide spread =
  *block*, not permit.

### B7. Stale / frozen / wrong LTP feed (silent poison)
**Scenario.** The intraday ATM LTP feed (§5b — not built yet) hiccups: it repeats the last print,
returns a stale value, or — worse — the ATM rolled to a new strike as spot moved and the feed is
still quoting the *old* strike's premium. Your TP/stop/trail run on a number that no longer
corresponds to what you hold.
**Why current design fails.** The class has **one** fail-safe: `option_premium is None` suppresses
premium exits (good). But it has **no staleness detection** (a repeated non-None value is trusted),
and **no check that the LTP belongs to the held strike**. A frozen-but-non-None premium will pin
trailing high-water and silently disable the trail/stop.
**Guards.**
- **[ENGINE/FEED]** **Premium staleness guard analogous to `data_age_min`:** stamp each LTP with a
  timestamp; if older than `max_premium_age_s`, treat as None (fail-safe path) and alert.
- **[ENGINE]** **Strike-identity check:** the LTP must carry the `security_id`/strike it is for;
  reject premiums that don't match `self._ladder_strike`. The ATM-roll-as-spot-moves bug (§5b) is
  exactly this.
- **[ENGINE]** **Reject implausible jumps** (e.g. premium doubling/halving in one tick with no
  matching underlying move) as bad prints — don't book a TP or trip a stop off a fat-finger print.

### B8. Mid-session restart leaves a naked scalp (the platform's signature failure)
**Scenario.** `dhan-trader` restarts at 12:30 with 3 lots of long CE open. The engine's tranche
book is **in memory only** — on boot it is empty. `on_tick` now thinks it's flat, manages nothing,
and the position sits **naked**: no TP, no stop, no time-stop, no trail. It only gets squared off
by the unconditional EOD rule at 15:25 — if the process is even alive then.
**Why current design fails.** §6.4 "position via fills only" + in-memory `_tranches` means **the
book does not survive a restart**. ORB solved the analogous hole with `reconcile_on_boot()` +
`seed_opening_ranges()`; the scalper spec has **no reconciliation story at all**. This is the
single most dangerous gap because it converts a managed scalp into an unmanaged naked long.
**Guards.**
- **[ENGINE/EXEC]** **Boot reconciliation:** on start, rebuild the tranche book from the DB
  (`engine_positions` / an intraday scalp log) + broker truth (LIVE) — entry premium, entry
  underlying, fill time per tranche — so stops/time-stops/trails resume correctly. Mirror ORB's
  `reconcile_on_boot()`.
- **[ENGINE]** If the book **cannot** be fully reconstructed (missing entry premium/time), do **not**
  resume scalping it — emit an **immediate flatten** of the unknown position rather than carry a
  position you can't risk-manage. "Unknown long → flatten" is safer than "unknown long → hope."
- **[ENGINE]** Persist tranche state (entry premium, fill time, hi-water, tp index) on every fill so
  reconstruction is exact, not approximate.

### B9. No broker-side stop — the stop dies with the process (naked between ticks)
**Scenario.** The "hard stop" is **synthetic**: it only exists as long as `on_tick` keeps being
called AND the SELL it emits actually fills. If the process dies, the feed stalls, or the executor
is wedged, there is **no resting stop order at the exchange**. A long option with no broker stop is
a naked overnight-if-you-die position.
**Why current design fails.** The platform's known posture (memory `orb-entry-exit-latency`): exits
are poll-only, no broker stop, naked if the process dies. The scalper inherits this **and is worse**
(faster instrument, tighter stops, more positions).
**Guards.**
- **[EXEC]** Place a **resting broker-side stop-loss order (SL/SL-M)** for each tranche at the
  exchange so the stop survives process death. The engine's synthetic stop becomes a *tighter*
  overlay, not the only line of defense.
- **[EXEC]** **Heartbeat/dead-man's-switch:** if the trader heartbeat goes stale while a position is
  open, an independent watcher (health monitor) must alert CRITICAL and, in live, trigger a
  broker-side flatten. (The platform already has `scripts/health_alert.py` + heartbeat — wire the
  open-position case in.)
- **[RISK]** RiskEngine kill-switch must be able to flatten the scalp book independently of the
  strategy object.

### B10. Process death / feed death while holding (managed → unmanaged silently)
**Scenario.** WebSocket drops; `on_tick` stops being called; premium never updates. The position is
frozen in time — no stop, no time-stop — until the feed returns or EOD. With a long option that's a
slow bleed at best, a gap disaster at worst.
**Why current design fails.** The engine is **passive** — it only acts when ticked. No tick = no
risk management. There is no "I haven't been ticked in N seconds while holding" alarm.
**Guards.**
- **[EXEC]** **No-tick watchdog:** while `position_lots > 0`, if no underlying tick arrives within
  `max_tick_gap_s`, treat as a feed outage → fall back to REST LTP poll, and if that fails, alert +
  (live) flatten via broker. Mirrors the runner's REST-fallback posture.
- **[EXEC]** The same applies to a stalled **option** LTP (B7) independently of the underlying tick.

### B10b. Pending-entry deadlock (a wedge that quietly stops all management)
**Scenario.** `on_tick` sets `_pending_entry = True` after emitting an ENTER and blocks further
rungs until `notify_fill`. If the ENTER is **rejected** and `notify_fill` is never called with the
correct semantics, `_pending_entry` stays True forever. Note `notify_fill` clears it on **any** call
including SELLs — so a stray SELL fill could clear a genuinely pending BUY, or a never-confirmed
BUY could wedge entries.
**Why current design fails.** `_pending_entry` is cleared unconditionally at the top of
`notify_fill` regardless of side, and there is **no timeout** on a pending entry.
**Guards.**
- **[ENGINE]** **Pending-entry timeout:** auto-clear `_pending_entry` after `pending_timeout_s` if
  no matching BUY fill arrives (order rejected/lost), with a warning — never wedge silently.
- **[EXEC]** On order REJECTED, call back into the engine to clear the pending flag explicitly
  (don't rely on a future unrelated fill to clear it).

### B11. Partial fills & FIFO mismatch (the book diverges from reality)
**Scenario.** A 3-lot ladder add gets **partially filled** (2 of 3). Or a SELL partially fills. The
engine's `notify_fill` assumes the reported `lots` is the truth and FIFO-reduces; if the executor
reports requested-not-filled quantities, the in-memory book diverges from the broker. TP/stop then
act on phantom lots.
**Why current design fails.** `notify_fill` trusts its `lots` argument completely; there is no
reconciliation against broker position. Partial fills on illiquid options are **normal**, not edge.
**Guards.**
- **[EXEC]** `notify_fill` must be called with **actually filled** quantity and **actual** fill
  premium (from order confirmation polling, like `LiveExecutor.get_order_by_id`), never the request.
- **[ENGINE/EXEC]** Periodic **book-vs-broker reconciliation** (LIVE) — if they diverge, trust the
  broker and flatten/realign rather than manage a fictional book.

### B12. Signal-flip whipsaw → cost bleed (death by a thousand round-trips)
**Scenario.** Choppy tape oscillates across VWAP±band with momentum flipping sign. Each flip
flattens the ladder (full cost stack) and, after cooldown, re-enters the other side (cost stack
again). The strategy pays the spread + ₹40 brokerage + STT *repeatedly* for net-zero direction.
This is the §1b "cost-killer" the design names but does not fully neutralize.
**Why current design fails.** `cooldown_min=3` and the FLAT deadband help, but a 3-min cooldown
permits ~many flip-flops/day; `max_trades=8` caps ladders but each can still flip-exit + re-enter.
Nothing tracks **consecutive losing scalps** or **cost as a fraction of P&L**.
**Guards.**
- **[ENGINE]** **Consecutive-loss circuit breaker:** after N losing scalps in a row (e.g. 3),
  enforce a longer stand-down. Chop is self-identifying via a loss streak.
- **[ENGINE]** **Whipsaw detector:** if the signal has flipped > M times in the last T minutes,
  declare a chop regime and stand down new ladders (raise the activity bar).
- **[ENGINE]** **Per-day cost budget:** track modeled cumulative cost; if cost/gross-P&L exceeds a
  threshold, stand down — the edge is gone for the day.

### B13. Daily-loss cap counts only *realized* P&L (the cap can be blown past)
**Scenario.** `daily_loss_cap` (₹8000) is checked **only in `_update_realized_pnl`**, i.e. on
SELL fills. A large **open** ladder underwater −₹15k has **not** tripped the cap because nothing was
realized; the engine happily holds (and could even add rungs in `scale_in_dips` mode). The cap is a
rear-view mirror.
**Why current design fails.** No **unrealized**-loss kill. The platform RiskEngine watches the
portfolio for exactly this reason; the strategy-level cap does not.
**Guards.**
- **[ENGINE]** Compute **unrealized** P&L from current premium each tick; trip stand-down on
  `realized + unrealized <= −daily_loss_cap`, not realized alone. (Mirrors the M3 backtester's
  "unrealized kill-switch" already shipped — reuse the concept.)
- **[RISK]** The real `RiskEngine` (portfolio-level, paper losses trip it) remains the owner and is
  never bypassed — the strategy cap is a tighter overlay.

### B14. Ladder pyramiding into a reversal (winner → loser amplified)
**Scenario.** Pyramid mode adds lots as the move extends; the 3rd rung fills near the local extreme;
the move reverses; now 3 lots reverse together. The trail (12% off high-water) gives back a chunk of
the whole stack at once. Pyramiding *amplifies* the reversal you didn't see coming.
**Why current design fails.** `max_rungs=3` bounds count but adds are purely move-based
(`rung_spacing_pts`) with **no exhaustion/extension check** — it adds most aggressively exactly when
the move is most extended.
**Guards.**
- **[ENGINE]** **No-add-when-extended:** block adds when the underlying is > X ATR from VWAP /
  ladder anchor (don't pyramid into a stretched move).
- **[ENGINE]** **Tighten the trail after adds:** with more lots on, reduce `trail_pct` so the stack
  gives back less; or trail per-tranche off each rung's own high-water.
- **[ENGINE]** Default `ladder_size_mode="decreasing"` consideration for live — flat sizing puts
  full size at the worst (most-extended) rung.

### B15. `scale_in_dips` mode = martingale into theta (a configured blow-up)
**Scenario.** If `ladder_mode="scale_in_dips"` is ever used, the engine **adds to a losing long**
as it retraces — averaging down a decaying, possibly-IV-crushing option. This is the textbook
options blow-up the spec itself warns about (§2a) yet leaves as a selectable mode.
**Why current design fails.** It's a footgun left loaded. Adding to a losing long compounds delta +
theta loss.
**Guards.**
- **[ENGINE]** Gate `scale_in_dips` behind an explicit hard cap: **strictly smaller** total size
  than pyramid, a **hard ₹ stop on the averaged book**, and never on expiry day / high-VIX. Prefer
  to **disable it for live** and keep it research-only.

### B16. VWAP accumulator distortion (the regime anchor can be wrong)
**Scenario.** VWAP here is a **typical-price, unit-volume** proxy (`_vwap_cum_v += 1` per bar), not a
true volume-weighted VWAP. On a gap or a single huge candle, the proxy VWAP lags reality, so the
LONG/SHORT bias anchor is wrong precisely when it matters (event days). Also: a **duplicate or
out-of-order tick** double-counts into the accumulator (no per-minute dedup).
**Why current design fails.** Each `on_tick` calls `_ingest_bar` unconditionally — there's no
"one bar per minute" guard, so a fast tick stream (multiple ticks/min) inflates `bars_seen`, warms
up too early, and skews VWAP/ATR windows. The engine is documented as bar-driven but is fed ticks.
**Guards.**
- **[ENGINE]** **Bar-close discipline:** aggregate ticks → 1-min bars (reuse `BarBuilder`) and call
  `_ingest_bar` **once per closed minute**, not per tick. Otherwise warm-up, ATR, momentum-k, and
  VWAP are all measured in "ticks," not minutes, and every threshold is silently wrong.
- **[ENGINE]** Dedup/monotonic-time guard on bar ingest (ignore a bar for a minute already seen).
- **[FEED]** If a real volume is available for the underlying/futures, use it for a true VWAP;
  document the proxy's limits otherwise.

### B17. Costs make small targets unwinnable (structural, not edge)
**Scenario.** §5d's own worked example: +10% on a ₹120 ATM = ₹780/lot gross; ₹40 brokerage +
STT/fees + ~₹1.2–2.4/unit slippage **each side** can erase a third-to-half of that. The first TP
rung may be **net-negative** after costs — the strategy can be profitable in "premium %" and lose
money in rupees.
**Why current design fails.** The engine books TP at gross premium %; it has **no cost model in the
exit gate**. Profitability lives entirely in a report the engine doesn't read.
**Guards.**
- **[ENGINE]** **Net-of-cost TP:** the first TP rung must clear modeled round-trip cost+slippage
  with margin, or be widened. Make `tp_ladder_pct[0]` validated against the break-even at runtime
  (refuse/scale the trade if it can't clear).
- **[FEAS]** Report **net-of-cost per-scalp expectancy** and **break-even target %** prominently;
  treat a positive gross / negative net as a **fail**.

### B18. Strike rounding / wrong-contract selection (you scalp the wrong option)
**Scenario.** `_round_to_step(22030, 50) = 22050` (rounds up). Near a half-step the chosen ATM can be
the *further* strike. Combined with `strike_offset` sign conventions (− = ITM, applied with opposite
signs for CE vs PE), a config slip selects a wrong/illiquid strike. Worse: the selected strike may
not exist / not be tradeable that expiry (deep strikes, holidays).
**Why current design fails.** The engine computes a strike integer with **no validation that the
contract exists, is liquid, or matches the scrip master**. Equity screener validates instruments;
the scalper does not.
**Guards.**
- **[EXEC]** Validate the computed `(strike, option_type, expiry)` against `fno_instruments` (exists,
  tradeable, within freeze/liquidity) before sending — reject + alert otherwise.
- **[ENGINE]** Make the round/offset convention test-locked (it is partly, §7) and assert CE/PE
  offset symmetry explicitly.

### B19. Freeze-quantity slicing & order rejection (size you request ≠ size you get)
**Scenario.** A larger ladder exceeds the NIFTY **freeze quantity** per order; the broker rejects or
the client slices it (`core/client.py` slices over freeze). Sliced orders fill at **different prices**
across slices, so the tranche's `entry_premium` is not a single number — the book's per-tranche stop
is computed off a price that isn't real.
**Why current design fails.** Engine assumes one fill = one premium per tranche. `freeze_qty` exists
in the scrip master but the engine is unaware.
**Guards.**
- **[EXEC]** Keep lots small enough to avoid freeze slicing for a scalper (the default 1-lot rungs
  help); if sliced, report a **volume-weighted** fill premium back via `notify_fill`.
- **[ENGINE]** Accept that `entry_premium` is a VWAP of fills, not a single print; document it.

### B20. Cooldown / clock edge cases (tz-naive vs tz-aware, DST-free but still buggy)
**Scenario.** `now` is sometimes tz-aware, sometimes naive (the code defensively patches both in
several places). A mismatch in `_cooldown_until` comparison or the future-skew `ref` could let an
entry through during cooldown or wrongly drop a valid tick.
**Why current design fails.** Mixed tz handling is a smell; the future-skew guard compares `now -
ref` where `ref` is force-stripped to match `now`'s tz-ness — subtle and fragile.
**Guards.**
- **[ENGINE]** **Normalize all timestamps to tz-aware IST at the boundary** (one place), then assume
  aware everywhere. Remove the scattered `replace(tzinfo=...)` patches. Add tests for a naive-tick
  stream and an aware-tick stream producing identical decisions.

### B21. Daily counters never reset on a stuck process (multi-day drift)
**Scenario.** Session reset keys off `now.date()` changing. If the process runs across midnight
without a tick that crosses the date boundary cleanly, or a future-skewed tick is (correctly)
ignored but was the only date-change signal, counters/VWAP could carry over. The backfill saw an
analogous "stale in-memory token on multi-day runs" class of bug.
**Why current design fails.** Single trigger (`_session_date != today`) with the future-skew guard
*before* it — a future-stamped tick is dropped before it can reset, which is correct, but a long gap
with no ticks means no reset until the next real tick.
**Guards.**
- **[ENGINE]** Reset is idempotent and date-keyed (good) — keep it, but ensure the **first tick of a
  new day always triggers reset before any decision** (it does; lock it with a cross-midnight test).
- **[EXEC]** A scheduled EOD job should assert flat + reset, independent of tick arrival.

---

## C. Cross-cutting: the unconditional EOD square-off is necessary but not sufficient

§3.10 / §6.2 EOD square-off is the strategy's main safety net and it is correctly **above all other
gates**. But it only fires **if the engine is ticked after 15:25 AND the SELL fills**. It does not
protect against B8 (restart loses the book → EOD doesn't know to flatten), B9/B10 (process/feed dead
at 15:25), or B2/B3 (can't fill the square-off). The EOD square-off must be backed by:
- **[EXEC]** a **broker-side EOD flatten** (independent scheduled job that flattens any open F&O at
  ~15:25 regardless of strategy state), and
- **[RISK]** the kill-switch path that can flatten the book without the strategy object.

A square-off that depends on the engine being alive and the market being liquid is not a guarantee.

---

## D. Minimum hardening checklist before forward-paper (and the gating ones for live)

Forward-paper (§5b) gate — must exist before a real paper track means anything:
- [ ] **[FEED]** Intraday ATM LTP feed with **bid/ask + timestamp + strike identity** (B3, B7).
- [ ] **[ENGINE]** Premium **staleness** + **strike-identity** + **implausible-jump** guards (B7).
- [ ] **[ENGINE]** Bar-close discipline: one `_ingest_bar` per closed minute (B16).
- [ ] **[ENGINE]** **Max-spread** entry gate + **net-of-cost** first TP (B3, B17).
- [ ] **[ENGINE]** **Unrealized** daily-loss kill, not realized-only (B13).
- [ ] **[ENGINE]** **Pending-entry timeout** (B10b).
- [ ] **[ENGINE/FEED]** Expiry/DTE + VIX passed in → **expiry-day mode** + **IV-spike filter**
      (B1, B5).
- [ ] **[ENGINE]** Consecutive-loss / whipsaw circuit breaker (B12).

Live gate — additionally MANDATORY (these are the naked-position killers):
- [ ] **[EXEC]** **Boot reconciliation** of the tranche book + "unknown long → flatten" (B8).
- [ ] **[EXEC]** **Resting broker-side stop** per tranche + dead-man's-switch (B9).
- [ ] **[EXEC]** **No-tick / no-premium watchdog** with REST fallback + flatten (B10).
- [ ] **[EXEC]** **Filled-qty/price** reconciliation (partial fills, freeze slicing) (B11, B19).
- [ ] **[EXEC]** **Circuit/freeze awareness** on exit; trapped-position alerting (B2).
- [ ] **[EXEC]** **Broker-side EOD flatten** independent of the engine (Section C).
- [ ] **[EXEC]** Contract **existence/liquidity validation** before send (B18).
- [ ] **[RISK]** Platform `RiskEngine` owns the kill-switch and can flatten the scalp book
      independently — never bypassed.

**Bottom line:** the engine class is a clean, testable *signal* machine, but "bulletproof" is a
property of the **whole pipeline**. As written, the scalper has **at least three live-grade naked
exposures** — restart-loses-the-book (B8), no-broker-side-stop (B9), and feed-death-while-holding
(B10) — plus structural cost/spread blindness (B3/B17) and no expiry/event/IV awareness (B1/B5/B6).
Until the [EXEC]/[FEED]/[RISK] guards above exist, the honest label is **"managed only while
everything upstream is healthy"**, which for a long-option intraday scalper is the opposite of
bulletproof.
