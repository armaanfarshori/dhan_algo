# Opening Gap — strategy spec (gap-and-go + gap-fade)

Implements `strategies/opening_gap.py`, a pure, synchronous, IO-free class mirroring
`strategies/orb.py` and obeying every rule in `_CONTRACT.md`. One position at a time,
EOD square-off unconditional, all intraday state derived causally from the bar stream
(no lookahead, no cross-session leakage).

The idea: at the session open the security gaps relative to the **prior trading day's
close**. We classify the gap by size and react in one of two regimes:

- **(a) Gap-and-go** — a *moderate* gap (`go_min_pct ≤ |gap| < fade_min_pct`) is read as
  genuine momentum. We trade **WITH** the gap (long an up-gap, short a down-gap), but only
  after the gap *holds* through a short confirmation window: the price must not fill back
  past a fraction of the gap. Entry is a continuation breakout of the confirmation-window
  extreme in the gap direction.
- **(b) Gap-fade** — an *extreme* gap (`|gap| ≥ fade_min_pct`) is read as an over-reaction
  likely to retrace toward the prior close. We trade **TOWARD prior close** (short an
  up-gap, long a down-gap), entering when the price shows a reversal off the
  confirmation-window extreme. Target is (a fraction of the way to) the prior close.

Exactly one direction/regime is attempted per session (one position at a time; once tried,
the side is locked like ORB's `_long_tried`/`_short_tried`). If the prior-day close is
missing, or the gap is too small to classify, the strategy is inert (returns `None`) all
day — except the unconditional EOD square-off.

---

## 1. Prior-day data injection — `seed_prior_day`

The per-session replay does NOT currently provide prior-session context. The strategy
declares this clean injection interface; the **engine owner** fetches the prior **daily**
bar and calls it once, before the first `on_tick` of each session.

```python
def seed_prior_day(self, prior_close: float,
                   prior_high: float = 0.0,
                   prior_low: float = 0.0) -> None:
    """Inject the prior trading day's daily-bar levels for THIS session.

    prior_close — REQUIRED. The reference for the gap and the fade target.
    prior_high / prior_low — OPTIONAL context (default 0.0 = unknown). Not
        required by the base logic; reserved for an optional fade-target clamp
        (see §2.4). If unknown, leave at 0.0 and the clamp is skipped.

    Must be called BEFORE the first on_tick of the session it describes. Validates
    inputs; a non-positive prior_close is ignored (leaves the strategy unseeded →
    inert for the day). Safe to call again on a later session (overwrites)."""
```

### 1.1 What prior-session data the engine must supply
- **Required:** `prior_close` = the **close of the most recent trading day strictly before
  the session being replayed** (the previous daily bar's `close`). This is the canonical
  gap reference.
- **Optional:** `prior_high`, `prior_low` from the same prior daily bar — used only for the
  optional fade-target clamp (§2.4). Pass `0.0` if not readily available.
- Source: the engine should read the `daily` (or `1d`) timeframe bar for `security_id`
  whose session date is the last trading day `< day`. If the strategy ran on 1-min bars
  only, the engine can instead compute it from the prior session's last 1-min close, but
  the **daily bar's official close is preferred** (it is the settlement reference traders
  watch). State which source was used in the study notes.

### 1.2 Wiring contract (engine owner)
Once per `(security_id, day)` replay, **before** the bar loop:
```python
strat = OpeningGap(security_id, params.opening_gap)
prior = fetch_prior_daily_bar(security_id, day)   # engine-owned helper
if prior is not None:
    strat.seed_prior_day(prior.close, prior.high, prior.low)
# else: leave unseeded — strategy returns None all session (graceful degrade)
```
`seed_prior_day` does NOT set `self._session_date` (unlike ORB's `seed_opening_range`):
the seed is regime context, not session state. The session is established normally by the
first `on_tick`. The seed survives the first `_reset_session` of the day because reset is
keyed on date change and the seed is applied just before that first tick — see §1.3.

### 1.3 Seed lifetime vs session reset (IMPORTANT)
`_reset_session` must **NOT** clear `self._prior_close` (the engine sets it for the upcoming
day *before* the loop, and the first `on_tick` triggers a reset). Concretely:
- The engine calls `seed_prior_day(...)` → sets `self._prior_close`, `self._prior_high`,
  `self._prior_low`, and `self._prior_day_for = None` sentinel? No — simpler: store the
  seed in dedicated fields that `_reset_session` leaves untouched. `_reset_session` resets
  only the per-session *trading* state (gap value, confirm-window extremes, locks). The
  prior-day fields persist until the engine overwrites them on the next session.
- To prevent stale prior-day data leaking into a session the engine forgot to seed, the
  strategy also tracks `self._seeded_for_session` lazily: on the first tick of a new date,
  it captures the *current* seed into the session (`self._gap_ref = self._prior_close`) and
  then treats `self._prior_close` as consumed. See §3 reset list. The net rule: **the seed
  applies to the very next session that begins after it was set.**

If `seed_prior_day` was never called (or with a non-positive close), `self._prior_close`
stays `0.0` → §2.1 short-circuits to `None` → no trades that session (graceful degrade).

---

## 2. Decision logic — per session

All times IST. The session reference levels:
```
open_px      = first valid bar's OPEN of the session (captured on the first non-skewed
               tick whose t >= MARKET_OPEN; see §2.6)
gap_ref      = self._prior_close captured at session start
gap          = (open_px - gap_ref) / gap_ref          # signed
gap_dir      = +1 if gap > 0 (up-gap) else -1 (down-gap)
```

### 2.1 Warm-up / classification gate (must pass before any trade logic)
Return `None` (do nothing but keep building state) unless ALL hold:
- `gap_ref > 0` (prior close was seeded), AND
- `open_px > 0` (session open captured), AND
- `abs(gap) >= go_min_pct` (gap big enough to classify — below this it is noise).

If `abs(gap) < go_min_pct` for the captured open, the session is **No-Trade**: set
`self._regime = "none"` and return `None` for the rest of the day (EOD square-off still
fires). The regime is decided ONCE, from the captured open, and never re-evaluated.

### 2.2 Regime classification (decided once, at open capture)
```
if abs(gap) <  go_min_pct:                regime = "none"      # too small → no trade
elif abs(gap) <  fade_min_pct:            regime = "go"        # moderate → trade WITH gap
else:                                     regime = "fade"      # extreme → fade toward close
```
`go_min_pct` default 0.01 (1%), `fade_min_pct` default 0.04 (4%). So:
- gap in `[1%, 4%)` → gap-and-go.
- gap `≥ 4%` → gap-fade.
- gap `< 1%` → no trade.

### 2.3 Confirmation window
Window length `confirm_min` minutes (default 5) starting at `MARKET_OPEN` (09:15).
`confirm_end = 09:15 + confirm_min` (i.e. 09:20 by default).

During `09:15 ≤ t < confirm_end` (and not yet locked):
```
self._cw_high = max(self._cw_high, bar_high or price)
self._cw_low  = min(self._cw_low,  bar_low  or price)
```
At the first tick with `t >= confirm_end`, lock the window
(`self._confirm_locked = True`) and evaluate the **hold test** (below). No entry can be
emitted before the window is locked (return `None` while building it).

**Hold test (gap-and-go only).** The gap must not have *filled back* through more than
`go_fill_frac` (default 0.5) of itself during the window:
```
# up-gap (gap_dir == +1): the low must have stayed above the partial-fill level
fill_level = gap_ref + (1 - go_fill_frac) * (open_px - gap_ref)   # for up-gap
hold = self._cw_low >= fill_level                                  # up-gap holds
# down-gap (gap_dir == -1):
fill_level = gap_ref + (1 - go_fill_frac) * (open_px - gap_ref)   # open_px<gap_ref ⇒ above open
hold = self._cw_high <= fill_level                                 # down-gap holds
```
(Equivalently: the window's adverse extreme stayed within `go_fill_frac` of the gap.)
If the gap-and-go hold test FAILS, the session becomes No-Trade (`regime → "none"`),
return `None` thereafter. The fade regime has **no** hold test — an extreme gap that holds
or extends is still faded; we just need the reversal trigger (§2.4).

### 2.4 Entry trigger (after window locked, regime "go" or "fade", still flat, side untried)

Let `cw_high = self._cw_high`, `cw_low = self._cw_low`.

**Regime "go" — continuation breakout of the window extreme, WITH the gap:**
- up-gap (`gap_dir == +1`): trigger when `price > cw_high`.
  ```
  side   = "BUY"
  stop   = cw_low * (1 - sl_buffer_pct)
  risk   = price - stop
  target = price + target_rr * risk
  reason = f"gap-and-go LONG  gap={gap:+.2%} cw={cw_high:.2f}/{cw_low:.2f}"
  ```
- down-gap (`gap_dir == -1`): trigger when `price < cw_low`.
  ```
  side   = "SELL"
  stop   = cw_high * (1 + sl_buffer_pct)
  risk   = stop - price
  target = price - target_rr * risk
  reason = f"gap-and-go SHORT gap={gap:+.2%} cw={cw_high:.2f}/{cw_low:.2f}"
  ```

**Regime "fade" — reversal off the window extreme, TOWARD prior close:**
For an up-gap we expect a pullback down toward `gap_ref`; for a down-gap a bounce up.
- up-gap (`gap_dir == +1`) → fade SHORT: trigger when `price < cw_low`
  (price rolling over off the post-open high).
  ```
  side   = "SELL"
  stop   = cw_high * (1 + sl_buffer_pct)            # above the session high
  raw_target = gap_ref + (1 - fade_target_frac) * (open_px - gap_ref)
               # fade_target_frac of the way from open back toward prior close
  target = max(raw_target, ... clamp)              # see clamp below
  reason = f"gap-fade SHORT gap={gap:+.2%} →close {gap_ref:.2f}"
  ```
- down-gap (`gap_dir == -1`) → fade LONG: trigger when `price > cw_high`.
  ```
  side   = "BUY"
  stop   = cw_low * (1 - sl_buffer_pct)
  raw_target = gap_ref + (1 - fade_target_frac) * (open_px - gap_ref)
  target = min/max clamp toward prior close
  reason = f"gap-fade LONG  gap={gap:+.2%} →close {gap_ref:.2f}"
  ```
`fade_target_frac` default 0.5 ⇒ target is halfway from the open back to the prior close.

**Optional prior_high/low clamp (§1.1 optional inputs).** If `prior_high > 0` /
`prior_low > 0` were seeded, you MAY clamp the fade target so it does not project past the
prior day's range (a conservative TP): for a fade SHORT, `target = max(raw_target,
prior_high)` is NOT right — instead `target = max(raw_target, gap_ref)` keeps it on the
sensible side; the prior_high/low clamp is purely optional and OFF by default
(`use_prior_range_clamp=False`). Keep the base spec to `raw_target` (the close-fraction
level) so the logic is unambiguous; document the clamp as a tunable.

On any ENTER, set `self._tried = True` (single attempt per session — like ORB's
per-side tried flags, but here only one regime/direction is ever eligible, so one flag).
`stop` and `target` are ABSOLUTE price levels; the engine stores them and runs the
intrabar wick check (`_intrabar_exit`) at those stored levels.

### 2.5 Exits for an open position
Once in a position, every bar (before entry logic) check stop/target on the bar close as a
defensive close-based exit; the engine's wick check handles intrabar fills at the stored
stop/target. Mirror ORB section 4 exactly, using `self._stop`/`self._target` captured at
ENTER:
- LONG (`self.position > 0`): `if price >= self._target` → EXIT "Target hit"; `if price <=
  self._stop` → EXIT "Stop-loss".
- SHORT (`self.position < 0`): `if price <= self._target` → EXIT "Target hit"; `if price >=
  self._stop` → EXIT "Stop-loss".

There is no time-based exit other than EOD; the position rides to stop, target, or close.

### 2.6 Open capture from the bar stream
The session "open" is the **OPEN of the first valid bar at/after 09:15** of the session.
Capture rule, on each non-skewed tick after the session-reset:
```
if self._open_px == 0.0 and t >= MARKET_OPEN:
    self._open_px = bar_open_if_known else price
```
`on_tick` receives `price = bar close` and `high`/`low`; the engine does **not** pass
`bar.open`. So the strategy approximates the session open with the **first bar's close**
(the close of the 09:15 bar) when `open` is unavailable — document this approximation. To
make it exact, the engine MAY additionally call `seed_prior_day` is unrelated; instead the
strategy reads the first tick's price as the open proxy. (ORB has the same limitation —
it builds the OR from close/high/low.) The confirmation-window extremes (§2.3) still use
the true `high`/`low`, so the breakout/fade triggers are accurate; only the gap% uses the
first-bar close as the open proxy.

> Implementation note: this is the single approximation in the spec. It is acceptable
> because (1) the 09:15 bar's open and close are typically within a few ticks, and (2) the
> regime thresholds (1% / 4%) are far wider than that error. If the engine is later changed
> to pass `bar.open`, switch `self._open_px` to the true first-bar open with no other change.

---

## 3. Order of checks inside `on_tick` (copy ORB's skeleton)

1. **Reject** `price <= 0` → `None`.
2. **Future-skew guard** (copy ORB lines 88–96 verbatim): ignore ticks > `MAX_FUTURE_SKEW`
   (2 min) ahead of wall clock; do not reset session, do not update state → `None`.
3. **Session reset** if `now.date() != self._session_date` → `_reset_session(today)`
   (this captures the pending seed into the session; see §3 reset list).
4. **Open capture** (§2.6) if not yet captured and `t >= MARKET_OPEN`.
5. **EOD square-off (UNCONDITIONAL)** — at `t >= 15:30 − squareoff_before_close_min`: if
   `self.position != 0` return `Decision(action="EXIT", reason="EOD square-off")`, else
   `None`. Placed BEFORE every gate so it never depends on regime/window being ready
   (matches ORB section 3).
6. **Exits** for an open position (§2.5) — checked before any new-entry logic.
7. **Classification gate** (§2.1): if `gap_ref<=0` or `open_px<=0` or `abs(gap)<go_min_pct`
   → ensure `self._regime` is set (decide once) and return `None`.
8. **Confirmation window** (§2.3): while `t < confirm_end` build `cw_high/cw_low`, return
   `None`. At/after `confirm_end`, lock once + run the gap-and-go hold test (may set regime
   to "none").
9. **Entry trigger** (§2.4): if flat, not yet tried, regime in {"go","fade"}, window
   locked, and the trigger condition holds → return the ENTER `Decision` and set
   `self._tried = True`, store `self._stop`/`self._target`.
10. Otherwise → `None`.

`notify_fill` / `notify_flat`: copy ORB verbatim (update `self.position`,
`self.entry_price`). Additionally, `notify_flat` need not clear `_stop`/`_target` (they are
only read while `self.position != 0`), but it MAY for tidiness.

---

## 4. `OpeningGapParams` dataclass

```python
@dataclass
class OpeningGapParams:
    go_min_pct: float = 0.01          # |gap| floor to classify at all (1%); below → no trade
    fade_min_pct: float = 0.04        # |gap| at/above this → fade regime (4%); between → go
    confirm_min: int = 5              # confirmation-window length, minutes (09:15–09:20)
    go_fill_frac: float = 0.5         # gap-and-go hold test: gap may fill back at most this
                                      #   fraction during the window, else no trade
    sl_buffer_pct: float = 0.002      # stop padding beyond the confirm-window extreme
    target_rr: float = 1.5            # gap-and-go target = entry ± target_rr × entry-risk
    fade_target_frac: float = 0.5     # gap-fade target = this fraction of the way from
                                      #   the open back toward prior close
    squareoff_before_close_min: int = 15   # EOD flatten lead (matches ORB)
    use_prior_range_clamp: bool = False    # optional fade-target clamp using prior_high/low
                                           #   (OFF by default; see §2.4)
```
Notes / invariants:
- Require `go_min_pct < fade_min_pct` (the regime bands must not overlap; assert in
  `__post_init__` or document as a precondition).
- `target_rr` applies to gap-and-go (R-multiple of the entry-to-stop distance). The fade
  regime sizes its target off the prior-close distance (`fade_target_frac`), NOT `target_rr`
  — its risk:reward emerges from where the reversal triggers vs the close.
- `go_fill_frac=0.5` means "the gap must hold at least half its size through the first
  `confirm_min`" — a stricter (lower) value demands a cleaner gap.

---

## 5. Warm-up / missing-data handling

- **No prior_close seeded** (`self._prior_close <= 0`): inert all session — every `on_tick`
  returns `None` except the unconditional EOD square-off. This is the graceful-degrade path
  the engine relies on when the prior daily bar is unavailable.
- **Pre-window** (`t < confirm_end`): return `None` while accumulating `cw_high`/`cw_low`
  and capturing the open; no entries possible.
- **Gap too small** (`abs(gap) < go_min_pct`): regime "none", inert for the day.
- **Gap-and-go hold test failed**: regime flips to "none" at window lock, inert thereafter.
- **Last bar of session**: the engine handles next-bar fills; an ENTER on the final bar has
  no fill bar and is skipped by the engine (it already guards `i == n - 1`). EOD square-off
  fires well before the last bar (15:15 by default) so open positions flatten cleanly.
- **Mid-session restart** (live): not required for the backtest. If implemented later, a
  `seed_prior_day` + re-derivation of the window from REST intraday bars would mirror ORB's
  `seed_opening_range`; out of scope for this spec.

---

## 6. Session-reset list (`_reset_session(today)`)

On a date change, reset ALL per-session *trading* state (no cross-session leakage), and
**capture the pending prior-day seed into the session**:
- `self._session_date = today`
- `self._open_px = 0.0`
- `self._gap = 0.0`
- `self._gap_dir = 0`
- `self._regime = None`            # "none" | "go" | "fade", decided once at classification
- `self._cw_high = 0.0`
- `self._cw_low = float("inf")`
- `self._confirm_locked = False`
- `self._tried = False`
- `self._stop = 0.0`
- `self._target = 0.0`
- **Capture seed:** `self._gap_ref = self._prior_close` (the value the engine set before
  this session). Do NOT zero `self._prior_close` here — but treat `self._gap_ref` as the
  per-session reference from now on (so a forgotten re-seed on the next day reuses the same
  number only if the engine never updated it; the engine is contracted to seed every day).

Do **NOT** reset in `_reset_session`:
- `self._prior_close`, `self._prior_high`, `self._prior_low` — owned by `seed_prior_day`,
  overwritten per session by the engine before the loop.
- `self.position`, `self.entry_price` — owned by `notify_fill`/`notify_flat` (same as ORB).

---

## 7. Unit-test cases (input bars → expected Decision)

All tests use `security_id="T"`, default `OpeningGapParams()` unless noted, with
`confirm_min=5`, `go_min_pct=0.01`, `fade_min_pct=0.04`. Bars are `(time, close, high,
low)` on `2024-06-03` (a Monday), starting 09:15 IST, 1-minute apart, fed via
`on_tick(now, close, high=high, low=low)`. Call `seed_prior_day(prior_close=...)` BEFORE
the first `on_tick` of the session unless the test is the "missing seed" case.
"Decision" = the return value.

1. **Missing prior-day seed → inert.** Do NOT call `seed_prior_day`. Feed a full normal
   session (open 102 vs nothing). *Assert:* every `on_tick` returns `None` (including
   through the window and any breakout); a position is never opened. (EOD square-off only
   fires if a position exists, which it never does here.)

2. **Gap too small → no trade.** `seed_prior_day(100.0)`. First bar open/close ≈ 100.5
   (gap +0.5% < 1%). Feed the window then a strong breakout above the window high.
   *Assert:* `self._regime == "none"` after window; all `on_tick` return `None`.

3. **Gap-and-go LONG (moderate up-gap, holds, breaks out).** `seed_prior_day(100.0)`.
   First bar close ≈ 102.0 (gap +2% → "go"). Window bars (09:15–09:19) keep low ≥ ~101.5
   (gap holds: fill_level = 100 + 0.5*(102−100)=101 → low 101.5 ≥ 101 ✓), window high ≈
   102.5. After 09:20, feed a bar with `price = 103.0 > cw_high`.
   *Assert:* the breakout bar returns `Decision(action="ENTER", side="BUY")`, `stop ≈ cw_low
   * (1 − 0.002)`, `target = 103.0 + 1.5*(103.0 − stop)`, reason contains "gap-and-go LONG".

4. **Gap-and-go SHORT (moderate down-gap, holds, breaks down).** `seed_prior_day(100.0)`.
   First bar close ≈ 98.0 (gap −2% → "go"). Window high ≤ ~98.5 (down-gap holds:
   fill_level = 100 + 0.5*(98−100)=99 → high 98.5 ≤ 99 ✓), window low ≈ 97.5. After 09:20,
   feed `price = 97.0 < cw_low`.
   *Assert:* `Decision(action="ENTER", side="SELL")`, `stop ≈ cw_high*(1+0.002)`,
   `target = 97.0 − 1.5*(stop − 97.0)`, reason contains "gap-and-go SHORT".

5. **Gap-and-go hold test FAILS → no trade.** Same up-gap as #3 (open 102, gap +2%), but a
   window bar dips to low 100.5 (< fill_level 101 → gap filled past 50%). After 09:20 feed
   a breakout above the window high.
   *Assert:* regime flips to "none" at window lock; the breakout returns `None`; no ENTER
   ever emitted.

6. **Gap-fade SHORT (extreme up-gap → fade toward prior close).** `seed_prior_day(100.0)`.
   First bar close ≈ 105.0 (gap +5% ≥ 4% → "fade"). Window high ≈ 105.5, low ≈ 104.0
   (no hold test for fade). After 09:20, feed `price = 103.5 < cw_low` (rolling over).
   *Assert:* `Decision(action="ENTER", side="SELL")`, `stop ≈ cw_high*(1+0.002)`,
   `target = 100 + (1−0.5)*(105−100) = 102.5`, reason contains "gap-fade SHORT".

7. **Gap-fade LONG (extreme down-gap → fade toward prior close).** `seed_prior_day(100.0)`.
   First bar close ≈ 95.0 (gap −5% → "fade"). Window low ≈ 94.5, high ≈ 96.0. After 09:20,
   feed `price = 96.5 > cw_high` (bouncing).
   *Assert:* `Decision(action="ENTER", side="BUY")`, `stop ≈ cw_low*(1−0.002)`,
   `target = 100 + (1−0.5)*(95−100) = 97.5`, reason contains "gap-fade LONG".

8. **No entry before window locks.** Up-gap "go" setup as in #3, but feed a bar with
   `price = 103.0` (above eventual window high) at **09:17** (inside the window).
   *Assert:* returns `None` (window not yet locked, no entry possible); the
   confirmation-window high still updates to include 103.

9. **Stop / target exit after entry.** Take the gap-and-go LONG of #3, call
   `notify_fill("BUY", qty, fill_px)`. (a) Feed `price = self._target` → EXIT "Target hit".
   (b) In a fresh run, after the same entry feed `price = self._stop` → EXIT "Stop-loss".
   *Assert:* the respective `Decision(action="EXIT")` with the matching reason substring.

10. **EOD square-off is unconditional and regime-independent.** Construct a held position
    (e.g. via #3 + `notify_fill`), then call `on_tick` with `now` at **15:16** IST
    (squareoff = 15:30 − 15 = 15:15, so 15:16 ≥ trigger). *Assert:* returns
    `Decision(action="EXIT", reason="EOD square-off")`. Also assert it fires even with NO
    seed and only the position set directly (`self.position = 1`, never seeded) → still EXIT.

11. **Flat at EOD time → None.** Same as #10 but `self.position == 0`. *Assert:* `None`.

12. **Session reset / no cross-day leakage.** Run a full up-gap day-1 that ENTERs (as #3).
    Then `seed_prior_day(<day-2 prior close>)` and feed the first bar of day-2 (date
    change). *Assert:* `_session_date` updated, `_open_px == 0.0` (until captured),
    `_regime is None`, `_tried is False`, `_confirm_locked is False`, `_cw_high == 0.0`,
    `_cw_low == inf`, and `_gap_ref` equals the new day-2 prior close. Position state is
    preserved (not touched by reset).

(Optional 13) **Future-skew guard.** A tick stamped > 2 min ahead of wall clock returns
`None` and does NOT advance the window, capture the open, or reset the session — copy ORB's
test pattern.

---

## 8. Backtest / parameter notes

- Register: `STRATEGIES["opening_gap"] = (OpeningGap, OpeningGapParams)`; selectable via
  `--strategy opening_gap`. Engine stores `decision.stop`/`decision.target` at ENTER (per
  the refactor) and the wick check (`_intrabar_exit`) exits at the stored levels intrabar;
  the per-bar close-based exit (§2.5) is the defensive close path. **Engine prerequisite:**
  the engine must fetch the prior daily bar and call `seed_prior_day(...)` before each
  `replay_security_day` bar loop (§1.2). With no seed, the strategy produces zero trades —
  so verify the wiring on a smoke run (non-zero trade count) before trusting any null result.
- **Primary sensitivity knobs:** `fade_min_pct` (where go→fade flips) and `go_min_pct`
  (noise floor). Sweep e.g. go ∈ {0.75%, 1%, 1.5%}, fade ∈ {3%, 4%, 5%}. Also `confirm_min`
  ∈ {3, 5, 10} (longer window = stricter hold, fewer/later entries) and `target_rr`
  ∈ {1.0, 1.5, 2.0} for the go regime / `fade_target_frac` ∈ {0.4, 0.5, 0.7} for fade.
- This strategy takes **at most one trade per security per session** (one regime, one
  direction, single attempt) — far fewer trades than Supertrend; expect many flat sessions
  (gaps < `go_min_pct`). Judge on OOS Sharpe and trade count adequacy; thin trade counts
  per name may need a wider universe to be statistically meaningful.
- Same harness/costs/universe as ORB (`--n 10 --slippage-bps 5 --equity 500000`,
  `--split-date 2026-01-01`). Survivorship = CEILING (current scrip master) — same honest-
  bars caveat as the ORB/M3 study. Gap strategies are especially survivorship-sensitive
  (delisted/blown-up names gap hardest), so treat fade results with extra suspicion. OOS is
  the bar for promotion.
- Keep the Kronos gate plumbing intact (`--gate none|kronos`) so the gap entries can be
  Kronos-gated in a later A/B, identical to ORB.
```
