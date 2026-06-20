# 04 — Daily Risk Governor (the "always green at EOD" honesty layer)

**Status:** PLAN — research only, PAPER. No live order paths. Sits on top of `strategies/options_scalper.py`.
**Branch:** `feat/fno-options-strategies`
**Audience:** the engineer wiring per-day risk controls into the intraday scalper, and the reviewer who needs to know exactly what is and is not guaranteed.

---

## 0. What this is (and the honest promise)

A **Daily Risk Governor** is a per-day state machine that decides, on every `on_tick`, whether the
scalper is allowed to open new ladders, must switch to **trail-only** management, or must **stand
down** for the rest of the session. It is the closest *honest* version of "always green at end of
day."

> **READ THIS — what it does NOT do.** This governor REDUCES variance and PROTECTS green days.
> It does **not**, and cannot, guarantee that every day ends green. A single position can gap or
> slip through its stop (long options can lose value fast around news/expiry), and the day's first
> trades can lose before any profit-lock has anything to lock. The governor's true claims are:
>
> 1. **A day cannot blow up** — realized loss is hard-capped at `−daily_loss_cap`; after that the
>    book is flat and no new risk goes on (a stop-gap fill can overshoot the cap by at most one
>    in-flight tranche's stop distance, not by an unbounded amount).
> 2. **A *sufficiently* green day cannot turn red** — once realized P&L clears the **profit-lock
>    floor**, the floor is ratcheted up and the day is defended at a positive number; it can give
>    back gains down to the floor but not below it.
> 3. **The number of ways a day can go wrong is bounded** — max trades, consecutive-loss cooldown,
>    and the no-new-entry-after-T / square-off-before-close rules cap how many independent bets the
>    day takes.
>
> Anyone who says "this makes every day green" is wrong. It makes the *bad days small and the good
> days hard to surrender.* That is the entire pitch.

This is the **strategy-level** analogue of the platform `engine/risk.py` `RiskEngine`. In live
wiring the real `RiskEngine` still owns the kill-switch and is **never** bypassed (Safety rule 2);
this governor is an *additional, narrower, strategy-scoped* layer that runs inside the scalper's
`on_tick`. Two independent brakes, never one replacing the other.

---

## 1. Relationship to what already exists in `options_scalper.py`

`ScalperParams` and `OptionsScalper` already implement a **subset** of a daily governor today:

| Already present | Field / state | Behaviour |
|---|---|---|
| Daily-loss cap | `daily_loss_cap=8000.0`, `_daily_realized_pnl`, `_standing_down` | On realized P&L ≤ `−cap`, `_standing_down=True` → no new ladders (`_evaluate_entry` early-returns); residual flattened in `_evaluate_exits`. |
| Max trades / day | `max_trades=8`, `_trades_today` | `_evaluate_new_ladder` returns `None` at the cap. |
| Re-entry cooldown | `cooldown_min=3`, `_cooldown_until` | After a *full flatten*, no new ladder until cooldown elapses. |
| No-trade windows | `no_trade_open_min=15`, `no_trade_close_min=20` | `_evaluate_entry` blocks entries outside the window. |
| Unconditional EOD square-off | `squareoff_before_close_min=5`, `_eod_exit_emitted` | Above all gates in `on_tick`. |

**What is MISSING and is the substance of this spec:**

1. **Daily PROFIT-LOCK** — after `+Y`, defend the day so a green day can't go red (stop, or switch
   to **trail-only**). *Not implemented at all today.*
2. **Consecutive-loss cooldown** — after `N` losing ladders in a row, pause longer than the normal
   `cooldown_min`. Today's cooldown is loss-blind (fires after every flatten equally).
3. **Trail-only management mode** — a state where existing tranches are still managed but **no new
   ladders and no new rungs** open. Today there is binary trade / stand-down only.
4. **A single explicit governor state machine** so these interact deterministically, instead of
   four independent early-returns whose ordering is implicit.

This spec defines #1–#4 as a small `DailyGovernor` that the scalper consults. The existing fields
above are **folded into it** (or kept and read by it) — see §6 integration — so there is one source
of truth per day, not two drifting copies.

---

## 2. The governor state machine

Five states. The governor is in exactly one at a time, evaluated fresh at the top of every
`on_tick` (after session-reset, after the unconditional EOD square-off check).

```
                          realized ≤ −daily_loss_cap
        ┌──────────────────────────────────────────────────────────────┐
        │                                                               ▼
   ┌─────────┐  realized ≥ profit_lock_arm   ┌──────────┐        ┌────────────┐
   │ ACTIVE  │ ────────────────────────────► │ LOCKED   │        │ STOOD_DOWN │
   │ (trade) │                               │ (trade,  │ ─────► │ (flat for  │
   └─────────┘ ◄──────┐                      │  defended│  floor │  the day)  │
        │             │                      │  floor)  │ broken └────────────┘
        │ N losses    │ cooldown elapsed     └──────────┘             ▲
        │ in a row    │                            │                  │ profit-lock
        ▼             │                            │ profit_lock_mode │ "stop"  mode
   ┌──────────┐       │                            │ == "trail_only"  │
   │ COOLDOWN │ ──────┘                            ▼                  │
   │ (no new) │                            ┌──────────────┐          │
   └──────────┘                            │ TRAIL_ONLY   │ ─────────┘
                                           │ (manage open,│  all flat
                                           │  no new)     │
                                           └──────────────┘
```

- **ACTIVE** — normal. New ladders + rungs allowed (subject to the existing window / max-trades /
  cooldown gates).
- **COOLDOWN** — entered after a *full flatten* (re-entry cooldown) **or** after a consecutive-loss
  streak (longer pause). No new ladders; **open tranches are still managed/exited normally**.
  Returns to ACTIVE (or LOCKED) when the cooldown timestamp passes.
- **LOCKED** — realized P&L has cleared `profit_lock_arm`. A **profit floor** is now active and
  ratcheted up as P&L climbs. New ladders allowed *only if* `profit_lock_mode == "stop"` would not
  apply yet (see §3); the day is now defended.
- **TRAIL_ONLY** — `profit_lock_mode == "trail_only"` and the lock has armed: keep managing open
  tranches (trailing them out) but **open no new ladders and add no new rungs**. The green is being
  walked off the field, not re-risked.
- **STOOD_DOWN** — terminal for the day. Realized loss hit `−daily_loss_cap`, **or** the profit
  floor was broken in `"stop"` mode, **or** consecutive-loss hard limit hit. Book is flattened;
  no new ladders; EOD square-off still fires if anything is somehow open. Only a new session
  (date change) clears it.

**State is recomputed from counters every tick — it is not edge-triggered bookkeeping.** This is
the `engine/risk.py` discipline: the meter drives the state, so a mid-session restart that rebuilds
counters lands in the correct state (see §5).

---

## 3. The profit-lock (the one genuinely new mechanic)

This is what lets a *green* day refuse to go red.

### 3a. Arming

The lock arms the first tick realized P&L (net of modeled costs) reaches `profit_lock_arm`
(default **₹6,000**). On arming:

```
floor = profit_lock_arm × profit_lock_floor_frac      # default 0.5  → ₹3,000 floor
```

The **floor is the minimum realized P&L the day will be allowed to keep.** Once armed, the floor
never decreases for the rest of the session (a ratchet).

### 3b. Ratcheting the floor up

After arming, every tick recompute a *candidate* floor from the running realized P&L high-water:

```
hi_water = max(hi_water, realized_pnl)
candidate = hi_water − profit_lock_giveback        # default giveback ₹2,500
floor = max(floor, candidate)                      # ratchet — never lowers
```

So if the day runs to +₹12,000, the floor ratchets to +₹9,500: the day can give back ₹2,500 of
gains but is defended at +₹9,500. Two knobs shape the curve:

- `profit_lock_giveback` (₹, default **2,500**) — how much of the high-water you're willing to
  return before defending. Smaller = tighter lock, more "scratch-green" days, more whipsaw exits.
- `profit_lock_floor_frac` (default **0.5**) — the *initial* floor as a fraction of `arm`, so the
  very first thing you defend at arm time is half the trigger, not the trigger itself (avoids
  arming and instantly tripping on the cost of the in-flight tranche).

### 3c. Defending the floor — two modes (`profit_lock_mode`)

When **realized P&L falls back to the floor** (`realized_pnl ≤ floor`), the governor reacts per
`profit_lock_mode`:

- `"trail_only"` *(default, recommended)* — transition to **TRAIL_ONLY**: stop opening new ladders
  and new rungs immediately; let the *currently open* tranches trail out under the normal §3
  trail/TP/stop logic of the scalper. Rationale: you keep the optionality of an in-flight winner
  running, but you stop *adding* fresh risk on a day that has already given back its cushion. This
  is the gentler lock and usually the right default for a momentum scalper.
- `"stop"` — transition to **STOOD_DOWN**: flatten everything now and stand down for the day. The
  hard version: the moment the day threatens to surrender the locked gain, the day is over, green.

Either way, **the day cannot fall below the floor by opening new bets** — the only thing that can
still erode realized P&L after the floor binds is the exit of already-open tranches (and in
`"stop"` mode even those are flattened immediately). This is the precise, defensible meaning of
"a green day can't go red": *a sufficiently green day is defended at a positive floor; it is not
made immortal.*

### 3d. Interaction with the loss cap

The loss cap and the profit floor are the two rails. Early in the day (before arm) only the loss
cap binds: the day can lose down to `−daily_loss_cap`. Once the lock arms, the *effective* floor is
`max(floor, −daily_loss_cap)` — i.e. the loss cap is always the absolute hard floor and the profit
lock only ever raises the defended level above it. They never conflict; the governor always
defends `max(profit_floor_if_armed, −daily_loss_cap)`.

---

## 4. Parameters (extend `ScalperParams`)

Add a nested block to `ScalperParams` (or a `GovernorParams` dataclass the scalper composes —
either is fine; nested keeps one params object to pass around). Defaults are tuned for the existing
₹8,000 loss cap and a NIFTY 65-lot scalper; **all are research defaults, not validated** (no
intraday backtest exists yet — see `options_scalper.md` §5).

```python
@dataclass
class GovernorParams:
    # ── hard daily-loss circuit-breaker (§3d) ───────────────────────────────
    daily_loss_cap: float = 8000.0        # ₹ realized-loss stand-down (already in ScalperParams;
                                          #   fold to one source of truth — the governor owns it)

    # ── daily PROFIT-LOCK (§3) ──────────────────────────────────────────────
    profit_lock_arm: float = 6000.0       # ₹ realized; lock arms at/above this
    profit_lock_floor_frac: float = 0.5   # initial floor = arm × this  (→ ₹3,000)
    profit_lock_giveback: float = 2500.0  # ₹ off the realized high-water the floor trails
    profit_lock_mode: str = "trail_only"  # "trail_only" | "stop"  on floor break

    # ── trade-count + streak governors (§1, §5) ─────────────────────────────
    max_trades: int = 8                   # ladders started / day (already in ScalperParams; fold)
    consec_loss_limit: int = 3            # losing ladders in a row → long cooldown
    consec_loss_cooldown_min: int = 20    # pause after the streak (vs normal cooldown_min)
    consec_loss_hard_stop: int = 5        # losing ladders in a row → STOOD_DOWN for the day

    # ── time gates (already in ScalperParams; fold) ─────────────────────────
    no_trade_open_min: int = 15           # no new ladders before OPEN + this
    no_trade_close_min: int = 20          # no new ladders after CLOSE − this  ("time T")
    squareoff_before_close_min: int = 5   # unconditional flatten before this
```

**A ladder is "a loss"** for the streak counters when its *full* flatten leaves the ladder's
cumulative realized P&L (sum over its tranches, net of modeled costs) `< 0`. A scratch (`== 0`) is
**not** a loss and does not advance the streak. A win **resets** the streak to 0. The streak is
tracked per *ladder*, not per tranche (a partial TP that nets the ladder green should not be
punished as a loss).

### 4a. Suggested presets

| Preset | arm | giveback | mode | loss_cap | consec_limit | Feel |
|---|---|---|---|---|---|---|
| **Conservative (default)** | 6,000 | 2,500 | trail_only | 8,000 | 3 | Locks early, walks gains off, small red days. |
| **Tight scratch-green** | 4,000 | 1,500 | stop | 6,000 | 2 | Hunts for green-then-done; many flat-to-small days, low variance, low ceiling. |
| **Let-it-run** | 10,000 | 5,000 | trail_only | 12,000 | 4 | Higher ceiling, wider floor, accepts more give-back; bigger green days, deeper reds. |

Pick by *temperament*, then validate on the forward paper track (`options_scalper.md` §5b) before
trusting any of it.

---

## 5. State the governor maintains (per session)

Mirrors `engine/risk.py`: **the meter is the source of truth; the state is derived.** All reset on
date change in `_reset_session` (extend the existing reset).

```python
# realized P&L meter (already exists as _daily_realized_pnl — reuse it)
realized_pnl: float = 0.0                # net of modeled costs

# profit-lock
lock_armed: bool = False
profit_hi_water: float = 0.0            # max realized_pnl seen today
profit_floor: float = 0.0              # ratcheted defended level (only meaningful once armed)

# streak
consec_losses: int = 0                  # consecutive losing ladders
last_ladder_pnl: float = 0.0           # accumulator for the open ladder (reset on new ladder)

# counters (already exist — _trades_today, _cooldown_until)
trades_today: int = 0
cooldown_until: Optional[datetime] = None

# derived state (recomputed each tick — DO NOT persist as the source of truth)
state: str = "ACTIVE"   # ACTIVE | COOLDOWN | LOCKED | TRAIL_ONLY | STOOD_DOWN
```

**Restart-safety (the `engine/risk.py` lesson #2/#3).** This is PAPER intraday and the scalper's
ladder book is rebuilt from fills, but the *day's realized P&L and streak* must not silently reset
to zero on a mid-session restart, or the loss cap and profit-lock are defeated. On boot, the
governor should re-derive `realized_pnl`, `trades_today`, and `consec_losses` from the day's
intraday paper-trade log (the `fno_paper_trades`-analogue table flagged in `options_scalper.md`
§5b), exactly as `RiskEngine.refresh_pnl()` re-derives from `trades`. **Until that log exists, this
is a known gap: a restart resets the day's governor — document it loudly and treat a restart as a
deliberate "reset the day" action.** Do not pretend the meter is restart-proof when it isn't.

---

## 6. Integration — exactly how it gates `on_tick`

The governor is consulted at two points in the existing `on_tick` flow, **without touching** the
two inviolable rules above it (future-skew guard, unconditional EOD square-off).

### 6a. Tick order (in `OptionsScalper.on_tick`)

```
1. underlying_price ≤ 0            → None                       (existing)
2. future-skew guard               → None                       (existing, untouched)
3. session reset on date change    → governor reset too         (extend _reset_session)
4. UNCONDITIONAL EOD square-off    → EXIT all                    (existing, ABOVE governor)
5. ingest bar + compute direction                               (existing)
6. governor.update(now, realized_pnl)  ← recompute state        (NEW)
7. if tranches: _evaluate_exits(...)   ← EXITS NEVER BLOCKED     (existing; see 6c)
8. if governor.allows_new_risk():      ← gate entries           (NEW gate around existing entry)
       _evaluate_entry(...)
```

### 6b. The gate is a single predicate

Replace the scattered `_standing_down` / `max_trades` / `cooldown` early-returns in
`_evaluate_entry` with one call:

```python
def allows_new_risk(self, now) -> bool:
    if self.state in ("STOOD_DOWN", "TRAIL_ONLY", "COOLDOWN"):
        return False
    if self.trades_today >= self.p.max_trades:
        return False
    if self.cooldown_until and now < self.cooldown_until:
        return False
    # window check stays where it is (it gates entries on clock, not governor state)
    return True   # ACTIVE or LOCKED
```

`_evaluate_ladder_add` (new rungs) consults the **same** predicate plus "not TRAIL_ONLY/LOCKED-and-
defending" — a rung is new risk, so it is blocked whenever a new ladder would be. The cleanest rule:
**adds are allowed only in ACTIVE and LOCKED states**; never in TRAIL_ONLY, COOLDOWN, or STOOD_DOWN.

### 6c. EXITS ARE NEVER BLOCKED — the one rule the governor must obey

This is the `engine/risk.py` `check_intent` invariant (lines 264–273): a halt/lock/cooldown must
**never** swallow an exit, or the day ends with a naked position past close. Concretely:

- `_evaluate_exits` runs **before** the governor entry gate and is **never** governor-gated.
- TRAIL_ONLY / STOOD_DOWN affect **opening** risk only; the trail/TP/stop/time-stop/signal-flip and
  EOD square-off logic on open tranches is untouched.
- When the governor transitions to STOOD_DOWN with a position open, it emits a single flatten EXIT
  (reuse `_flatten_all`) — that EXIT is an *exit*, so it is allowed by definition.

### 6d. Feeding the streak + lock — where the meter updates

`notify_fill(side="SELL", ...)` already calls `_update_realized_pnl` and `_on_fully_flat`. Extend:

- In `_update_realized_pnl`: after updating `_daily_realized_pnl`, call `governor.on_realized(pnl)`
  so `profit_hi_water` / `profit_floor` ratchet on the realized step (not just on ticks).
- In `_on_fully_flat`: a ladder just closed → classify `last_ladder_pnl` as win/scratch/loss,
  advance/reset `consec_losses`, and if `consec_losses >= consec_loss_hard_stop` go STOOD_DOWN, else
  if `>= consec_loss_limit` set `cooldown_until = now + consec_loss_cooldown_min` (longer pause) and
  enter COOLDOWN. Reset `last_ladder_pnl` when the *next* ladder opens.
- `governor.update(now, realized)` on every tick recomputes `state` from the meter (the §2 diagram),
  so the loss cap and profit floor bind even on a tick with no fill.

### 6e. Live wiring (later, out of scope here)

When this strategy is eventually wired live (it is PAPER-only now), the governor's STOOD_DOWN /
flatten still routes through the platform `RiskEngine` and `PaperExecutor`/`LiveExecutor`; the
governor **proposes**, the `RiskEngine` **owns** the kill-switch (Safety rule 2). The governor's
profit-lock is a *strategy* concern; the `RiskEngine`'s equity-fraction loss halt is a *platform*
concern. Both run; neither is bypassed.

---

## 7. Worked day (concrete, default preset)

NIFTY scalper, defaults: `daily_loss_cap=8000`, `profit_lock_arm=6000`, `floor_frac=0.5`,
`giveback=2500`, `mode=trail_only`, `consec_loss_limit=3`, `consec_loss_cooldown_min=20`.

| Time | Event | realized | hi_water | floor | state | New ladders? |
|---|---|---|---|---|---|---|
| 09:30 | warm-up done, first ladder | 0 | 0 | — | ACTIVE | yes |
| 10:05 | ladder #1 +₹2,100 | +2,100 | 2,100 | — | ACTIVE | yes |
| 10:40 | ladder #2 −₹1,400 (loss 1) | +700 | 2,100 | — | ACTIVE | yes |
| 11:10 | ladder #3 +₹5,800 → **arm** (≥6,000) | +6,500 | 6,500 | max(3,000, 6,500−2,500)=**4,000** | LOCKED | yes |
| 12:00 | ladder #4 +₹3,200 | +9,700 | 9,700 | ratchet → **7,200** | LOCKED | yes |
| 13:15 | give-back: open tranche exits, realized → +7,100 | +7,100 | 9,700 | 7,200 | realized ≤ floor → **TRAIL_ONLY** | **no** |
| 13:15→ | existing tranches trail out only | … | | 7,200 | TRAIL_ONLY | no |
| 15:25 | EOD square-off (unconditional) | ~+7,1xx | | | flat | n/a |

Day ends **green at ~+₹7,100**, defended at the ₹7,200 floor (the small under-floor close is the
spread/slippage on the final trail exits — the floor binds on *new risk*, not on the cost of
walking out an already-open position; if you need the floor to be exact, use `mode="stop"`).

**A bad-day example (loss cap):** three losing ladders take realized to −₹8,050 at 11:50 → governor
hits `daily_loss_cap`, flattens, STOOD_DOWN. No more trades. Day closes ≈ −₹8,050 (the ₹50 overshoot
is the last stop's slippage past the cap — bounded, not unbounded). The day was *small and over*,
which is the entire point.

---

## 8. Unit-test cases (deterministic, pure — same contract as `options_scalper.py` §7)

Feed `(now, underlying, premium)` + `notify_fill` sequences; assert governor `state` and whether an
ENTER is emitted. No DB, no network.

1. **Loss cap → STOOD_DOWN.** Drive realized to `−daily_loss_cap` via SELL fills. Next qualifying
   LONG signal → `None`; state `STOOD_DOWN`. Assert an open book (if any) got one flatten EXIT.
2. **Profit-lock arms.** Realized crosses `profit_lock_arm` → `lock_armed=True`, `state=LOCKED`,
   `floor == arm × floor_frac`. New ladders still allowed (LOCKED is a trading state).
3. **Floor ratchets up.** After arm, push realized high-water up; assert `floor == hi_water −
   giveback` and that a *subsequent lower* high-water does **not** lower the floor.
4. **Floor break, trail_only.** `mode="trail_only"`; drop realized to `≤ floor` → `state=TRAIL_ONLY`;
   a fresh LONG signal → **no ENTER**; but an open tranche hitting its trail → EXIT still fires
   (exits never blocked).
5. **Floor break, stop.** `mode="stop"`; drop realized to `≤ floor` → one flatten EXIT, `STOOD_DOWN`,
   subsequent signals → `None`.
6. **Consecutive-loss cooldown.** `consec_loss_limit=3`: three losing ladders in a row → `COOLDOWN`
   with `cooldown_until = now + consec_loss_cooldown_min`; a signal inside that window → `None`; a
   win in between **resets** the streak (two losses, a win, two losses → no long cooldown).
7. **Consecutive-loss hard stop.** `consec_loss_hard_stop=5`: five losing ladders → `STOOD_DOWN`.
8. **Scratch is not a loss.** A ladder that closes at exactly 0 does not advance `consec_losses`.
9. **No-new-entry after T.** A qualifying signal at `CLOSE − no_trade_close_min + 1min` → `None`
   (window gate), independent of governor state.
10. **EOD square-off overrides everything.** Position open at `CLOSE − squareoff_before_close_min`
    while `STOOD_DOWN` / `TRAIL_ONLY` → still one flatten EXIT (rule above the governor).
11. **Exits never blocked under STOOD_DOWN.** Force STOOD_DOWN with a position open; a tick that
    would trigger a hard-stop on a tranche → EXIT still emitted.
12. **Session reset clears the day.** New `now.date()` → `state=ACTIVE`, meter/streak/lock all zero
    (the documented restart-resets-the-day caveat, §5).
13. **LOCKED still defends the absolute loss floor.** With lock armed at a low arm but a deep
    subsequent loss path, assert the defended level is `max(profit_floor, −daily_loss_cap)` (the
    loss cap is never undercut by lock math).

---

## 9. Hard rules (mirror `options_scalper.py` §6 + `engine/risk.py`)

1. **EOD square-off is unconditional and ABOVE the governor** — never gate it on governor state.
2. **Exits are never blocked** — STOOD_DOWN / TRAIL_ONLY / COOLDOWN gate *opening* risk only.
3. **The meter is the source of truth; state is derived every tick** — no edge-triggered state that
   a restart could desync (subject to the §5 restart caveat, which must be documented, not hidden).
4. **The loss cap is the absolute floor** — the profit-lock only ever raises the defended level
   above `−daily_loss_cap`, never below.
5. **PAPER only** — no live order path here; live routes through the platform `RiskEngine`, which
   owns the kill-switch and is never bypassed.
6. **One source of truth per day** — fold the existing `daily_loss_cap` / `max_trades` /
   `cooldown_min` / window fields into the governor; do not maintain two drifting copies.
7. **Honesty in the report** — any forward-paper or backtest summary must state the variance-
   reduction framing explicitly and must **not** claim a per-day green guarantee.
```
