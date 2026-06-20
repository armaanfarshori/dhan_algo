# Strategy spec — EMA Crossover (9/21), intraday trend-following

Target file: `strategies/ema_crossover.py`. Implements the same pure, synchronous, IO-free
interface as `strategies/orb.py` (`on_tick → Optional[Decision]`, `notify_fill`, `notify_flat`).
Reuse `Decision` from `strategies/orb.py` — **do not redefine it**. Also reuse the module
constants `IST`, `MARKET_OPEN`, `MARKET_CLOSE`, `MAX_FUTURE_SKEW` (import from `strategies.orb`)
to stay byte-identical with ORB's session/future-skew handling.

This is a **signal-exit** strategy: entries flip on the EMA cross, exits happen on the opposite
cross. A protective stop is still attached to every ENTER (contract rule 4 — the engine's
intrabar wick check needs a concrete `stop`). `target` is set to a far "no-target" level so the
engine's target arm never fires; all real exits come from `on_tick` (opposite cross, stop-close,
or EOD).

---

## 1. Indicators — incremental EMA from the bar-close stream

Two EMAs over the **bar close** series within a single session (no cross-session carry):

- fast: `n_fast = 9`
- slow: `n_slow = 21`

Smoothing factor for period `n`: `k = 2 / (n + 1)`.

**Warm-up via SMA seed (standard, leak-free):**
For each EMA, accumulate the first `n` closes, sum them, and on the `n`-th close set
`ema = sum / n` (simple average seed). From the `(n+1)`-th close onward apply the recurrence:

```
ema_t = close_t * k + ema_{t-1} * (1 - k)
```

Track each EMA independently with its own counter and running sum:

```
# per EMA: _count, _sum, _value (Optional[float]), and constant k
def _update_ema(self, slot, close):       # slot = "fast" | "slow"
    if value is None:                      # still seeding
        _count += 1
        _sum   += close
        if _count == n:                    # SMA seed completes
            value = _sum / n
    else:
        value = close * k + value * (1 - k)
    return value                           # None until seeded
```

The slow EMA seeds last (needs 21 closes), so the strategy is fully "ready" only after
`n_slow` bars. Until both EMAs are seeded, `on_tick` returns `None` (warm-up).

**Cross detection** needs the *previous* relationship between fast and slow. Keep
`self._prev_fast` / `self._prev_slow` (the EMA pair from the prior bar that had both seeded).
A cross at bar `t`:

- **Bullish cross**: `prev_fast <= prev_slow` AND `fast_t > slow_t`
- **Bearish cross**: `prev_fast >= prev_slow` AND `fast_t < slow_t`

The very first bar where both EMAs are seeded has no `prev_*` pair yet → record the pair, emit no
cross that bar (you cannot know the prior relationship; avoid a phantom cross). This means the
earliest possible entry is bar `n_slow + 1`.

> One bar = one close. `high`/`low` are used only for the protective-stop swing tracking and are
> NOT fed into the EMAs. Volume is ignored.

---

## 2. Entry rule (exact)

Evaluated on each bar close, only when flat (`self.position == 0`), both EMAs seeded, a valid
`prev_*` pair exists, and `now.time()` is within the tradable window
`[entry_start, eod_squareoff)` (see §6, EOD takes precedence).

- **Bullish cross** (`prev_fast <= prev_slow` and `fast > slow`):
  `Decision(action="ENTER", side="BUY", stop=<long stop>, target=<far target>, reason=...)`
- **Bearish cross** (`prev_fast >= prev_slow` and `fast < slow`):
  `Decision(action="ENTER", side="SELL", stop=<short stop>, target=<far target>, reason=...)`

Optional **trend/flat filter** (default ON): require the EMA separation to exceed a floor so we
don't churn in a flat, EMAs-glued market:
`abs(fast - slow) >= price * min_separation_pct`. If not met, return `None` (no entry; the cross
is consumed by updating `prev_*` regardless, so we don't re-fire it next bar).

**Stop levels** (`stop_mode` param):
- `"swing"` (default): use the recent swing extreme over a rolling window of the last
  `swing_lookback` bar lows/highs (within the session), padded by `sl_buffer_pct`.
  - long: `stop = min(recent lows) * (1 - sl_buffer_pct)`
  - short: `stop = max(recent highs) * (1 + sl_buffer_pct)`
  - If fewer than `swing_lookback` bars exist, use what is available (≥1 bar always exists).
- `"pct"` (fallback / simpler): fixed percentage from entry reference close.
  - long: `stop = close * (1 - stop_pct)`
  - short: `stop = close * (1 + stop_pct)`

Compute the stop from the **signal-bar close** as the entry reference (`close` passed to
`on_tick`). The engine fills at next-bar open and re-sizes off that fill, but the stop *level* is
the absolute price we hand it — that is fine and mirrors ORB (ORB also derives stop from OR
levels, not the actual fill).

**Far target** (`no_target`): so the engine's target arm is inert and exits are signal-driven.
- long: `target = close * (1 + far_target_mult)` (e.g. ×1.50 = +50%, unreachable intraday)
- short: `target = close * (1 - far_target_mult)`
Clamp short target at a small positive floor (never ≤ 0).

Set `self._stop_level` to the ENTER stop after emitting it (used by the stop-close exit in §3).
Do **not** assume the ENTER executed — `self.position` only changes via `notify_fill`.

---

## 3. Exit rules (exact, in priority order inside `on_tick`)

Checked only when `self.position != 0`. **Priority order matters** — return the first that fires:

1. **EOD square-off (unconditional, top of method)** — see §6. Highest priority.
2. **Opposite-cross signal exit** (the core exit):
   - long open + bearish cross (`prev_fast >= prev_slow` and `fast < slow`) →
     `Decision(action="EXIT", reason="EMA cross-down")`
   - short open + bullish cross (`prev_fast <= prev_slow` and `fast > slow`) →
     `Decision(action="EXIT", reason="EMA cross-up")`
   - The engine flips the position by re-entering on the *next* bar's cross logic once flat; we
     do not emit ENTER while still holding. (Exit this bar; the now-flat strategy may ENTER on a
     subsequent cross. A single bar cannot both exit and enter.)
3. **Protective stop on close** — `self._stop_level` breached by the bar close:
   - long: `close <= self._stop_level` → `Decision(action="EXIT", reason="Stop-loss ₹{lvl:.2f}")`
   - short: `close >= self._stop_level` → `Decision(action="EXIT", reason="Stop-loss ₹{lvl:.2f}")`
   - This is the *close-based* backstop. The engine's intrabar wick check (using the `stop` we
     passed at ENTER) catches the *intrabar* pierce independently — both use the same level, so
     they agree. Keeping the close-based check here mirrors ORB section 4.

If none fire, return `None`.

> No fixed profit target by design (trend-following rides the move until the opposite cross or
> stop). The far `target` exists only to satisfy the Decision contract / engine arm.

---

## 4. `EmaCrossoverParams` dataclass

```python
from dataclasses import dataclass

@dataclass
class EmaCrossoverParams:
    n_fast: int = 9
    n_slow: int = 21
    stop_mode: str = "swing"            # "swing" | "pct"
    swing_lookback: int = 10            # bars for swing-extreme stop (stop_mode="swing")
    sl_buffer_pct: float = 0.002        # padding beyond the swing extreme
    stop_pct: float = 0.01              # fixed stop distance (stop_mode="pct")
    min_separation_pct: float = 0.0005  # EMA-gap floor to take a cross (anti-churn); 0 disables
    far_target_mult: float = 0.50       # "no target" arm: ±50% from entry (unreachable intraday)
    entry_start_min: int = 5            # don't enter in the first N min after open (warm-up/noise)
    squareoff_before_close_min: int = 15
```

Defaults are deliberately intraday-sane: 9/21 on 1-min bars, a swing stop with a 0.2% pad, a tiny
separation floor to skip dead-flat crosses, no profit target, 5-min open delay, and the same
15-min EOD square-off as ORB. The backtest harness sweep can vary `n_fast/n_slow`,
`min_separation_pct`, `stop_mode`, and `swing_lookback`.

---

## 5. `on_tick` skeleton (control flow)

```python
def on_tick(self, now, price, high=None, low=None) -> Optional[Decision]:
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

    # 1. EOD square-off — UNCONDITIONAL, before any indicator-readiness gate
    squareoff = (datetime.combine(today, MARKET_CLOSE)
                 - timedelta(minutes=self.p.squareoff_before_close_min)).time()
    if t >= squareoff:
        if self.position != 0:
            return Decision(action="EXIT", reason="EOD square-off")
        return None

    # 2. track swing window (raw high/low or price) for the stop — BEFORE EMA update
    self._push_swing(high if high is not None else price,
                     low  if low  is not None else price)

    # 3. update EMAs from the close (= price)
    fast = self._update_ema("fast", price)
    slow = self._update_ema("slow", price)
    if fast is None or slow is None:
        return None                              # warm-up: not both seeded

    # 4. need a prior pair to detect a cross
    if self._prev_fast is None:
        self._prev_fast, self._prev_slow = fast, slow
        return None                              # first seeded bar: no prior relationship

    bull = self._prev_fast <= self._prev_slow and fast > slow
    bear = self._prev_fast >= self._prev_slow and fast < slow
    # advance prior pair NOW so a consumed/ignored cross never re-fires next bar
    self._prev_fast, self._prev_slow = fast, slow

    # 5. exits first (position open)
    if self.position > 0:
        if bear:
            return Decision(action="EXIT", reason="EMA cross-down")
        if price <= self._stop_level:
            return Decision(action="EXIT", reason=f"Stop-loss ₹{self._stop_level:.2f}")
        return None
    if self.position < 0:
        if bull:
            return Decision(action="EXIT", reason="EMA cross-up")
        if price >= self._stop_level:
            return Decision(action="EXIT", reason=f"Stop-loss ₹{self._stop_level:.2f}")
        return None

    # 6. entries (flat) — respect the open delay
    entry_start = (datetime.combine(today, MARKET_OPEN)
                   + timedelta(minutes=self.p.entry_start_min)).time()
    if t < entry_start:
        return None
    if self.p.min_separation_pct > 0 and abs(fast - slow) < price * self.p.min_separation_pct:
        return None
    if bull:
        self._stop_level = self._long_stop(price)
        return Decision(action="ENTER", side="BUY", stop=self._stop_level,
                        target=price * (1 + self.p.far_target_mult),
                        reason=f"EMA{self.p.n_fast}>EMA{self.p.n_slow} cross-up")
    if bear:
        self._stop_level = self._short_stop(price)
        return Decision(action="ENTER", side="SELL", stop=self._stop_level,
                        target=max(price * (1 - self.p.far_target_mult), 0.05),
                        reason=f"EMA{self.p.n_fast}<EMA{self.p.n_slow} cross-down")
    return None
```

`notify_fill` / `notify_flat`: copy ORB verbatim (update `self.position` / `self.entry_price`;
`notify_flat` also leaves `self._stop_level` as-is or zero — it is only read while position≠0).

---

## 6. Session reset list (`_reset_session(today)`)

On every date change (and at construction), reset ALL intraday state — no cross-session leakage:

- `self._session_date = today`
- fast EMA: `_fast_value = None`, `_fast_count = 0`, `_fast_sum = 0.0`
- slow EMA: `_slow_value = None`, `_slow_count = 0`, `_slow_sum = 0.0`
- `self._prev_fast = None`, `self._prev_slow = None`
- swing window: `self._swing_highs = []`, `self._swing_lows = []` (or fixed-size deques)
- `self._stop_level = 0.0`

Position state (`self.position`, `self.entry_price`) is **not** reset here — it is owned by
`notify_fill`/`notify_flat` and may legitimately survive a date boundary mid-test (EOD square-off
flattens it the same session anyway).

Window/EOD constants reused from `strategies.orb`: `MARKET_OPEN = 09:15`, `MARKET_CLOSE = 15:30`,
`MAX_FUTURE_SKEW = 2 min`.

---

## 7. Unit-test cases (input bar sequence → expected Decision)

All times IST on a single trading day (e.g. 2024-03-01). Feed closes via `on_tick(ts, close,
high, low)`. Use `n_fast=3, n_slow=5` in tests for short, hand-checkable warm-up (set
`EmaCrossoverParams(n_fast=3, n_slow=5, min_separation_pct=0.0, entry_start_min=0,
stop_mode="pct", stop_pct=0.05)` unless a case says otherwise). With `min_separation_pct=0` the
anti-churn filter is disabled so crosses fire deterministically. Drive `notify_fill`/`notify_flat`
between bars to simulate the engine confirming fills.

Recurrence reminder (n=3 → k=0.5; n=5 → k=1/3). SMA seed = average of first n closes.

**T1 — warm-up returns None.**
Feed 4 ascending closes `100, 101, 102, 103` at 09:16–09:19. Slow EMA needs 5 closes → not
seeded. Every `on_tick` → `None`.

**T2 — first seeded bar emits no cross.**
Feed 5 closes `100,101,102,103,104`. On the 5th bar both EMAs seed (fast already seeded at bar 3).
This is the first bar with both seeded → records `prev_*`, returns `None` (no prior relationship).

**T3 — bullish cross → ENTER BUY with stop+far target.**
Construct a series where fast crosses above slow. E.g. start flat/declining so `fast <= slow`,
then a sharp up-move so `fast > slow` on a later bar. Expect on the cross bar:
`Decision(action="ENTER", side="BUY")`, `stop ≈ close*(1-0.05)` (pct mode), `target ≈
close*1.50`, reason contains "cross-up". (Verify `stop < close < target`.)

**T4 — bearish cross → ENTER SELL.**
Mirror of T3 (rising then sharp down-move so `fast < slow`). Expect
`Decision(action="ENTER", side="SELL")`, `stop ≈ close*1.05`, `target ≈ close*(1-0.50)`,
reason contains "cross-down".

**T5 — opposite cross exits an open long.**
Run T3 to get the BUY ENTER, then `notify_fill("BUY", qty, fill_px)`. Continue feeding a
down-move that produces a bearish cross. Expect `Decision(action="EXIT", reason="EMA cross-down")`
on the cross bar (and NOT a new SELL ENTER that same bar — exit only).

**T6 — protective stop on close exits a long (no opposite cross yet).**
After a BUY ENTER + `notify_fill`, feed a bar whose close ≤ stop level but where fast has not yet
crossed below slow. Expect `Decision(action="EXIT", reason="Stop-loss ₹…")`.

**T7 — EOD square-off is unconditional and dominates.**
With an open position (`notify_fill` done), call `on_tick` at 15:16 (≥ 15:30 − 15 min).
Expect `Decision(action="EXIT", reason="EOD square-off")` regardless of EMA state — even if EMAs
are not seeded. Also: same time, flat position → `None`.

**T8 — session reset clears EMAs across a date change.**
Seed EMAs fully on day 1 (feed enough closes). Then call `on_tick` on day 2 at 09:16 → must
return `None` (warm-up again), proving `_reset_session` wiped EMA state and `prev_*`. Feeding the
same day-1 cross sequence on day 2 reproduces the day-1 first-cross behavior (no leakage).

**T9 — min_separation filter skips a marginal cross.**
With `min_separation_pct=0.01` (1%), engineer a cross where `abs(fast-slow) < price*0.01` on the
cross bar. Expect `None` (no ENTER). Then confirm `prev_*` advanced so the next bar with the same
sign does NOT re-fire the cross (no double-trigger).

**T10 — entry_start delay blocks an early cross, allows a later one.**
With `entry_start_min=15`, produce a bullish cross at 09:20 (within the first 15 min) → expect
`None` (too early; cross consumed via `prev_*` advance). Produce the next qualifying cross after
09:30 → expect the ENTER. Confirms the open-delay gate without leaking the early cross.

(Optional T11 — swing-stop mode: with `stop_mode="swing", swing_lookback=3`, after a BUY ENTER
the emitted `stop` equals `min(last 3 lows) * (1 - sl_buffer_pct)`; assert the exact value from a
known low sequence.)

---

## 8. Backtest / harness notes

- Register in the engine: `STRATEGIES["ema_crossover"] = (EmaCrossover, EmaCrossoverParams)`
  (the refactor coder owns wiring `--strategy ema_crossover`). Same period, split, universe,
  slippage, equity, and cost stack as ORB — comparability is the whole point.
- The engine's `_intrabar_exit` (post-refactor) uses the **stored** `decision.stop` / `.target`.
  Because `target` is a far ±50% level, the target arm will essentially never fire intrabar;
  the stop arm uses the same level as the §3 close-based stop, so they agree. All trend exits
  come from the on_tick opposite-cross EXIT (gap-aware stop logic in the engine still applies to
  the stop level).
- This is signal-exit + flip: expect more trades than ORB, smaller average edge per trade, and
  sensitivity to chop. The `min_separation_pct` and `entry_start_min` knobs are the primary
  anti-churn defenses; sweep them in IS, lock before OOS.
- Gate compatibility: works unchanged under `--gate kronos` (the gate sees the same
  `df.iloc[:i+1]` slice at ENTER time).
- Survivorship = CEILING (current scrip master), same caveat as the ORB study; OOS
  (`--split-date 2026-01-01`) is the bar.
</content>
</invoke>
