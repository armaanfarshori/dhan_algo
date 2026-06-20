# 03 — Per-trade exit discipline + ladder (Options Scalper)

**Status:** SPEC — research only, PAPER. No live order path.
**Scope:** the *exit half* of `strategies/options_scalper.py` — stop-loss, take-profit,
trailing, time-stop, and the ladder add/reduce rules — pinned to concrete params and to
the exact `ScalpDecision` the engine already emits.
**Source of truth:** `strategies/options_scalper.py` (the live ladder engine) and
`docs/fno_strategies/options_scalper.md` §2–§3. This file is the per-trade discipline
layer; it does **not** introduce new mechanics, it specifies the numbers, ordering, and
intent, and confirms each rule's mapping into code.

> **Why exits, not entries, are the whole game here.** This is *long premium, intraday*.
> Two forces work against you every second you hold: **theta** (premium bleeds with time)
> and **the spread** (you cross it twice per round-trip, ~₹40 brokerage + STT/fees +
> slippage before any P&L). The edge is not in being right often — it is in **cutting
> losers before theta+spread compound them** and **banking winners on momentum exhaustion
> instead of round-tripping them**. Exits are therefore deliberately **asymmetric and
> tight**: small, laddered take-profits; a hard stop; and an aggressive *time*-stop that
> exists purely because a scalp that isn't working *is already losing* to theta.

---

## 0. The one-line discipline

> **Buy ATM CE/PE 1 lot on signal; add 1 lot every +10 NIFTY pts in favor up to 3 lots
> (pyramid, flat sizing); bank +10% / +20% / +35% of each lot's premium, then trail the
> last lot 12% off its premium high; hard stop −20% per lot, time-stop 12 min if it hasn't
> earned its first TP, flatten the whole ladder on a signal flip, flat by EOD.**

Asymmetry check (per lot, default params): first target **+10%**, hard stop **−20%**.
That looks "wrong" (risk > first reward) but is intentional for a *laddered* book — you
take +10% on lot 1, +20% on lot 2, +35%/trail on lot 3, so the *blended* winning ladder
clears the −20% tail with margin, while the time-stop caps the dominant failure mode
(stagnation) well before −20%. The stop is the *catastrophe* rail; the **time-stop is the
primary loss-control** for a scalper.

---

## 1. ScalpDecision — the only output contract

Every exit decision is exactly this dataclass (frozen, in `options_scalper.py`):

```python
ScalpDecision(action="EXIT", side="SELL",
              option_type="CE"|"PE", strike=<ladder strike>,
              lots=<lots this decision sells>, reason="<audit string>")
```

Rules that hold for **all** exits:
- `action="EXIT"`, `side="SELL"` always (long-only — the only SELLs are exits).
- `option_type` / `strike` are the **ladder's fixed contract** (`_ladder_option_type`,
  `_ladder_strike`), set at first entry and reused for every add and every exit.
- `lots` is the count this decision sells — a *partial* (one tranche) or the *whole* book.
- The engine **never mutates the tranche book on emit.** State changes only when the caller
  calls `notify_fill("SELL", lots, premium, now)` (FIFO reduce) or `notify_flat()`. So an
  exit decision is a *request*; the book is truth-via-fills (hard rule 4).
- `reason` is a human-readable audit string (already produced by the engine) — keep it; the
  forward-paper log and calibration read it.

`option_premium` (LTP of the held contract) drives every premium-based exit. **If it is
`None` while a position is open, no premium-based exit may fire** — the engine logs a
stale-premium warning and returns `None` (fail-safe rule 6). Session/EOD rules still fire.

---

## 2. Exit priority order (as implemented in `_evaluate_exits`)

Exits are checked **once per tick, in this fixed order**, and the **first** match returns.
This ordering is the discipline — safety/whole-ladder rails first, premium scalping last:

| # | Rule | Scope | Premium needed? | Maps to |
|---|------|-------|-----------------|---------|
| 1 | **Signal-flip** | whole ladder | no | `_flatten_all("Signal-flip …")` |
| 2 | **Daily-loss residual flatten** | whole ladder | no | `_flatten_all("Daily-loss stand-down …")` |
| 3 | *(premium None → return, fail-safe)* | — | — | warn + `None` |
| 4 | **Hard stop** | per tranche; all-breach → whole | yes | partial EXIT or `_flatten_all` |
| 5 | **Time-stop** | per tranche; all-breach → whole | yes | partial EXIT or `_flatten_all` |
| 6 | **TP ladder / trailing** | front tranche | yes | partial EXIT |

EOD square-off (§6) sits **above all of these**, evaluated at the top of `on_tick` before
exits or entries are even considered. It is unconditional.

**Why this order:** a flip means the *thesis is gone* — get out whole, don't keep scalping
TPs in the wrong direction. The hard stop precedes the time-stop so a fast adverse move
exits at the −20% rail rather than waiting for the clock. The TP ladder is last because it
is the only *optional/opportunistic* exit — everything above it is loss control.

---

## 3. Stop-loss (hard, catastrophe rail)

- **Param:** `stop_pct = 0.20` (20% of *each tranche's own* fill premium).
- **Trigger:** for any tranche, `option_premium <= entry_premium * (1 - stop_pct)`.
- **Per-tranche, with whole-ladder shortcut:** the engine sums lots of *all breached*
  tranches into `stop_lots`. If `stop_lots >= position_lots` it emits one `_flatten_all`;
  otherwise a partial `EXIT SELL lots=stop_lots` (`reason="Hard stop partial …"`).
- **Premium-relative on purpose:** 20% of the actual fill premium works identically across
  strikes/days (an ATM ₹120 premium → stop at ₹96; an ₹80 premium → stop at ₹64). No
  absolute-rupee stop, which would be too loose on cheap days and too tight on rich ones.
- **No broker-side stop order in PAPER.** This is a *logical* stop checked each tick against
  LTP; in live wiring the RiskEngine still owns the real kill-switch and is never bypassed.

Test anchor (spec §7.9): fill @ ₹120 → exit at ₹96; **no** exit at ₹100.

---

## 4. Take-profit ladder (bank winners, in order)

- **Param:** `tp_ladder_pct = [0.10, 0.20, 0.35]` — three successive premium targets as a
  fraction of *the tranche's* fill premium.
- **One tranche per level, in order, via a single class-level cursor.** `_tp_ladder_index`
  advances 0→1→2; each fired level sells **one tranche's lots** (the front tranche). So with
  a full 3-lot ladder: **+10% banks lot 1, +20% banks lot 2, +35% banks lot 3.** This is the
  "many small profits" core of a scalp — you are taking money off as momentum delivers it,
  not betting the whole position on the biggest target.
- **Trigger (front tranche `tr`):**
  `option_premium >= tr.entry_premium * (1 + tp_ladder_pct[_tp_ladder_index])`.
- **Fewer rungs than TP levels:** the cursor still runs in order against whatever is open
  (a 1-lot ladder takes `tp_ladder_pct[0]` then trails).
- **Reason string:** `TP[{i}] +{pct}% (premium ₹…)`.

**Momentum-exhaustion banking, not greed:** the targets are small (10/20/35%) precisely
because intraday option moves *mean-revert and decay*; holding for a "home run" gives theta
and the spread a second bite. The trailing remainder (§5) is the *only* uncapped lot, so a
genuine runaway is still captured without exposing the whole book to give-back.

Test anchors (spec §7.7): 3 lots @ ₹120 → ₹132 sells 1, ₹144 sells 1, ₹162 → top level →
last lot converts to trailing.

---

## 5. Trailing stop (the uncapped remainder)

- **Param:** `trail_pct = 0.12` — give back 12% of premium from the high-water mark.
- **Activation:** when the **final** TP level (`tp_ladder_pct[-1]`) fires, the *next*
  tranche is pre-marked `trailing = True` and seeded with the current premium as its
  high-water, so it trails immediately on the following tick.
- **Mechanics:** each tick updates `hi_water_premium = max(hi_water, option_premium)`; exit
  when `option_premium <= hi_water_premium * (1 - trail_pct)`.
- **Scope:** the single remaining (last) tranche only. Earlier tranches are already banked at
  fixed TPs; only the runner trails.
- **Reason:** `Trail hit ₹… (high-water ₹…, floor ₹…)`.

**Why 12% give-back:** wide enough to survive normal 1-min premium jitter on a trending
move, tight enough to lock most of a momentum extension. Tighter than the −20% hard stop
because by this point the lot is *in profit past +35%* — we are protecting gains, not
capital.

Test anchors (spec §7.8): runs to ₹180 high-water → exits at ₹158.4 (180×0.88); **no** exit
at ₹165.

---

## 6. Time-stop (theta guard — the primary scalper rail)

- **Param:** `time_stop_min = 12` minutes.
- **Trigger:** a tranche that **has not yet hit its first TP** (`tr.tp_index == 0`) and whose
  age `(now - fill_time) >= time_stop_min` is exited at market.
- **Per-tranche, with whole-ladder shortcut:** sums all such tranches into `time_stop_lots`;
  all-breach → `_flatten_all`, else partial `EXIT SELL`.
- **Why this is the most important exit for a scalp:** a long option that has gone *nowhere*
  for 12 minutes is not "waiting" — it is *bleeding theta and still paying the spread to
  exit*. Stagnation is the dominant failure mode (more common than a clean −20% stop hit),
  so the time-stop, not the price stop, does most of the day's loss control. It deliberately
  fires **before** a slow grind reaches the −20% hard stop.
- **Note:** once a tranche has earned its first TP (`tp_index > 0` / it's banked or
  trailing), the clock no longer applies to it — a working scalp is allowed to run under the
  TP/trail rules.

Test anchors (spec §7.10): fill @ 10:00, oscillates ±5% (no TP1) → exit at 10:12; **no**
time-stop at 10:11.

> **Implementation note (flag for the coder):** the time-stop reads `tr.tp_index`, but the
> partial-TP path advances the *class-level* `_tp_ladder_index` rather than the front
> tranche's `tp_index` (which stays 0). If a tranche is partially TP'd but not removed, it
> could still be eligible for a time-stop. With the current "one tranche per TP level" sizing
> this is benign (a TP'd tranche is sold whole on the next fill), but if tranche sizing ever
> diverges from TP granularity, set `tr.tp_index` on the exited tranche so a banked tranche
> is never re-counted by the theta guard. Track as a follow-up, not a blocker.

---

## 7. Whole-ladder exits (thesis / risk rails)

These flatten the **entire** book in one `ScalpDecision` (`lots = position_lots`).

- **Signal-flip (highest-priority exit):** §1 anchor flips against the open ladder
  (LONG→SHORT or SHORT→LONG). Flatten all. **Do not reverse on the same tick** — a fresh
  entry must re-qualify next tick (avoids ping-pong); cooldown then applies.
  `reason="Signal-flip LONG→SHORT"` / `"…SHORT→LONG"`.
- **Daily-loss kill:** realized intraday net P&L `<= -daily_loss_cap` (₹8000) sets
  `_standing_down`; any residual book is flattened (`"Daily-loss stand-down — flatten
  residual"`) and **no new ladders** open for the rest of the day. Strategy-level analogue
  of the platform RiskEngine; the real RiskEngine still owns the live kill-switch.
- **EOD square-off — UNCONDITIONAL, evaluated first in `on_tick`:** at
  `MARKET_CLOSE − squareoff_before_close_min` (5 min → 15:25 IST), if any lots are open,
  emit one EXIT for the whole book regardless of signal/ladder/P&L/warm-up. Guarded by
  `_eod_exit_emitted` so it fires once. Long options are never carried overnight.

---

## 8. Ladder — how it adds and reduces

The ladder is *scale-in on the way up, scale-out in pieces* — the entry side is summarized
here only as it bears on exit discipline (full entry spec in `02_*`/parent §2a).

### 8a. Adds (pyramid a winner)
- **Param:** `ladder_mode = "pyramid"` (default), `rung_spacing_pts = 10.0`,
  `tranche_lots = 1`, `max_rungs = 3`, `ladder_size_mode = "flat"`.
- Add a rung only when the underlying has moved a further `rung_spacing_pts` **in the
  trade's favor** from the previous rung *and* the §1 anchor still matches the ladder
  direction. Never add while FLAT or flipped.
- **Default is pyramid, not averaging-down.** `scale_in_dips` (add on an adverse retrace)
  exists as a knob but is **off by default**: for long options, scaling into adverse moves
  compounds theta + delta loss and is the classic blow-up. Adds only happen when the thesis
  is *already working*.
- `decreasing` sizing (1, then halve, floor 1) caps risk at extended prices; `flat` is the
  default. `max_rungs=3` hard-caps exposure at 3 lots/ladder.

### 8b. Reduces (the exit ladder above, restated as "how the ladder shrinks")
The book shrinks **only** via §3–§7 exits, FIFO through `notify_fill("SELL", …)`:
1. **TP ladder** peels one tranche per +10/+20/+35% level (banking winners piecewise).
2. **Trailing remainder** sells the final runner on a 12% give-back.
3. **Hard stop / time-stop** peel breached tranche(s); all-breach collapses to a flatten.
4. **Flip / daily-loss / EOD** flatten the whole book at once.

When the book reaches zero lots, `_on_fully_flat` fires: it sets the **re-entry cooldown**
(`cooldown_min = 3` min — no new ladder until then), resets all ladder context and the TP
cursor, and timestamps the flatten. `max_trades = 8` caps **ladders started per day**.

---

## 9. Parameter table (per-trade exit + ladder)

All defaults verbatim from `ScalperParams` in `options_scalper.py` — this spec sets no new
numbers, it pins these as the discipline.

| Param | Default | Role | Tightness rationale |
|-------|---------|------|---------------------|
| `stop_pct` | `0.20` | hard stop, % of tranche fill premium | catastrophe rail; loose vs first TP by design — time-stop is primary |
| `tp_ladder_pct` | `[0.10, 0.20, 0.35]` | per-lot TP levels, in order | small targets bank momentum before theta/spread reclaim it |
| `trail_pct` | `0.12` | give-back from high-water on the runner | only uncapped lot; protects gains, survives 1-min jitter |
| `time_stop_min` | `12` | minutes before a TP-less tranche is cut | **primary loss control** — a stagnant long is already losing to theta |
| `cooldown_min` | `3` | post-flatten lockout before new ladder | stops instant re-fire on the same micro-move |
| `rung_spacing_pts` | `10.0` | underlying pts between adds | adds only on real follow-through, not noise |
| `tranche_lots` | `1` | lots per rung | small unit; ladder, don't lump |
| `max_rungs` | `3` | tranches per ladder | hard exposure cap (3 lots) |
| `ladder_mode` | `"pyramid"` | add direction | add to winners only; never average down by default |
| `ladder_size_mode` | `"flat"` | rung sizing | equal lots; `"decreasing"` available |
| `max_trades` | `8` | ladders started/day | turnover/cost cap |
| `daily_loss_cap` | `8000.0` | ₹ realized-loss stand-down | strategy-level kill |
| `squareoff_before_close_min` | `5` | EOD flatten offset (→15:25) | unconditional, no overnight |
| `no_trade_open_min` / `no_trade_close_min` | `15` / `20` | entry blackout windows | avoid open-auction + pre-close illiquidity |

---

## 10. Cost reality the exits must clear (do not skip)

Every scalp pays the **full cost stack on every round-trip**: 2 × ₹20 brokerage = **₹40**,
plus sell-side STT (0.15% of premium), exchange fee, SEBI, stamp, GST, **and slippage on
both legs** (use a conservative `slippage(premium, pct≥0.01)` for small-target scalping —
you cross the spread both ways). Worked feel: ATM premium ~₹120, a **+10% target = ₹12/unit
≈ ₹780/lot gross**; ₹40 brokerage + STT/fees + ~₹1.2–2.4/unit slippage *each side* can erase
a third-to-half of that. **The discipline above is only viable if the average winning scalp
clears round-trip cost+slippage with margin.** The forward-paper / backtest report MUST show
**net-of-cost per-scalp expectancy** and the **break-even target %** at the assumed cost
stack, prominently — that is the real go/no-go, not gross hit-rate.

---

## 11. Cross-references
- Engine: `strategies/options_scalper.py` (`_evaluate_exits`, `_evaluate_ladder_add`,
  `_on_fully_flat`, `_tranche_lots_for_rung`, `ScalperParams`, `ScalpDecision`).
- Parent spec: `docs/fno_strategies/options_scalper.md` §2 (ladder), §3 (exits), §4
  (params), §5 (data feasibility / cost dominance), §7 (test cases this discipline must pass).
- Data gap (blocks live exit testing): no intraday option-LTP feed today — exits that depend
  on `option_premium` can only be exercised against a live intraday ATM CE/PE subscription
  (separate PR) or a forward-paper LTP stream. See parent §5.
```
