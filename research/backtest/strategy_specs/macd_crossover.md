# Strategy spec — MACD Crossover (`macd_crossover`)

A trend-following intraday strategy on 1-minute bars. Trade the crossover of the MACD line
(fast EMA − slow EMA) against its signal line (EMA of the MACD line). Enter long when MACD
crosses **above** signal; enter short when MACD crosses **below** signal. Reverse-cross is the
primary exit; a protective ATR stop backstops it; EOD square-off is unconditional.

This file is the implementation contract for `strategies/macd_crossover.py`. It mirrors
`strategies/orb.py` (same `Decision`, same `on_tick` signature, same hard rules from
`research/backtest/strategy_specs/_CONTRACT.md`). Read `_CONTRACT.md` and `orb.py` first.

---

## 1. Indicator math — computed INCREMENTALLY from the bar stream

All indicators are derived **only** from the close stream (plus high/low for ATR) seen so far in
the **current session**. No look-ahead, no cross-session leakage (session reset wipes all of it).

### 1.1 EMA recurrence

A single-pole EMA with smoothing `α = 2 / (period + 1)`:

```
ema_t = α · close_t + (1 − α) · ema_{t−1}
```

We keep three EMAs derived from the close stream and one (`signal`) derived from the MACD-line
stream:

| name        | period | smoothing α            | input series       |
|-------------|--------|------------------------|--------------------|
| `ema_fast`  | 12     | 2/(12+1) = 0.153846…   | bar close          |
| `ema_slow`  | 26     | 2/(26+1) = 0.074074…   | bar close          |
| `signal`    | 9      | 2/(9+1) = 0.2          | MACD-line series   |

Derived each bar (once all components exist):

```
macd_line_t  = ema_fast_t − ema_slow_t
signal_t     = α_sig · macd_line_t + (1 − α_sig) · signal_{t−1}
histogram_t  = macd_line_t − signal_t
```

### 1.2 Warm-up seeding (SMA seed — match TA-Lib / common convention)

Plain `ema_{t−1} = close` bootstrapping biases early values. Use the standard SMA-seed:

1. **`ema_fast` / `ema_slow`:** accumulate closes. The slow EMA is the binding constraint.
   - Buffer the first `slow_period` (26) closes.
   - On the **26th** close: seed `ema_slow = SMA(last 26 closes)` and seed
     `ema_fast = SMA(last 12 closes)` (the most recent 12 of those same 26). From the 27th close
     onward, advance both by the EMA recurrence.
   - **MACD line** is undefined until `ema_slow` exists (bar index ≥ 26, 1-based).
2. **`signal`:** the signal EMA needs `signal_period` (9) MACD-line values to seed.
   - Buffer the first 9 MACD-line values (bars 26..34 inclusive, 1-based).
   - On the **9th** MACD-line value (bar index 34): seed `signal = SMA(first 9 macd_line values)`,
     and `histogram = macd_line − signal`. From the next bar, advance by the EMA recurrence.
3. **Indicators are "ready"** (a crossover can be detected) only from bar index **35** onward
   (the first bar that has BOTH a current `histogram` and a previous-bar `histogram` to compare).
   Before that, `on_tick` returns `None` for entry purposes.

> Counting (1-based, per session): closes 1–25 → buffering; close 26 → first MACD line; closes
> 26–34 → buffering signal; close 34 → first signal/histogram; close 35 → first crossover-eligible
> bar. With 1-min bars and 09:15 open, bar 35 ≈ 09:49 IST.

### 1.3 Crossover detection

Track `prev_hist` (the previous bar's histogram) and `hist` (current). A crossover is a sign
change of the histogram (`macd_line − signal`):

```
cross_up   = prev_hist <= 0 and hist > 0       # MACD crossed ABOVE signal  → bullish
cross_down = prev_hist >= 0 and hist < 0       # MACD crossed BELOW signal  → bearish
```

(Using `<=`/`>=` on the prior bar catches a cross that starts exactly at zero. `hist == 0` on the
current bar is treated as "no cross yet".)

### 1.4 ATR (for the protective stop) — Wilder, incremental

True Range per bar:

```
tr_t = max( high_t − low_t,
            |high_t − prev_close|,
            |low_t  − prev_close| )      # prev_close is the previous bar's close
```

Wilder ATR with `atr_period` (14):

- Buffer the first 14 TR values; seed `atr = mean(first 14 TR)` on the 14th bar.
- Then `atr_t = (atr_{t−1} · (14 − 1) + tr_t) / 14`.
- On the very first bar of a session there is no `prev_close`; use `tr = high − low` for that bar.

ATR is ready by bar 14, well before the crossover-eligible bar 35, so a stop is always available
at entry. Defensive fallback: if for any reason `atr` is not ready or ≤ 0 at entry time, use the
percent stop (`stop_pct`) instead.

---

## 2. Decision rules → `Decision(action/side/stop/target/reason)`

`Decision` is imported from `strategies.orb` (do **not** redefine). `price` = bar close;
`high`/`low` = current bar extremes.

### 2.1 Entry (only when flat: `self.position == 0`)

Entry requires indicators ready AND, optionally, the zero-line filter (see §3 params):

- **Long** — `cross_up` is true and (if `require_zero_filter`) `macd_line > 0`:
  ```
  stop   = price − atr_mult · atr      (fallback: price · (1 − stop_pct))
  target = price + reward_mult · (price − stop)      # R-multiple target
  Decision(action="ENTER", side="BUY", stop=stop, target=target,
           reason=f"MACD cross up  macd={macd_line:.3f} sig={signal:.3f}")
  ```
- **Short** — `cross_down` is true and (if `require_zero_filter`) `macd_line < 0`:
  ```
  stop   = price + atr_mult · atr      (fallback: price · (1 + stop_pct))
  target = price − reward_mult · (stop − price)
  Decision(action="ENTER", side="SELL", stop=stop, target=target,
           reason=f"MACD cross down  macd={macd_line:.3f} sig={signal:.3f}")
  ```

Notes:
- `stop` and `target` are **absolute price levels** (contract rule 4) and feed the engine's
  intrabar wick detection. They are computed from the **signal-bar close** (`price`); this is
  acceptable — the engine sizes off the actual next-bar fill, and orb does the same.
- Guard: if `atr_mult · atr` (or `stop_pct · price`) rounds to a degenerate stop equal to `price`
  (zero stop distance), return `None` (skip the trade) — the engine would reject `qty<=0` anyway,
  but skipping is cleaner.
- One direction per crossover: because we only enter when flat and the cross is a single-bar
  event, no extra tried-flag is needed. After a fill the position is non-zero and the entry block
  is skipped until flat again.

### 2.2 Exits (only when in a position)

Evaluated **before** entry logic each bar, in this order:

1. **EOD square-off (unconditional, highest priority — contract rule 2):**
   at `t >= 15:30 − squareoff_before_close_min`, if `self.position != 0` →
   `Decision(action="EXIT", reason="EOD square-off")`. Must NOT depend on indicators being ready.
   When flat at/after this time, return `None` (do not open new positions in the square-off window).
2. **Signal exit — opposite crossover (primary exit):**
   - Long open and `cross_down` → `Decision(action="EXIT", reason="MACD cross down (exit long)")`.
   - Short open and `cross_up`  → `Decision(action="EXIT", reason="MACD cross up (exit short)")`.
   The reverse cross flips conviction; we exit on it. (We do **not** auto-reverse into the opposite
   position on the same bar — the engine enforces flat-before-enter and we never assume our ENTER
   filled. The next bar, now flat, the still-true cross can trigger the opposite entry naturally if
   it persists; in practice a single-bar cross is consumed by the exit, which is the intended
   conservative behavior.)
3. **Protective stop via close (defense-in-depth):** the engine's `_intrabar_exit` uses the stored
   `stop`/`target` for wick fills, so the on_tick close-based stop is a backstop for the rare
   close-beyond-stop case:
   - Long: `if price <= stored_stop` → `Decision(action="EXIT", reason=f"Stop-loss ₹{stored_stop:.2f}")`.
   - Short: `if price >= stored_stop` → same with the short stop.
   - Target close-cross is likewise a backstop (engine wick handles the common case):
     Long `price >= stored_target` / Short `price <= stored_target` → EXIT "Target hit".
   The strategy must **store the entry stop/target** at fill time so these checks use the same
   levels the engine stored. See §4 (`notify_fill` records `self._stop` / `self._target`).

If none fire, return `None`.

---

## 3. `MacdCrossoverParams` dataclass

```python
from dataclasses import dataclass

@dataclass
class MacdCrossoverParams:
    fast_period: int = 12
    slow_period: int = 26
    signal_period: int = 9
    atr_period: int = 14
    atr_mult: float = 1.5           # protective stop = atr_mult × ATR from entry close
    reward_mult: float = 1.5        # target = reward_mult × stop-distance (R-multiple)
    stop_pct: float = 0.01          # fallback stop if ATR not ready (1% of price)
    require_zero_filter: bool = False  # if True, long needs macd>0, short needs macd<0
    squareoff_before_close_min: int = 15
```

Parameter notes for the backtest:
- Defaults are the **standard MACD (12/26/9)** plus a 1.5×ATR stop and 1.5R target — a neutral
  starting point comparable to ORB's `target_multiplier=1.5`.
- `require_zero_filter=False` by default (pure crossover). Suggest an A/B with `True`
  (zero-line confirmation) in the IS period; promote only on a meaningfully better OOS Sharpe.
- Keep `slow_period > fast_period` and all periods ≥ 2 (assert in `__init__` if cheap).
- Backtest harness is the contract default: 2024-01-01→2026-06-19, split 2026-01-01, `--n 10`,
  `--slippage-bps 5`, `--equity 500000`, costs from `costs.py`. Register in the engine
  `STRATEGIES` registry as `"macd_crossover": (MacdCrossover, MacdCrossoverParams)`.

---

## 4. State, session reset, and caller feedback

### Instance state (set in `__init__`, reset in `_reset_session`)
```
_session_date: Optional[date]
# EMA / MACD state
_close_buf: list[float]          # warm-up buffer for fast/slow EMA seeding (cap slow_period)
ema_fast, ema_slow: Optional[float]
_ema_seeded: bool
_macd_buf: list[float]           # warm-up buffer for signal EMA seeding (cap signal_period)
signal: Optional[float]
_signal_seeded: bool
prev_hist: Optional[float]       # previous bar's histogram (None until first histogram exists)
# ATR state
_tr_buf: list[float]             # warm-up buffer (cap atr_period)
atr: Optional[float]
_prev_close: Optional[float]
# position view (updated ONLY via notify_fill/notify_flat)
position: int = 0
entry_price: float = 0.0
_stop: float = 0.0               # stored protective stop for the open position
_target: float = 0.0             # stored target for the open position
```

### `notify_fill(side, qty, price)`
Mirror ORB: `self.position += qty if side=="BUY" else -qty`; set `entry_price=price` when
non-flat else 0. **Additionally**, on a fresh entry (position became non-zero) record the
`stop`/`target` that were emitted with the ENTER decision. Implementation: stash the last ENTER
decision's stop/target on `self._pending_stop/_pending_target` when you emit it, then copy them
into `self._stop/_target` here. (Backtest engine stores its own copy from `decision.stop/target`,
so this is for live/backstop parity.)

### `notify_flat()`
`position=0; entry_price=0.0; _stop=0.0; _target=0.0`.

### Session reset list (`_reset_session(today)`) — wipe ALL intraday state on date change
- `_session_date = today`
- `_close_buf = []`, `ema_fast = None`, `ema_slow = None`, `_ema_seeded = False`
- `_macd_buf = []`, `signal = None`, `_signal_seeded = False`, `prev_hist = None`
- `_tr_buf = []`, `atr = None`, `_prev_close = None`
- Do **NOT** reset `position` / `entry_price` / `_stop` / `_target` here — position state is owned
  by notify_fill/notify_flat (a position could be carried across a mid-session restart and must
  still be flattened; ORB follows the same principle by not zeroing position in reset).

### `on_tick` skeleton (order of operations)
1. `if price <= 0: return None`.
2. **Future-skew guard** — copy ORB verbatim (ignore ticks > `MAX_FUTURE_SKEW` ahead; compare in
   IST, tolerate naive/aware). A future-stamped tick must not reset the session.
3. `today = now.date()`; if `self._session_date != today: self._reset_session(today)`.
4. **Update indicators with THIS bar** (close + high/low): advance ATR, then EMAs/MACD/signal,
   compute `hist`. Keep `prev_hist` = the histogram from BEFORE this update (snapshot it before
   overwriting). Update `_prev_close = price` at the end.
5. **EOD square-off** check (unconditional) — return EXIT if `position != 0` and in window; return
   `None` if flat and in window.
6. **Exit checks** (if `position != 0`): opposite-cross signal exit, then stored stop/target close
   backstop. Return the EXIT if any fire.
7. **Entry checks** (only if `position == 0` and indicators ready): cross_up/cross_down (+ optional
   zero filter) → ENTER with computed stop/target. Stash pending stop/target for `notify_fill`.
8. Otherwise `None`.

> Indicators are updated every bar (step 4) regardless of position, so the histogram series is
> continuous and crossovers are detected whether or not we hold a position. The EOD/exit/entry
> gating only controls what Decision we emit, never whether indicators advance.

---

## 5. Unit-test cases (input bars → expected Decision)

All bars are 1-minute, session 2024-01-02 (a weekday), starting 09:15 IST, fed in order to one
`MacdCrossover("TEST")` instance. `now` is IST-aware. Unless noted, `high=low=close` (flat bars)
to keep ATR/levels deterministic. Use default params. Where a "ramp" is needed to warm up, feed a
constant or trending close series for bars 1..34 then craft the crossover at bar ≥35.

> Helper for tests: a constant close for the first 34 bars makes `ema_fast == ema_slow == close`,
> so `macd_line == 0`, `signal == 0`, `hist == 0` at warm-up completion — a clean zero baseline to
> trigger crosses from.

1. **Warm-up returns None.** Feed 34 bars of constant close=100. Each `on_tick` returns `None`
   (indicators not crossover-ready until bar 35). Assert no Decision before bar 35.

2. **Long entry on cross up.** After 34 constant bars at 100 (baseline hist≈0), feed bar 35 with a
   higher close (e.g. 100→ rising series that pushes `ema_fast > ema_slow` so `macd_line>0` and
   `hist>0` from `prev_hist<=0`). Expect `Decision(action="ENTER", side="BUY")` with `stop < price`
   and `target > price`, reason starting "MACD cross up".
   *(Concretely: bars 1–34 = 100; bar 35 = 101, bar 36 = 103, … feed an up-ramp; the first bar
   where hist turns positive yields the BUY. Test asserts the side and that stop<close<target.)*

3. **Short entry on cross down.** Symmetric to (2): warm up flat at 100, then a down-ramp
   (101→97…) drives `macd_line<0`, `hist<0` from `prev_hist>=0`. Expect
   `Decision(action="ENTER", side="SELL")`, `stop > price`, `target < price`, reason "MACD cross
   down".

4. **No entry while already long.** Continue from (2) after `notify_fill("BUY", qty, fill)`. Feed
   another cross-up bar (hist stays positive / re-crosses up). Expect `None` (position != 0 blocks
   entry; no opposite-cross, no stop hit).

5. **Signal exit on opposite cross (long → flat).** From an open long (after notify_fill), feed a
   down-ramp that flips `hist` negative (`cross_down`). Expect
   `Decision(action="EXIT", reason="MACD cross down (exit long)")`. (Position not auto-reversed.)

6. **Signal exit on opposite cross (short → flat).** Symmetric to (5): open short, feed cross_up,
   expect `Decision(action="EXIT", reason="MACD cross up (exit short)")`.

7. **Protective stop (close backstop) for a long.** Open long at entry≈100 with stored
   `_stop≈98.5` (1.5×ATR). With NO opposite cross, feed a bar whose close drops below `_stop`
   (e.g. close=98.0). Expect `Decision(action="EXIT", reason=startswith "Stop-loss")`.
   *(The engine wick path normally handles this; the test exercises the on_tick backstop directly
   by passing a close below the stored stop.)*

8. **Target backstop for a long via close.** Open long, no opposite cross, feed a bar whose close
   ≥ stored `_target`. Expect `Decision(action="EXIT", reason=startswith "Target hit")`.

9. **EOD square-off is unconditional.** Open long. Feed a bar at `now = 15:16 IST`
   (≥ 15:30 − 15min) with an arbitrary close that triggers NO indicator exit. Expect
   `Decision(action="EXIT", reason="EOD square-off")`. Then feed a flat (position via notify_flat)
   bar at 15:20 → expect `None` (no new entries in the square-off window even on a fresh cross).

10. **Session reset on date change.** Warm up and open a long on day 1. Feed the first bar of day
    2 (`now.date()` advanced). Assert indicator state is wiped (no Decision can fire from day-1
    crossover history — first day-2 cross-eligible bar is again bar 35). Position state (if still
    held) is NOT wiped by reset, but the EOD square-off on day 1 should already have flattened it
    in a realistic replay; this test feeds day 2 from flat and asserts warm-up restarts (returns
    `None` for the first 34 day-2 bars).

11. *(Optional)* **Zero-line filter blocks a cross.** With `require_zero_filter=True`: construct a
    `cross_up` where `macd_line` is still **negative** at the cross bar. Expect `None` (filter
    rejects the long); then a later bar where `macd_line>0` on cross_up yields the BUY.

12. *(Optional)* **ATR-not-ready fallback stop.** Force `atr=None` at entry (e.g. monkeypatch or a
    session shorter than `atr_period` via crafted indices) and confirm the long stop falls back to
    `price·(1−stop_pct)`.

---

## 6. Implementation checklist (parity with ORB)

- [ ] Pure, synchronous, IO-free class in `strategies/macd_crossover.py`.
- [ ] `from strategies.orb import Decision` (reuse — do not redefine). Reuse `IST`, `MARKET_OPEN`,
      `MARKET_CLOSE`, `MAX_FUTURE_SKEW` constants (import or re-declare identically).
- [ ] `on_tick(self, now, price, high=None, low=None) -> Optional[Decision]` exact signature.
- [ ] Future-skew guard copied from ORB.
- [ ] Session reset on date change wipes the full §4 indicator list.
- [ ] EOD square-off unconditional and indicator-independent.
- [ ] ENTER carries absolute `stop` and `target`; store them for the on_tick stop/target backstop.
- [ ] One position at a time; position tracked only via notify_fill/notify_flat.
- [ ] Indicators incremental, session-scoped, no look-ahead; warm-up returns None.
- [ ] Register in engine `STRATEGIES` as `"macd_crossover"`; runs under `--gate none|kronos`
      unchanged; ORB regression tests remain byte-for-byte identical.
- [ ] Add `tests/test_macd_crossover.py` covering cases §5.1–§5.10 (and optional 11–12).
