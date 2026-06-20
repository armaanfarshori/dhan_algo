# 05 — Regime Filter for the Options Scalper (when NOT to scalp)

**Status:** PLAN — research only, PAPER. No live order paths. Layer ON TOP of the existing
scalper, not a rewrite.
**Targets:** `strategies/options_scalper.py` (`OptionsScalper`, `ScalperParams`, `direction_signal`).
**Companion data:** `core/fno_derived.py` (realized-vol series), `index_bars` (NIFTY 50 spot
`security_id="13"`, India VIX `security_id="21"`), the F&O event calendar.
**Sibling of:** `ml/fno_vol_gate.py` — but DISTINCT. That gate decides BUY/SELL/STAND-ASIDE **vol
premium** on a *daily* horizon for premium structures. This filter decides whether the *intraday
long-option scalper* should be **switched on at all today / right now**. Do not confuse the two.

---

## 0. Why a regime filter is the key EV lever

A long-option scalper is **structurally short theta and short the bid/ask spread** every round
trip. It is profitable only when intraday **movement** is large enough that the directional premium
gain on the winners outruns (a) theta bleed on the time-stopped flats and (b) the full cost stack
paid twice per scalp. Two regimes destroy it:

1. **Low-vol chop** — price glued to VWAP, small true ranges. The momentum trigger fires on noise,
   each scalp pays spread + cost, the time-stop bleeds theta, and nothing moves enough to hit even
   the first TP rung. This is the dominant killer of intraday long-premium.
2. **Event-risk gaps** — scheduled events (RBI policy, Union Budget, monthly expiry, US Fed,
   index-heavy earnings) produce gap/whipsaw regimes where IV is rich (you overpay for the option),
   intraday paths are discontinuous, and the scalper's tight stops get gapped through. Long premium
   *can* pay on a clean trend day, but the expected path is "buy rich IV → IV crush + chop."

The existing signal layer (§1 of `docs/fno_strategies/options_scalper.md`) already has a *micro*
activity floor (`min_atr_pts`) and a VWAP deadband. **The regime filter is the macro/session layer
above it:** it gates whole sessions and whole time-of-day windows ON only when the day's character
favors intraday movement, and OFF otherwise. Micro filter = "is this bar moving?"; regime filter =
"is today a day worth scalping, and is it the right part of the day?"

Net stance: **default OFF; switch ON only when ALL favorable conditions hold.** A scalper that
trades fewer, better sessions beats one that trades every day.

---

## 1. Where the filter plugs in (no new order path)

The filter is a **gate that suppresses NEW ladder opens** — it never forces an exit and never
fabricates a trade. Concretely it makes `_evaluate_new_ladder` return `None` when the regime is
unfavorable, exactly like the existing `_standing_down` / cooldown / `max_trades` guards.

```
on_tick
 └─ _evaluate_entry
     ├─ warm-up / trade-window / standing-down            (existing)
     ├─ regime_gate(now, session_ctx) == OFF  → return None   ← NEW (this spec)
     └─ _evaluate_new_ladder / _evaluate_ladder_add        (existing)
```

Hard invariants preserved:

- **EOD square-off and all EXIT logic are ABOVE the filter** — the regime gate can only stop *new*
  entries. An already-open ladder still trails / stops / time-stops / squares off normally. If the
  regime flips OFF mid-session, we stop adding rungs and stop opening ladders; we do **not**
  panic-flatten (let the position's own exits work).
- **Ladder ADDs:** a regime that goes OFF intra-day blocks new ladders but, by default, still
  allows the active ladder to complete its planned rungs (`add_blocked_when_off = False`). A
  stricter mode (`add_blocked_when_off = True`) also freezes pyramiding the moment the regime turns
  OFF. Default is the lenient one (don't starve a working trade).
- **Fail-open on missing data** (mirrors `ml/fno_vol_gate.py` and the Kronos gate): if a regime
  input is `None`/stale (no VIX, no realized-vol, calendar unavailable), the *vol-level* sub-gate
  defaults to **ALLOW** so a data outage never silently halts the strategy — EXCEPT the
  **event-day calendar**, which fails **CLOSED** (a missing calendar must not let us trade through
  an unflagged Budget day). See §3e.

---

## 2. The regime is the AND of independent sub-gates

`regime_gate(...) == ON` iff **every** enabled sub-gate passes. Each sub-gate is small, pure, and
independently testable. Defaults below are intraday-NIFTY-sane starting points to be swept in
forward paper (no historical intraday backtest exists — see §6).

| # | Sub-gate | Reads | ON when | Default param |
|---|---|---|---|---|
| A | **VIX floor** | India VIX (`index_bars` id `21`, prev close / live) | `vix >= vix_floor` | `vix_floor = 12.0` |
| B | **VIX ceiling** | India VIX | `vix <= vix_ceiling` | `vix_ceiling = 30.0` |
| C | **VIX regime band** | trailing VIX percentile/EWMA | not in bottom decile of its own recent range | `vix_pctile_floor = 0.20` |
| D | **Session realized-range floor** | intraday true-range accumulation | day's realized movement on pace to clear a floor | `day_range_floor_pts = 60.0` |
| E | **Open-drive vs chop classifier** | first `or_minutes` bars | opening range shows directional drive, not balance | see §3d |
| F | **Time-of-day windows** | `now.time()` | inside an allowed momentum window | see §3f |
| G | **Event-day calendar** | F&O event list | NOT a blocked event day (or only in restricted mode) | fail-CLOSED |

Each sub-gate can be individually disabled via its param (set to a no-op sentinel) so the forward
A/B can isolate each lever's contribution.

---

## 3. Sub-gate definitions (exact)

### 3a. VIX floor (A) — don't scalp a dead-vol tape

India VIX is the cheapest, most direct read on whether intraday movement is *available* at all.
Below ~12 the index typically grinds; theta + spread dominate and long premium is a losing game.

```
vix_ok_floor = (vix is None) or (vix >= p.vix_floor)      # fail-open if None
```

- **Source:** India VIX from `index_bars` `security_id="21"`. Live: the day's VIX print (or
  previous close as a proxy if the live VIX feed is absent — VIX is slow-moving day to day).
  Units: VIX is quoted in **percent** (e.g. 13.5 = 13.5% annualised) — compare in percent (do NOT
  divide by 100 here, unlike `fno_vol_gate`'s VIX path).
- **Default `vix_floor = 12.0`.** Sweep 10–14.

### 3b. VIX ceiling (B) — don't buy into a panic / event-rich IV

Very high VIX means options are expensive (you overpay) and the regime is gap-prone; a long-premium
*scalper* (tight stops, time-stops) gets chopped and IV-crushed. (A *premium-seller* would love
this — that is `fno_vol_gate`'s job, not ours.)

```
vix_ok_ceiling = (vix is None) or (vix <= p.vix_ceiling)  # fail-open if None
```

- **Default `vix_ceiling = 30.0`.** Sweep 25–35. Above the ceiling → scalper OFF for the day.

### 3c. VIX regime band (C) — relative, not just absolute

Absolute VIX thresholds drift with the long-run vol regime. Add a *relative* floor: VIX must not be
sitting in the **bottom `vix_pctile_floor` of its own trailing distribution** (e.g. trailing
60-session percentile, computed from `index_bars` id `21`). This catches "low for this regime"
chop even when absolute VIX is above `vix_floor`.

```
vix_ok_band = (vix_pctile is None) or (vix_pctile >= p.vix_pctile_floor)   # fail-open
```

- **Default `vix_pctile_floor = 0.20`, `vix_pctile_window = 60` sessions.** Disable by setting
  `vix_pctile_floor = 0.0`. Computed daily, pre-session (a session-level constant, not per-tick).

### 3d. Session realized-range floor (D) — is TODAY actually moving?

VIX is a forward expectation; this is the *realized* check. Accumulate the day's running movement
and require it to be on pace to clear a points floor before allowing entries.

Two equivalent expressions (pick one; `proj` is the default):

- **Cumulative since open:** running `session_high - session_low` of the NIFTY spot must exceed a
  time-scaled fraction of `day_range_floor_pts`:
  ```
  elapsed_frac = minutes_since_open / total_session_minutes      # 0..1
  range_ok = (session_high - session_low) >= p.day_range_floor_pts * max(elapsed_frac, p.min_elapsed_frac)
  ```
- **ATR-projected (proj):** scale the existing trailing `atr_window`-min ATR (already computed in
  `direction_signal`) to a full session and require the projection to clear the floor.

This reuses the bar window the scalper already maintains (`_highs`, `_lows`); no new feed.

- **Default `day_range_floor_pts = 60.0` NIFTY points, `min_elapsed_frac = 0.15`.** A NIFTY day
  that has not traveled ~60 points of range is not worth scalping long premium. Sweep 40–90.
- This is the **session-scale** complement to the existing per-bar `min_atr_pts` micro floor —
  keep both; they catch different failure modes (slow grind vs single dead bar).

### 3e. Open-drive vs chop classifier (E) — trend day vs balance day

The single biggest tell for a profitable long-premium scalp day is an **open drive**: price leaves
the opening range early and trends, versus a **balance/rotational** day that oscillates around the
open (death for tight-stop long premium). Classify from the first `or_minutes` bars:

```
or_high, or_low  = high/low of the first or_minutes bars (the opening range)
or_size          = or_high - or_low
post_or_move     = |close_at(or_end + confirm_min) - or_mid|     # directional follow-through
drive = (or_size >= p.or_min_pts) and (post_or_move >= p.drive_mult * or_size)
```

- **`drive == True`** → open-drive / trend bias → **favorable, gate E passes.**
- **`drive == False`** → balance / chop day → gate E **fails** (no new ladders) UNLESS a strict
  later momentum break re-qualifies the session (`rearm_on_break = True`): a clean break beyond the
  opening range by `rearm_break_pts` after the OR window can flip E back ON for the rest of the day.
- **Defaults:** `or_minutes = 15`, `or_min_pts = 20.0`, `drive_mult = 1.0`, `confirm_min = 10`,
  `rearm_on_break = True`, `rearm_break_pts = 25.0`.
- Reuses the same opening-range machinery the scalper already has for `signal="orb"` (`_orb_high`,
  `_orb_low`) — wire E to read those instead of duplicating the OR accumulation.

### 3f. Time-of-day windows (F) — scalp the momentum hours, skip the dead zone

Intraday NIFTY movement is front- and back-loaded. The lunch lull (≈ 11:30–13:15 IST) is low-range
chop; the open (first ~10 min) is auction noise; the last few minutes are square-off only.

Allow new ladders **only** inside the configured momentum windows:

```
MOMENTUM_WINDOWS (IST, default):
  09:30 – 11:15      morning trend window  (after warm-up, before lunch lull)
  13:15 – 15:00      afternoon trend window (after lunch, before square-off run-in)
```

```
tod_ok = any(start <= now.time() < end for (start, end) in p.momentum_windows)
```

- The **lunch lull 11:15–13:15 is OFF by default** (`block_lunch = True`).
- These windows sit **inside** the existing `no_trade_open_min` (15) / `no_trade_close_min` (20)
  guards and the EOD square-off — they are *narrower*, never wider. If a window edge conflicts with
  the existing guards, the **more restrictive** bound wins.
- Defaults are the start point; sweep the lull boundaries (some regimes trend through lunch).

### 3g. Event-day calendar (G) — fail CLOSED

Scheduled high-impact events distort intraday paths and IV. Maintain a small F&O event calendar
(dates + optional intraday windows) and gate the session:

- **Hard-block dates** (`block_full_day`): scalper OFF all day — e.g. **RBI MPC**, **Union
  Budget**, **monthly NIFTY expiry day** (last-Thursday gamma/pin risk), major **US FOMC** spillover
  days, scheduled **GDP/CPI** prints. Default: OFF the whole day.
- **Restricted dates** (`block_until` / `block_after`): trade only outside a window around the event
  (e.g. RBI speaks ~10:00 → block 09:30–11:00, allow the afternoon trend window once the dust
  settles). Optional, per-event.
- **FAIL CLOSED:** if the calendar source is missing/unreadable, gate G returns **OFF** (do not
  trade through an unknown event landscape). This is the ONE sub-gate that fails closed; all VIX/
  range gates fail open. Rationale: a missing VIX print is a tolerable degrade; trading through an
  unflagged Budget day is not.
- **Source:** a committed `events.yaml`/`events.json` (date → {type, scope, window}) the platform
  already needs for the F&O track; the live process loads it at session reset. No secrets.

---

## 4. `RegimeParams` (extension of `ScalperParams`)

Add a nested params block (or extend `ScalperParams`) so the backtest/forward sweep can toggle each
lever independently. Sentinels disable a sub-gate.

```python
from dataclasses import dataclass, field

@dataclass
class RegimeParams:
    enabled: bool = True                       # master switch (False = legacy behavior)

    # A/B — absolute VIX band (percent units)
    vix_floor: float = 12.0                    # 0.0 disables
    vix_ceiling: float = 30.0                  # large value (e.g. 999) disables

    # C — relative VIX band
    vix_pctile_floor: float = 0.20             # 0.0 disables
    vix_pctile_window: int = 60                # trailing sessions

    # D — session realized-range floor (NIFTY points)
    day_range_floor_pts: float = 60.0          # 0.0 disables
    min_elapsed_frac: float = 0.15
    range_mode: str = "proj"                   # "proj" | "cumulative"

    # E — open-drive vs chop
    or_minutes: int = 15
    or_min_pts: float = 20.0
    drive_mult: float = 1.0
    confirm_min: int = 10
    rearm_on_break: bool = True
    rearm_break_pts: float = 25.0
    require_open_drive: bool = True            # False disables gate E

    # F — time-of-day windows (IST)
    momentum_windows: list = field(default_factory=lambda: [
        ("09:30", "11:15"),
        ("13:15", "15:00"),
    ])
    block_lunch: bool = True                   # informational; encoded by the windows above

    # G — event calendar (FAIL CLOSED)
    event_calendar_path: str = "config/fno_events.yaml"
    block_on_missing_calendar: bool = True     # True = fail closed (do NOT set False lightly)

    # ladder-add behavior when regime flips OFF mid-session
    add_blocked_when_off: bool = False         # False = let an open ladder finish its rungs
```

Disable sentinels (for the A/B isolation runs): `vix_floor=0.0`, `vix_ceiling=999.0`,
`vix_pctile_floor=0.0`, `day_range_floor_pts=0.0`, `require_open_drive=False`,
`momentum_windows=[("09:15","15:30")]`. With all disabled, behavior is byte-identical to the
pre-filter scalper (regression guard).

---

## 5. Pure helper + integration

A single pure, IO-free classifier mirrors `direction_signal` — DB-free, unit-testable with
hand-fed inputs. The DB/feed glue (fetch VIX, load calendar) is the caller's job and is passed in
as a small context object so the classifier never touches a DB.

```python
@dataclass(frozen=True)
class RegimeContext:
    vix: Optional[float]              # India VIX percent (live or prev close); None ok
    vix_pctile: Optional[float]      # trailing percentile 0..1; None ok
    session_high: float              # running NIFTY spot high since open
    session_low: float               # running NIFTY spot low since open
    minutes_since_open: float
    or_high: float                   # opening-range high (gate E)
    or_low: float                    # opening-range low
    post_or_move: float              # follow-through magnitude (gate E)
    is_event_block_day: Optional[bool]   # None => calendar missing => FAIL CLOSED
    now_time: dtime

def regime_on(ctx: RegimeContext, p: RegimeParams) -> tuple[bool, str]:
    """Return (ON?, reason). Pure. Fail-open on VIX/range Nones; fail-CLOSED on
    missing calendar. reason names the FIRST failing sub-gate (for logging/shadow)."""
    if not p.enabled:
        return True, "regime-disabled"
    # G — event calendar, FAIL CLOSED
    if ctx.is_event_block_day is None:
        if p.block_on_missing_calendar:
            return False, "event-calendar-missing (fail-closed)"
    elif ctx.is_event_block_day:
        return False, "event-day-block"
    # A/B/C — VIX (fail-open on None)
    if ctx.vix is not None:
        if p.vix_floor > 0 and ctx.vix < p.vix_floor:
            return False, f"vix<{p.vix_floor}"
        if ctx.vix > p.vix_ceiling:
            return False, f"vix>{p.vix_ceiling}"
    if ctx.vix_pctile is not None and ctx.vix_pctile < p.vix_pctile_floor:
        return False, f"vix-pctile<{p.vix_pctile_floor}"
    # F — time of day
    if not _in_windows(ctx.now_time, p.momentum_windows):
        return False, "outside-momentum-window"
    # D — session realized range
    if p.day_range_floor_pts > 0:
        frac = max(ctx.minutes_since_open / 375.0, p.min_elapsed_frac)
        if (ctx.session_high - ctx.session_low) < p.day_range_floor_pts * frac:
            return False, "range-floor"
    # E — open-drive vs chop
    if p.require_open_drive:
        or_size = ctx.or_high - ctx.or_low
        drive = or_size >= p.or_min_pts and ctx.post_or_move >= p.drive_mult * or_size
        if not drive:
            return False, "no-open-drive"
    return True, "regime-on"
```

Integration in `_evaluate_entry` (right after the existing standing-down check, before
`_evaluate_new_ladder`):

```python
on, why = regime_on(self._build_regime_ctx(now), self.rp)
if not on:
    if not self._tranches or self.rp.add_blocked_when_off:
        logger.debug("[OptionsScalper %s] regime OFF (%s) — no new entries", self.security_id, why)
        return None
```

**Shadow-first rollout (mandatory, mirrors the Kronos gate):** ship the filter in **shadow mode**
(`shadow=True`) where it **logs** `[REGIME-SHADOW] would BLOCK (reason)` / `would ALLOW` and records
the verdict, but does **not** actually suppress entries. Collect ≥ N favorable/blocked sessions in
forward paper, compare block-vs-allow EV, then flip to enforcing. Never enable enforcing on day one.

---

## 6. Data sources & feasibility

| Input | Source today | Status |
|---|---|---|
| India VIX (level + history) | `index_bars` `security_id="21"` (daily) | **available** (EOD); live intraday VIX feed is a small gap |
| VIX trailing percentile | computed from `index_bars` id `21` | **available** (daily, pre-session) |
| NIFTY spot intraday range / OR / drive | live NIFTY 1-min feed | needs the **intraday NIFTY feed** the scalper already requires (see `options_scalper.py` data notice) |
| Realized-vol context | `core/fno_derived.realized_vol_series` on id `13` | **available** (daily) |
| Event calendar | committed `config/fno_events.yaml` | **to be authored** (small, manual, FAIL CLOSED until present) |

- The **VIX, realized-vol, and percentile** sub-gates (A/B/C) are computable **today** from
  `index_bars` and can be validated daily without any intraday feed — they are session-level
  constants decided at session reset.
- The **range / open-drive / time-of-day** sub-gates (D/E/F) ride entirely on the intraday NIFTY
  spot bar window the scalper *already* maintains — **no new feed** beyond what the scalper needs.
- As with the scalper itself, **there is no faithful historical intraday backtest** (no intraday
  option premiums, no guaranteed intraday index path — see the data-feasibility notice atop
  `strategies/options_scalper.py`). Validate this filter in **forward paper, shadow-first**, and
  sweep the params there. Do **not** fabricate an intraday path from daily bars to "backtest" it.

---

## 7. Unit-test cases (pure `regime_on`, hand-fed `RegimeContext`)

1. **All favorable → ON.** vix=15, pctile=0.5, range cleared, open-drive True, 10:00 IST, not event
   day → `(True, "regime-on")`.
2. **VIX floor blocks.** vix=10 (< 12), else favorable → `(False, "vix<12.0")`.
3. **VIX ceiling blocks.** vix=35 (> 30) → `(False, "vix>30.0")`.
4. **VIX percentile blocks.** vix=15 but pctile=0.05 (< 0.20) → `(False, "vix-pctile<0.2")`.
5. **VIX None → fail-open** (A/B/C skipped); other gates decide.
6. **Lunch lull blocks.** now=12:00, all else favorable → `(False, "outside-momentum-window")`.
7. **Range floor blocks.** session range 20 pts at 30% elapsed vs 60-pt floor → `(False,
   "range-floor")`.
8. **No open drive blocks.** or_size 8 < or_min_pts 20 → `(False, "no-open-drive")`.
9. **Event day blocks.** is_event_block_day=True → `(False, "event-day-block")` (dominates even
   with everything else favorable).
10. **Calendar missing → FAIL CLOSED.** is_event_block_day=None, block_on_missing_calendar=True →
    `(False, "event-calendar-missing (fail-closed)")`.
11. **enabled=False → always ON** (`"regime-disabled"`) — legacy/regression guard.
12. **All sub-gates disabled (sentinels) → ON regardless** of marginal inputs (proves the A/B
    isolation path and the byte-identical-to-legacy claim).
13. **Mid-session OFF with open ladder, `add_blocked_when_off=False`** → integration test: open
    ladder still adds rungs; new ladders blocked. With `True`: adds also blocked.

---

## 8. Summary — the ON switch

> **Scalp ONLY when:** VIX is in a live-but-not-panic band (≈12–30) and not bottom-decile for its
> regime; today's NIFTY has shown an **open drive** (trend, not balance) and realized range is on
> pace to clear ~60 pts; the clock is in a **momentum window** (09:30–11:15 or 13:15–15:00, never
> the lunch lull); and it is **not** a scheduled event day. Otherwise the scalper is **OFF** — the
> single highest-leverage decision for positive EV in long-premium intraday is *not trading the
> chop and the event gaps.*
