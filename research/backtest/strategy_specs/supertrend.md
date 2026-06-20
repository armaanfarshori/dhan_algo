# Supertrend — strategy spec (ATR-based trend following)

Implements `strategies/supertrend.py`, a pure, synchronous, IO-free class mirroring
`strategies/orb.py` and obeying every rule in `_CONTRACT.md`. One position at a time,
EOD square-off unconditional, indicators computed incrementally with no lookahead.

The Supertrend is a trailing-stop trend follower: when the close flips above the
Supertrend line we go (and stay) **long**, when it flips below we go **short**, and the
Supertrend line itself is the trailing stop. There is no separate profit target — the
exit is "opposite flip", which is detected on `on_tick` and also expressed to the engine
as the protective `stop` so the backtester's intrabar wick check exits at the line.

---

## 1. Indicator math — incremental, bar by bar

Inputs per bar: `high`, `low`, `close` (the engine passes `price = close`, plus bar
`high`/`low`). We keep the prior bar's close, prior ATR, prior final bands, and the prior
Supertrend direction. All recurrences are causal (use only the current bar and prior
state) — no future bars, no peeking.

### 1.1 True Range (TR)
For each bar with previous close `prev_close`:
```
tr = max(high - low,
         abs(high - prev_close),
         abs(low  - prev_close))
```
On the **first bar of the session** there is no prev_close, so `tr = high - low`.

### 1.2 ATR — Wilder smoothing
ATR period = `atr_period` (default 10). Wilder's RMA, seeded with a simple average of the
first `atr_period` TRs:

- Accumulate TRs while `bar_count <= atr_period`.
- On the bar where `bar_count == atr_period`: `atr = mean(first atr_period TRs)` (this is
  the seed; ATR becomes available).
- Every bar after: `atr = (prev_atr * (atr_period - 1) + tr) / atr_period`.

Until the seed bar, `atr` is undefined and the strategy is in warm-up (returns `None`).

### 1.3 Basic bands
```
hl2          = (high + low) / 2
basic_upper  = hl2 + multiplier * atr
basic_lower  = hl2 - multiplier * atr
```
`multiplier` default 3.0. These are recomputed every bar from the current ATR.

### 1.4 Final bands — the band-locking recurrence
The final bands "ratchet": they only move in the trend-tightening direction unless price
closes through them. With `prev_final_upper`, `prev_final_lower`, `prev_close`:

```
final_upper = basic_upper
    if (basic_upper < prev_final_upper) or (prev_close > prev_final_upper)
    else prev_final_upper

final_lower = basic_lower
    if (basic_lower > prev_final_lower) or (prev_close < prev_final_lower)
    else prev_final_lower
```
In words:
- The final **upper** band tightens downward as ATR/price fall, but is *locked* (held at
  its prior value) while the trend is down — it only releases when the prior close has
  *broken above* it (i.e. trend has flipped up).
- The final **lower** band tightens upward in an uptrend and is locked otherwise; it
  releases when the prior close *broke below* it.

On the **seed bar** (first bar ATR exists), there is no `prev_final_*`; initialise
`final_upper = basic_upper`, `final_lower = basic_lower`.

### 1.5 Supertrend line + direction
Direction is `+1` (uptrend, line = final_lower, acts as a trailing stop below price) or
`-1` (downtrend, line = final_upper, trailing stop above price). With `prev_dir`,
`prev_supertrend` (the prior line value), `prev_final_upper`, `prev_final_lower`, and
current `close`:

Seed bar (no prior direction): choose the initial direction from the close vs basic bands.
Convention (matches the common TradingView implementation):
```
if bar is seed:
    direction = +1 if close <= basic_upper else +1   # default start +1
    # We start direction = +1 and supertrend = final_lower; the first real flip
    # sets a clean state. (No trade is taken on the seed bar — warm-up.)
    supertrend = final_lower if direction == +1 else final_upper
```
Subsequent bars — flip rule based on which band the **prior** Supertrend tracked:
```
if prev_dir == +1:
    # was uptrend, line was the lower band
    direction = -1 if close < final_lower else +1
else:  # prev_dir == -1, line was the upper band
    direction = +1 if close > final_upper else -1

supertrend = final_lower if direction == +1 else final_upper
```
A **flip** is `direction != prev_dir`. `+1 → -1` is a *short flip*; `-1 → +1` is a
*long flip*.

State carried to next bar: `prev_close=close`, `prev_atr=atr`,
`prev_final_upper=final_upper`, `prev_final_lower=final_lower`, `prev_dir=direction`,
`prev_supertrend=supertrend`.

---

## 2. Decision mapping (action / side / stop / target / reason)

Order of checks inside `on_tick` (copy ORB's skeleton):

1. **Reject** `price <= 0` → `None`.
2. **Future-skew guard** (copy ORB lines 88–96 verbatim): ignore ticks > `MAX_FUTURE_SKEW`
   (2 min) ahead of wall clock; do not reset session, do not update indicators → `None`.
3. **Session reset** if `now.date() != self._session_date` → `_reset_session(today)`.
4. **EOD square-off (UNCONDITIONAL)** — at `t >= 15:30 − squareoff_before_close_min`: if
   `self.position != 0` return `Decision(action="EXIT", reason="EOD square-off")`, else
   `None`. This is placed BEFORE the warm-up gate so it never depends on the indicator
   being ready (matches ORB section 3). After computing this we still must **not** update
   indicators with this late bar? — No: we DO update indicators every non-skewed bar for
   consistency, but EOD square-off short-circuits the trade logic. Implementation: update
   indicators first (step 5), then do EOD check before entry/flip logic. (EOD check itself
   never reads the indicator.) Concretely, run step 5 (indicator update), then:
   `if t >= squareoff: return EXIT if position else None`.
5. **Indicator update** (section 1) for this bar. Track `self._bars_seen`.
6. **Warm-up gate** — if ATR not yet seeded (`self._atr is None`) OR this is the seed bar
   (we never trade on the seed bar; direction is just initialised): return `None`.
7. **Trade logic** (only reached when warm-up done and before EOD square-off time):

   Let `flipped = (direction != prev_dir)` for THIS bar, `line = supertrend` (current).

   - **If flat (`self.position == 0`)** and a flip occurred this bar:
     - long flip (`direction == +1`): the new uptrend line is `final_lower` (below price).
       ```
       Decision(action="ENTER", side="BUY",
                stop=line,                       # the Supertrend line = trailing stop
                target=price + far_mult*atr_or_wide,  # see §2.1
                reason=f"ST long flip line={line:.2f}")
       ```
     - short flip (`direction == -1`): line is `final_upper` (above price).
       ```
       Decision(action="ENTER", side="SELL",
                stop=line,
                target=price - far_mult*atr_or_wide,
                reason=f"ST short flip line={line:.2f}")
       ```
   - **If long (`self.position > 0`)**: exit on a short flip OR if the close has crossed
     below the current line (defensive — the flip rule already captures this, but emit a
     close-based EXIT too so live ticks exit promptly):
     ```
     if direction == -1 or price < line:   # opposite flip / line breached on close
         return Decision(action="EXIT", reason=f"ST flip down / stop line={line:.2f}")
     ```
     After this EXIT fills and the strategy is flat, the SAME short flip bar would also be
     an entry candidate — but the engine only processes one decision per bar and enforces
     flat-before-enter, so the re-entry short is taken on the **next** flip evaluation once
     `notify_flat` is received (mirrors ORB, which never both-exits-and-enters in one tick).
   - **If short (`self.position < 0`)**:
     ```
     if direction == +1 or price > line:
         return Decision(action="EXIT", reason=f"ST flip up / stop line={line:.2f}")
     ```
   - Otherwise → `None`.

### 2.1 The signal-exit ↔ trailing-stop hybrid — how it maps to ENTER `stop` and `on_tick` EXIT

This strategy has no profit target; the exit is "opposite flip", which equals "close
crosses the Supertrend line". Two complementary mechanisms cover it:

- **`stop` on ENTER = the current Supertrend line.** The backtest engine stores
  `decision.stop` and `decision.target` at entry and runs an intrabar wick check
  (`_intrabar_exit` in `engine.py`) using `bar.low`/`bar.high`. So if a later bar's wick
  pierces the line we get a realistic intrabar stop fill at the line price. **Important:**
  the engine's stored stop is the line value *at entry time*; the Supertrend line moves
  every bar. To get a true *trailing* stop in the backtest we rely on the per-bar
  `on_tick` EXIT (below) firing when the close crosses the updated line, NOT only on the
  static stored stop. The stored stop is the protective floor/ceiling for intrabar gaps;
  the trailing behaviour comes from the close-based flip EXIT each bar.
- **`on_tick` EXIT (close-based flip).** Every bar, while in a position, we recompute the
  line and emit `Decision(action="EXIT", ...)` the moment `direction` flips or `close`
  crosses the line. In the live engine (tick stream) this is the trailing stop. In the
  backtester this fires on the signal bar's close and fills at the next bar's open (engine
  step 1 / step 3 EXIT branch).
- **`target`** is set to a *wide far level* (entry ± `target_atr_mult * atr`, default
  multiplier large, e.g. 100, effectively unreachable) purely to satisfy the contract's
  "ENTER must carry a concrete stop and target". It should never be the binding exit; the
  flip/line exit is the real one. Document `target_atr_mult` as "wide sentinel, not a real
  TP".

Net effect: stop = current line (trailing, via per-bar close EXIT), target = sentinel,
exit reason = opposite flip / EOD.

---

## 3. `SupertrendParams` dataclass

```python
@dataclass
class SupertrendParams:
    atr_period: int = 10                  # Wilder ATR lookback
    multiplier: float = 3.0               # band width = multiplier * ATR
    target_atr_mult: float = 100.0        # WIDE sentinel target (never the real exit)
    squareoff_before_close_min: int = 15  # EOD flatten lead (matches ORB)
    min_atr_pct: float = 0.0              # optional: skip entries if atr < price*this
                                          #   (0.0 = disabled; a vol floor like 0.001
                                          #    can suppress dead-flat names)
```
Notes:
- `atr_period=10`, `multiplier=3.0` are the requested defaults and the canonical Supertrend
  settings.
- `target_atr_mult` large by design (sentinel). Keep it as a param so the backtest can
  prove the target never binds.
- `min_atr_pct` is an optional liquidity/vol filter analogous to ORB's `min_range_pct`;
  default off so the base spec is pure Supertrend.

---

## 4. Warm-up handling

- Return `None` on every bar until ATR is seeded: that is until `self._bars_seen ==
  atr_period` (the seed bar computes the simple-average seed). With period 10, bars 1–10
  feed the seed; ATR exists from bar 10.
- The **seed bar itself takes no trade** — direction is merely initialised (`+1`,
  `supertrend = final_lower`). First tradable bar is bar 11.
- EOD square-off must still fire during warm-up if a position somehow exists (e.g. a
  DB-reconciled position after a mid-session restart) — the EOD check sits above the
  warm-up gate, exactly like ORB.

---

## 5. Session-reset list (`_reset_session(today)`)

Reset ALL of these on a date change (no cross-session leakage):
- `self._session_date = today`
- `self._bars_seen = 0`
- `self._tr_seed = []` (accumulator for the ATR seed average)
- `self._atr = None`
- `self._prev_close = None`
- `self._prev_final_upper = None`
- `self._prev_final_lower = None`
- `self._prev_dir = None`
- `self._prev_supertrend = None`
- (position state `self.position`/`self.entry_price` is owned by notify_fill/notify_flat
  and is NOT reset here — same as ORB.)

`notify_fill` / `notify_flat`: copy ORB verbatim (update `self.position`,
`self.entry_price`).

---

## 6. Unit-test cases (input bars → expected Decision)

All tests use `security_id="T"`, `SupertrendParams(atr_period=3, multiplier=2.0)` unless
noted (period 3 keeps fixtures short). Bars are `(time, close, high, low)` on
`2024-06-03` (a Monday), starting 09:15 IST, 1-minute apart, fed via
`on_tick(now, close, high=high, low=low)`. "Decision" = the return value.

> Use a small ATR period so the seed lands quickly. The expected directions below assume
> the band-locking recurrence in §1.4 and the flip rule in §1.5; the coder should compute
> the exact line values from the recurrence and assert `side`/`action`/`reason` substring
> and that `stop == supertrend_line` at the asserted precision.

1. **Warm-up returns None.** Feed bars 1–3 (period 3 → seed on bar 3). Each of bars 1, 2
   returns `None` (ATR undefined). Bar 3 (seed) also returns `None` (no trade on seed).
   *Assert:* all three `on_tick` calls return `None`.

2. **First long flip → ENTER BUY with stop = line.** After the seed, feed a rising
   sequence so the close climbs above the upper band and direction goes `+1` while flat.
   Bars (close,high,low): `(100,100,100)`,`(100,101,99)`,`(100,101,99)` [seed],
   then `(108,109,99)` — a strong up bar that flips direction to `+1`.
   *Assert:* the flip bar returns `Decision(action="ENTER", side="BUY")`, `stop` equals the
   current `final_lower` (Supertrend line), `target > price`, reason contains "long flip".

3. **First short flip → ENTER SELL with stop = line.** Symmetric to #2 with a falling
   sequence: `(100,100,100)`,`(100,101,99)`,`(100,101,99)` [seed], then `(92,101,91)` — a
   strong down bar flipping direction to `-1`.
   *Assert:* `Decision(action="ENTER", side="SELL")`, `stop == final_upper`,
   `target < price`, reason contains "short flip".

4. **Hold long while uptrend persists → None.** After the ENTER BUY of #2, call
   `notify_fill("BUY", qty, fill_px)`, then feed more up/flat bars whose close stays above
   the line and direction stays `+1`.
   *Assert:* each subsequent `on_tick` returns `None` (no exit, trailing line still below).

5. **Long exit on opposite flip.** From the held-long state of #4, feed a sharp down bar
   whose close drops below the current `final_lower` so direction flips to `-1`.
   *Assert:* returns `Decision(action="EXIT")`, reason contains "flip down" / "stop line".
   (The re-entry short is NOT emitted on the same bar.)

6. **Short exit on opposite flip.** Mirror of #5 from a held-short state (after #3 +
   `notify_fill("SELL", ...)`): feed a sharp up bar whose close exceeds `final_upper`,
   direction flips `+1`.
   *Assert:* `Decision(action="EXIT")`, reason contains "flip up" / "stop line".

7. **Trailing stop ratchets (line moves with price, never against trend).** In a held-long
   uptrend, assert the Supertrend line (== `final_lower`) is **monotonically
   non-decreasing** across consecutive up bars (the band-locking rule). Implementation
   detail: expose the current line via a `status()`/property; assert `line[i] >= line[i-1]`
   while direction stays `+1`.
   *Assert:* non-decreasing line; no EXIT emitted.

8. **EOD square-off is unconditional and indicator-independent.** Construct a held-long
   position, then call `on_tick` with `now` at `15:16` IST (15:30 − 15 + default? use
   `squareoff_before_close_min=15` → 15:15 onward).
   *Assert:* returns `Decision(action="EXIT", reason="EOD square-off")` REGARDLESS of
   direction/line. Also assert it fires even if called during warm-up with a position set
   (simulate `self.position=1` directly, only 1 bar seen) → still EXIT.

9. **Flat at EOD time → None.** Same as #8 but `self.position == 0`.
   *Assert:* returns `None` (no spurious exit).

10. **Session reset clears state / no cross-day leakage.** Feed a full uptrend on day 1
    (reach direction `+1`, line established). Then feed the first bar of day 2 (date
    change). *Assert:* `_session_date` updated, `_atr is None`, `_prev_dir is None`,
    `_bars_seen == 1`, and the day-2 bar returns `None` (back in warm-up). Position state
    is preserved (not touched by reset) but no trade signal is produced until day-2 warm-up
    completes.

(Optional 11) **Future-skew guard.** A tick stamped > 2 min ahead of wall clock returns
`None` and does NOT advance `_bars_seen` or reset the session — copy ORB's test pattern.

---

## 7. Backtest / parameter notes

- Register in the engine: `STRATEGIES["supertrend"] = (Supertrend, SupertrendParams)`;
  selectable via `--strategy supertrend`. Engine stores `decision.stop`/`decision.target`
  at ENTER (per the refactor) and the wick check exits at the stored stop for intrabar
  gaps; the per-bar close-based flip EXIT provides the trailing behaviour.
- Defaults `atr_period=10, multiplier=3.0` for the actual study; tune `multiplier`
  (e.g. 2 / 2.5 / 3 / 3.5) as the primary sensitivity knob — lower = tighter stop, more
  flips/whipsaw; higher = looser stop, fewer trades.
- This strategy is **always in the market** during the session once warmed up (long or
  short by direction), unlike ORB which can sit flat. Expect more trades and higher cost
  drag; the `min_atr_pct` vol floor and the EOD square-off bound exposure. Whipsaw in
  choppy names is the known failure mode — judge on OOS Sharpe (split-date 2026-01-01),
  same harness/costs/universe as ORB (`--n 10 --slippage-bps 5 --equity 500000`).
- Survivorship = CEILING (current scrip master) — same honest-bars caveat as the ORB/M3
  study. OOS is the bar for promotion.
```
