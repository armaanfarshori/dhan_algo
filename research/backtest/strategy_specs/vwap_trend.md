# Strategy spec — VWAP Trend (trend-following around session VWAP)

Target file: `strategies/vwap_trend.py`. Implements the same pure, synchronous, IO-free interface
as `strategies/orb.py` (`on_tick → Optional[Decision]`, `notify_fill`, `notify_flat`). Reuse
`Decision` from `strategies/orb.py` — **do not redefine it**. Also reuse the module constants
`IST`, `MARKET_OPEN`, `MARKET_CLOSE`, `MAX_FUTURE_SKEW` (import from `strategies.orb`) to stay
byte-identical with ORB's session/future-skew handling.

This strategy trades **WITH the intraday trend** as defined by session VWAP and its slope:

- **Long** when price is **above a rising VWAP**, pulls **back to / touches** VWAP, then **holds
  or reclaims** it (a pullback-continuation entry, not a breakout chase).
- **Short** when price is **below a falling VWAP**, **rallies into** VWAP, then **rejects** from
  it (price fails to reclaim VWAP and turns back down).

It is a **signal-exit** strategy: the primary exit is the bar **closing back through VWAP against
the position** (a long exits when close < VWAP; a short exits when close > VWAP). A protective
stop sits a buffer **beyond VWAP** on the wrong side (contract rule 4 — the engine's intrabar wick
check needs a concrete `stop`). `target` is set to a far "no-target" level so the engine's target
arm never fires; all real exits come from `on_tick` (VWAP cross-back, stop-close, or EOD).

> ### Not the mean-reversion variant
> The VWAP **mean-reversion** strategy fades *extension away from* VWAP (buys when price is far
> *below* VWAP expecting a snap back *up to* it; sells when far *above*). This **trend** variant is
> the opposite stance: it requires price on the **trend side** of a **sloped** VWAP and enters on
> a **pullback that holds**, riding the move **further away** from VWAP in the trend direction. The
> two must never be confused in code or tests: here a long needs `price > VWAP` and `slope > 0`,
> there a long needs `price << VWAP`. Keep the names distinct: this file = `vwap_trend`, the other
> = `vwap_mr`.

---

## 1. Indicator — session VWAP, computed incrementally (REQUIRES VOLUME)

Session VWAP at bar `t` is the volume-weighted average of the typical price over **all bars since
the session open**:

```
typical_t = (high_t + low_t + close_t) / 3          # if high/low absent, use close
vwap_t     = Σ(typical_i * volume_i) / Σ(volume_i)   # i = first session bar … t
```

Maintained with two running accumulators (reset each session):

```
self._cum_pv  += typical_t * volume_t     # Σ price·volume
self._cum_vol += volume_t                 # Σ volume
vwap_t = self._cum_pv / self._cum_vol     # undefined until _cum_vol > 0
```

### 1a. VWAP slope (direction) — incremental, lag-`slope_lag` difference

Trend direction is the sign of the recent VWAP change. Keep a fixed-size history of the last
`slope_lag + 1` VWAP values (a `collections.deque(maxlen=slope_lag + 1)`):

```
self._vwap_hist.append(vwap_t)
if len(self._vwap_hist) <= slope_lag:
    slope = None                                   # not enough history yet
else:
    prev = self._vwap_hist[0]                       # vwap from slope_lag bars ago
    slope = (vwap_t - prev) / prev                  # fractional change over the window
```

- `slope > slope_eps`  → **rising** VWAP (uptrend bias) — only longs eligible.
- `slope < -slope_eps` → **falling** VWAP (downtrend bias) — only shorts eligible.
- `|slope| <= slope_eps` → **flat** VWAP — no new entries (avoids churn in directionless tape).

`slope_eps` is a small fractional floor (default `0.0` disables the flat filter; a sane non-zero
default is given in §4). Using a *fractional* (÷prev) slope makes `slope_eps` price-independent so
one threshold works across ₹50 and ₹5000 names.

### 1b. Pullback / rejection state machine (this is what makes it "trend pullback", not a chase)

Per session, track the relationship of **close to VWAP** to detect a pullback that *touched* VWAP
and then *held/reclaimed* it (long), or a rally that *touched* VWAP and *rejected* (short).

State variable `self._zone` ∈ {`"NONE"`, `"PULLBACK_LONG"`, `"PULLBACK_SHORT"`}:

**Long pullback (only when `slope > slope_eps`):**
1. Arm: a bar whose **low ≤ VWAP ≤ prior closes were above** — i.e. price *pulls back into* VWAP
   from above. Concretely: previous bar `close_{t-1} > vwap_{t-1}` (we were above) AND this bar's
   `low_t <= vwap_t * (1 + touch_band_pct)` (it dipped to/through VWAP). Set
   `self._zone = "PULLBACK_LONG"`.
2. Trigger (entry): on a **subsequent** bar (can be the same conditions resolving next bar) the
   close **reclaims/holds** VWAP: `close_t >= vwap_t * (1 + reclaim_band_pct)`. → ENTER BUY.
   - If instead the close breaks decisively **below** VWAP
     (`close_t < vwap_t * (1 - invalidate_band_pct)`), the pullback failed → reset
     `self._zone = "NONE"` (the uptrend thesis is voided; do not enter).

**Short rejection (only when `slope < -slope_eps`):**
1. Arm: previous bar `close_{t-1} < vwap_{t-1}` (we were below) AND this bar's
   `high_t >= vwap_t * (1 - touch_band_pct)` (it rallied to/through VWAP). Set
   `self._zone = "PULLBACK_SHORT"`.
2. Trigger (entry): a subsequent close **rejects** VWAP: `close_t <= vwap_t * (1 - reclaim_band_pct)`.
   → ENTER SELL.
   - If the close breaks decisively **above** VWAP (`close_t > vwap_t * (1 + invalidate_band_pct)`),
     the downtrend thesis is voided → reset `self._zone = "NONE"`.

Notes:
- The "arm then trigger" split means we do not enter the instant price merely touches VWAP — we
  wait for the bar that confirms the hold/reject, which is the trend-pullback discipline.
- A single bar **can** both arm and trigger if its low touches VWAP and its close reclaims with
  margin (gap-style pullback): evaluate arm first, then trigger, within the same `on_tick`.
- `self._zone` is cleared to `"NONE"` on any ENTER (consumed), on invalidation, and on entering a
  position (you never re-arm while holding).
- Track `self._prev_close` and `self._prev_vwap` (the prior bar's close & VWAP) to evaluate the
  "we were above/below" arming condition. Update them at the END of every processed bar.

> Bars feed VWAP via `typical = (high+low+close)/3`. The slope and zone machine read the same
> per-bar VWAP. One `on_tick` = one bar.

---

## 2. The VOLUME problem — required engine/interface change (READ CAREFULLY)

> **FINAL (shipped) interface — supersedes the `on_bar_volume` design below:**
> The standardized `on_tick` signature is
> `on_tick(now, price, high=None, low=None, volume=None)`, and the backtest engine passes the bar's
> volume **directly** into `on_tick`
> (`engine.py` step 3: `strategy.on_tick(ts, bar_close, high=bar_high, low=bar_low, volume=float(bar["volume"]))`).
> There is **NO** separate `on_bar_volume()` / `notify_bar()` setter — the optional `volume` kwarg
> on `on_tick` is the one and only volume path, and ORB/EMA/etc. simply ignore it (no regression).
> Strategies that need prior-day levels expose `seed_prior_day(prior_high, prior_low, prior_close)`,
> called positionally by the engine before the first `on_tick` of each session.
>
> The historical "Chosen design" below (a `hasattr`-gated `on_bar_volume` setter) was **NOT** the
> form that shipped; it is retained only as design rationale.

`on_tick(self, now, price, high=None, low=None)` originally **did not pass volume**, but VWAP is
undefined without it — the one genuine interface gap versus ORB/EMA. It was resolved by adding the
optional `volume` kwarg to the shared `on_tick` signature (see the FINAL note above); ORB and every
other strategy accept and ignore it.

### (Historical) design considered: a separate `on_bar_volume()` setter invoked *before* `on_tick`

> Superseded — kept for the record only. The shipped code uses the `volume=` kwarg on `on_tick`.

The originally-proposed alternative added ONE optional method to the strategy and had each driver
push the bar's volume just before the existing `on_tick` call:

```python
# (NOT shipped) strategies/vwap_trend.py
def on_bar_volume(self, volume: float) -> None:
    """Caller pushes the just-closed bar's volume immediately BEFORE on_tick."""
    self._pending_volume = float(volume) if volume and volume > 0 else 0.0
```

This was rejected in favour of the `volume=` kwarg, which threads one bar's data through a single
call (no ordering fragility) and keeps ORB byte-for-byte unchanged via the optional default.

---

## 3. Entry rule (exact) → `Decision`

Evaluated on each bar close, only when flat (`self.position == 0`), VWAP defined
(`self._cum_vol > 0`), slope available (`slope is not None`), and `now.time()` within the tradable
window `[entry_start, eod_squareoff)` (see §6; EOD takes precedence). Run the §1b state machine to
update `self._zone` first, then:

- **Long trigger** (slope rising, `self._zone == "PULLBACK_LONG"` armed, and this bar reclaims:
  `close >= vwap * (1 + reclaim_band_pct)`):
  ```
  stop   = vwap * (1 - sl_buffer_pct)                  # buffer BELOW VWAP
  target = close * (1 + far_target_mult)               # far "no-target" arm
  Decision(action="ENTER", side="BUY", stop=stop, target=target,
           reason=f"VWAP-trend long pullback  vwap={vwap:.2f} slope={slope:+.4f}")
  ```
- **Short trigger** (slope falling, `self._zone == "PULLBACK_SHORT"` armed, and this bar rejects:
  `close <= vwap * (1 - reclaim_band_pct)`):
  ```
  stop   = vwap * (1 + sl_buffer_pct)                  # buffer ABOVE VWAP
  target = max(close * (1 - far_target_mult), 0.05)    # clamp > 0
  Decision(action="ENTER", side="SELL", stop=stop, target=target,
           reason=f"VWAP-trend short rejection  vwap={vwap:.2f} slope={slope:+.4f}")
  ```

On emitting an ENTER: set `self._stop_level` to the stop (used by the close-based stop in §4),
set `self._zone = "NONE"` (consumed). Do **not** assume the ENTER executed — `self.position` only
changes via `notify_fill`.

Optional **minimum distance-from-VWAP-at-trigger floor is intentionally NOT used** — the whole
point is to enter *near* VWAP on the pullback. Trend quality is governed by `slope_eps` and the
band params, not by distance from VWAP.

---

## 4. Exit rules (exact, priority order inside `on_tick`)

Checked only when `self.position != 0`. **Return the first that fires:**

1. **EOD square-off (unconditional, top of method)** — see §6. Highest priority; must not depend on
   VWAP being defined (mid-session restart may have no accumulators yet).
2. **VWAP cross-back signal exit** (the core trend exit):
   - long open + `close < vwap * (1 - vwap_exit_band_pct)` →
     `Decision(action="EXIT", reason="VWAP cross-back (long)")`
   - short open + `close > vwap * (1 + vwap_exit_band_pct)` →
     `Decision(action="EXIT", reason="VWAP cross-back (short)")`
   - (If VWAP is undefined here — only possible after a restart with no bars — skip this rule and
     rely on the stop / EOD. Guard with `if self._cum_vol > 0`.)
3. **Protective stop on close** — `self._stop_level` breached by the bar close:
   - long: `close <= self._stop_level` → `Decision(action="EXIT", reason="Stop-loss ₹{lvl:.2f}")`
   - short: `close >= self._stop_level` → `Decision(action="EXIT", reason="Stop-loss ₹{lvl:.2f}")`
   - Close-based backstop; the engine's intrabar wick check uses the same `stop` level passed at
     ENTER, so they agree (mirrors ORB §4 and the EMA spec).

If none fire, return `None`. **No fixed profit target** by design — the strategy rides the trend
until price closes back through VWAP (rule 2) or the stop. The far `target` exists only to satisfy
the Decision contract / engine arm.

> Note the asymmetry that makes this *trend*-following: the exit (rule 2) fires when price returns
> *to* VWAP against the position, i.e. the trend stalled. The mean-reversion variant would do the
> opposite (its *target* is VWAP). Do not copy this exit into `vwap_mr`.

---

## 5. `VwapTrendParams` dataclass

```python
from dataclasses import dataclass

@dataclass
class VwapTrendParams:
    slope_lag: int = 5                  # bars back for the VWAP-slope difference
    slope_eps: float = 0.0002           # fractional slope floor for "rising"/"falling" (0 = off)
    touch_band_pct: float = 0.0010      # how close to VWAP a wick must come to "touch" (arm)
    reclaim_band_pct: float = 0.0005    # margin past VWAP a close needs to confirm hold/reject
    invalidate_band_pct: float = 0.0015 # margin past VWAP (wrong side) that voids an armed zone
    vwap_exit_band_pct: float = 0.0005  # margin past VWAP (wrong side) for the cross-back EXIT
    sl_buffer_pct: float = 0.0030       # protective stop distance beyond VWAP
    far_target_mult: float = 0.50       # "no-target" arm: ±50% from entry (unreachable intraday)
    entry_start_min: int = 15           # no entries in the first N min (let VWAP/slope form)
    squareoff_before_close_min: int = 15
```

Defaults are intraday-sane for 1-min NSE equities: a 5-bar slope, tiny fractional bands (5–15 bps)
so the touch/reclaim/exit logic is robust to single-tick noise around VWAP, a 0.30% stop beyond
VWAP, no profit target, a 15-min open delay (VWAP is meaningless on the first few bars and a
5-bar slope needs ≥6 bars), and the same 15-min EOD square-off as ORB. The backtest sweep can vary
`slope_lag`, `slope_eps`, the band params, and `sl_buffer_pct`.

---

## 6. `on_tick` skeleton (control flow)

```python
def on_tick(self, now, price, high=None, low=None, volume=None) -> Optional[Decision]:
    if price <= 0:
        return None

    # future-skew guard — COPY ORB exactly (compare in IST, naive/aware tolerant)
    wall = datetime.now(IST)
    ref = wall if now.tzinfo is not None else wall.replace(tzinfo=None)
    if now - ref > MAX_FUTURE_SKEW:
        return None

    today, t = now.date(), now.time()
    if self._session_date != today:
        self._reset_session(today)

    # 1. EOD square-off — UNCONDITIONAL, before any VWAP-readiness gate
    squareoff = (datetime.combine(today, MARKET_CLOSE)
                 - timedelta(minutes=self.p.squareoff_before_close_min)).time()
    if t >= squareoff:
        if self.position != 0:
            return Decision(action="EXIT", reason="EOD square-off")
        return None

    # 2. read this bar's volume (passed directly into on_tick), update VWAP accumulators
    vol = float(volume) if volume is not None and volume > 0 else 0.0
    hi = high if high is not None else price
    lo = low  if low  is not None else price
    typical = (hi + lo + price) / 3.0
    if vol > 0:
        self._cum_pv  += typical * vol
        self._cum_vol += vol
    if self._cum_vol <= 0:
        return None                                  # warm-up: no volume yet
    vwap = self._cum_pv / self._cum_vol

    # 3. slope
    self._vwap_hist.append(vwap)
    slope = None
    if len(self._vwap_hist) > self.p.slope_lag:
        prev = self._vwap_hist[0]
        if prev > 0:
            slope = (vwap - prev) / prev

    # snapshot prior-bar values BEFORE we overwrite them at the end
    prev_close, prev_vwap = self._prev_close, self._prev_vwap

    # 4. EXITS first (position open)
    if self.position > 0:
        result = None
        if close_back := (price < vwap * (1 - self.p.vwap_exit_band_pct)):
            result = Decision(action="EXIT", reason="VWAP cross-back (long)")
        elif price <= self._stop_level:
            result = Decision(action="EXIT", reason=f"Stop-loss ₹{self._stop_level:.2f}")
        self._prev_close, self._prev_vwap = price, vwap     # ALWAYS advance prior state
        return result
    if self.position < 0:
        result = None
        if price > vwap * (1 + self.p.vwap_exit_band_pct):
            result = Decision(action="EXIT", reason="VWAP cross-back (short)")
        elif price >= self._stop_level:
            result = Decision(action="EXIT", reason=f"Stop-loss ₹{self._stop_level:.2f}")
        self._prev_close, self._prev_vwap = price, vwap
        return result

    # 5. ENTRIES (flat) — need slope, window, prior bar
    decision = None
    entry_start = (datetime.combine(today, MARKET_OPEN)
                   + timedelta(minutes=self.p.entry_start_min)).time()
    if (slope is not None and t >= entry_start and prev_vwap is not None):
        # --- arm the zone from the pullback/rally into VWAP ---
        if slope > self.p.slope_eps:                  # uptrend bias → look for long pullback
            if prev_close > prev_vwap and lo <= vwap * (1 + self.p.touch_band_pct):
                self._zone = "PULLBACK_LONG"
            if self._zone == "PULLBACK_LONG":
                if price < vwap * (1 - self.p.invalidate_band_pct):
                    self._zone = "NONE"               # pullback failed
                elif price >= vwap * (1 + self.p.reclaim_band_pct):
                    self._stop_level = vwap * (1 - self.p.sl_buffer_pct)
                    decision = Decision(
                        action="ENTER", side="BUY",
                        stop=self._stop_level,
                        target=price * (1 + self.p.far_target_mult),
                        reason=f"VWAP-trend long pullback vwap={vwap:.2f} slope={slope:+.4f}")
                    self._zone = "NONE"
        elif slope < -self.p.slope_eps:               # downtrend bias → look for short rejection
            if prev_close < prev_vwap and hi >= vwap * (1 - self.p.touch_band_pct):
                self._zone = "PULLBACK_SHORT"
            if self._zone == "PULLBACK_SHORT":
                if price > vwap * (1 + self.p.invalidate_band_pct):
                    self._zone = "NONE"
                elif price <= vwap * (1 - self.p.reclaim_band_pct):
                    self._stop_level = vwap * (1 + self.p.sl_buffer_pct)
                    decision = Decision(
                        action="ENTER", side="SELL",
                        stop=self._stop_level,
                        target=max(price * (1 - self.p.far_target_mult), 0.05),
                        reason=f"VWAP-trend short rejection vwap={vwap:.2f} slope={slope:+.4f}")
                    self._zone = "NONE"
        else:
            self._zone = "NONE"                       # flat VWAP → no setup

    # 6. advance prior-bar state and return
    self._prev_close, self._prev_vwap = price, vwap
    return decision
```

`notify_fill` / `notify_flat`: copy ORB verbatim (update `self.position`/`self.entry_price`).
`notify_flat` may also set `self._zone = "NONE"` defensively; `self._stop_level` is only read while
`position != 0`, so it can be left as-is.

> Implementation note: keep ONE place that advances `self._prev_close/_prev_vwap` at the very end of
> the method (the skeleton above advances inside each branch *for clarity*; the cleaner refactor is a
> single advance just before every `return` after VWAP is computed — but NOT before the warm-up
> returns in steps 1–2, where `vwap` is undefined). Ensure `_prev_*` is advanced on **every** path
> that has a valid `vwap`, so the "we were above/below" arming check always reflects the immediately
> prior bar.

---

## 7. Session reset list (`_reset_session(today)`)

On every date change (and at construction), reset ALL intraday state — no cross-session leakage:

- `self._session_date = today`
- VWAP accumulators: `self._cum_pv = 0.0`, `self._cum_vol = 0.0`
- slope history: `self._vwap_hist = collections.deque(maxlen=self.p.slope_lag + 1)`
- prior-bar state: `self._prev_close = None`, `self._prev_vwap = None`
- zone machine: `self._zone = "NONE"`
- stop level: `self._stop_level = 0.0`
- pending volume: `self._pending_volume = 0.0`

Position state (`self.position`, `self.entry_price`) is **not** reset here — owned by
`notify_fill`/`notify_flat`; EOD square-off flattens it the same session anyway.

Window/EOD constants reused from `strategies.orb`: `MARKET_OPEN = 09:15`, `MARKET_CLOSE = 15:30`,
`MAX_FUTURE_SKEW = 2 min`.

---

## 8. Unit-test cases (input bar sequence → expected Decision)

All times IST on a single trading day (e.g. 2024-03-01). Each bar is fed by a single
`strat.on_tick(ts, close, high, low, volume=vol)` call (the engine passes bar volume directly into
`on_tick`). Use small, hand-checkable params unless a case overrides:
`VwapTrendParams(slope_lag=2, slope_eps=0.0, entry_start_min=0, touch_band_pct=0.002,
reclaim_band_pct=0.0, invalidate_band_pct=0.005, vwap_exit_band_pct=0.0, sl_buffer_pct=0.01,
far_target_mult=0.50)`. With `reclaim_band_pct=0` and `vwap_exit_band_pct=0`, triggers/exits fire on
a clean cross; `slope_eps=0` means any positive/negative slope counts. Drive
`notify_fill`/`notify_flat` between bars to simulate the engine confirming fills.

**T1 — warm-up: no volume → None.**
Feed 3 bars with `on_tick(..., volume=0)` (or omit the kwarg entirely). `_cum_vol`
stays 0 → every call returns `None` (VWAP undefined). Proves the zero-volume fail-safe.

**T2 — VWAP accumulates correctly.**
Feed bar1 close=100,high=100,low=100,vol=1000; bar2 close=102,h=102,l=102,vol=1000. After bar2,
assert internal VWAP == (100*1000 + 102*1000)/2000 == 101.0 (typical = close when h=l=close). No
entry yet (slope needs `slope_lag+1`=3 VWAP points). Both `on_tick` → `None`.

**T3 — rising VWAP + pullback that holds → ENTER BUY.**
Engineer rising VWAP over ≥4 bars (ascending typicals so slope>0), with price comfortably above
VWAP. Then a bar dips its **low to VWAP** (`low <= vwap*(1+touch_band)`) while prior close was
above VWAP → arms `PULLBACK_LONG`. Next bar closes back **above** VWAP (`close >= vwap`) → expect
`Decision(action="ENTER", side="BUY")`, `stop ≈ vwap*(1-0.01)` (below VWAP), `target ≈ close*1.50`,
reason contains "long pullback". Assert `stop < vwap < close < target`.

**T4 — falling VWAP + rally into VWAP that rejects → ENTER SELL.**
Mirror of T3: descending typicals so slope<0, price below VWAP; a bar's **high reaches VWAP**
(prior close below) → arms `PULLBACK_SHORT`; next bar closes **below** VWAP → expect
`Decision(action="ENTER", side="SELL")`, `stop ≈ vwap*(1+0.01)` (above VWAP), reason contains
"short rejection". Assert `target < close < vwap < stop`.

**T5 — flat VWAP blocks entry.**
With `slope_eps=0.001`, feed bars whose VWAP barely moves (`|slope| <= slope_eps`). Even if a
clean pullback-and-reclaim pattern occurs, expect `None` (no ENTER) — `self._zone` is forced to
`"NONE"` in the flat branch. Confirms the trend filter.

**T6 — pullback invalidation (failed continuation) → no entry.**
Set up `PULLBACK_LONG` armed (rising VWAP, low touched VWAP), then the next close breaks
**below** VWAP by more than `invalidate_band_pct` → `self._zone` resets to `"NONE"`, returns
`None`. A later genuine reclaim with a freshly re-armed zone is required to enter (assert the
strategy did NOT enter on the breakdown bar).

**T7 — VWAP cross-back exits an open long.**
Run T3 to ENTER BUY, then `notify_fill("BUY", qty, fill_px)`. Feed a bar whose **close falls
below VWAP** (`close < vwap`) while still above the stop. Expect
`Decision(action="EXIT", reason="VWAP cross-back (long)")` — and NOT a new SELL ENTER that bar.

**T8 — protective stop on close exits a short (no VWAP cross yet).**
After a SELL ENTER + `notify_fill`, feed a bar whose **close ≥ stop level** (price rallied past the
buffer above VWAP) but whose close is still on the short side relative to the `vwap_exit_band`
check ordering — engineer so the stop fires. Expect `Decision(action="EXIT", reason="Stop-loss ₹…")`.
(If both the cross-back and stop conditions hold, the cross-back rule has priority — document the
chosen ordering and test it: feed a bar that triggers cross-back only, then one that triggers stop
only.)

**T9 — EOD square-off is unconditional and dominates.**
With an open position (`notify_fill` done), call `on_tick` at 15:16 (≥ 15:30 − 15 min). Expect
`Decision(action="EXIT", reason="EOD square-off")` regardless of VWAP/slope state — even with
`_cum_vol == 0`. Also: same time, flat position → `None`.

**T10 — session reset clears VWAP across a date change.**
Fully accumulate VWAP + slope on day 1 (feed enough bars to enable entries). Then on day 2 at
09:16 feed one bar → must return `None` (warm-up again: `_cum_vol` reset, slope history empty),
proving `_reset_session` wiped accumulators, `_vwap_hist`, `_prev_*`, and `_zone`. Replaying day-1's
sequence on day 2 reproduces the day-1 behavior (no leakage).

**T11 — entry_start delay blocks an early valid setup, allows a later one.**
With `entry_start_min=15`, produce a fully valid long pullback-and-reclaim at 09:20 (within the
first 15 min) → expect `None` (too early). Produce the same pattern after 09:30 → expect the BUY
ENTER. Confirms the open-delay gate.

**T12 (orientation guard — proves this is TREND, not mean-reversion).**
Feed a bar with price **far below a rising VWAP** (deep discount, `close << vwap`, slope>0) with no
pullback arming. Expect `None` — this strategy does NOT buy a dip far under VWAP (that is the
mean-reversion behavior). Conversely a long only fires after an *armed pullback that reclaims*
VWAP from above (T3). This test must fail if someone accidentally implements the MR logic.

---

## 9. Backtest / harness notes

- Register in the engine: `STRATEGIES["vwap_trend"] = (VwapTrend, VwapTrendParams)` in
  `research/backtest/registry.py` (the refactor coder owns wiring `--strategy vwap_trend`). Same
  period (`2024-01-01 → 2026-06-19`), `--split-date 2026-01-01`, `--n 10`, `--slippage-bps 5`,
  `--equity 500000`, and cost stack as ORB — comparability is the whole point.
- **Volume hook is the one prerequisite**: the `hasattr(strat, "on_bar_volume")` call in
  `research/backtest/engine.py` (§2.1) must land before this strategy can run. It is additive and
  `hasattr`-gated, so ORB/EMA/RSI remain byte-for-byte unchanged (regression guard intact). The
  `volume` column is already SELECTed by `load_day_bars`.
- The engine's `_intrabar_exit` (post-refactor) uses the **stored** `decision.stop`/`.target`.
  Because `target` is a far ±50% level, the target arm essentially never fires intrabar; the stop
  arm uses the same level as the §4 close-based stop, so they agree (gap-aware stop logic in the
  engine still applies).
- This is signal-exit (VWAP cross-back) + pullback-entry: expect FEWER trades than a raw EMA flip
  (the arm→trigger discipline filters chases), with edge concentrated in trending sessions and
  poor performance in choppy, VWAP-glued tape. `slope_eps`, `entry_start_min`, and the band params
  are the anti-churn knobs — sweep in IS, lock before OOS.
- Gate compatibility: works unchanged under `--gate kronos` (the gate sees the same `df.iloc[:i+1]`
  slice at ENTER time).
- Survivorship = CEILING (current scrip master), same caveat as the ORB/EMA study; OOS
  (`--split-date 2026-01-01`) is the bar.
</content>
</invoke>
