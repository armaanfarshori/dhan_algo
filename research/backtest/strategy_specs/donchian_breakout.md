# Donchian Channel Breakout (turtle-style, intraday) — strategy spec

Planning deliverable per `_CONTRACT.md`. Implement in `strategies/donchian_breakout.py` as a pure,
synchronous, IO-free class mirroring `strategies/orb.py`. Reuse `Decision` from `strategies/orb.py`
(do NOT redefine it). Reuse the IST / `MARKET_OPEN` / `MARKET_CLOSE` / `MAX_FUTURE_SKEW` constants
and the future-skew guard verbatim from ORB.

---

## 1. Idea (concrete)

A classic turtle breakout, intraday-only. Maintain a rolling Donchian channel over the last `N`
**completed** bars (default exclude the current forming bar — see `exclude_current_bar`):

- **Upper channel** = highest `high` of the last `N` bars.
- **Lower channel** = lowest `low` of the last `N` bars.

Enter LONG when the current bar's close (`price`) breaks **above** the upper channel; enter SHORT
when it breaks **below** the lower channel. Protective stop is the opposite channel edge (with a
buffer) but capped to a max ATR-based distance so a very wide channel does not produce a tiny
position / huge nominal risk. Target is a multiple of the entry's risk (R-multiple). Exit on stop,
target, opposite-channel break (signal exit), or unconditional EOD square-off.

**One breakout attempt per side per session** — mirror ORB's `_long_tried` / `_short_tried` flags.

### Justification for one-attempt-per-side
This matches ORB's proven structure and the engine's expectations, and it is the conservative,
honest choice for an intraday turtle:
- The first clean breakout of the session's developing range is the highest-quality signal; later
  re-breaks of the same channel are usually chop/whipsaw and inflate cost (each round trip pays the
  full Indian intraday stack via `costs.py`).
- It bounds trade count per name per day (≤ 2 entries: at most one long + one short), keeping the
  backtest comparable to ORB and avoiding a degenerate strategy that thrashes in a ranging session.
- It removes a free re-entry knob that would otherwise need its own tuning/over-fitting.

A `tried` side is **set the moment we EMIT the ENTER** (not on fill) — identical to ORB — so a
rejected/un-filled order still consumes the attempt. This is intentional: we never assume our ENTER
executed (Decision protocol), and we do not want to chase the same breakout repeatedly.

---

## 2. Params dataclass

```python
@dataclass
class DonchianBreakoutParams:
    channel_period: int = 20            # N — bars in the Donchian channel
    exclude_current_bar: bool = True    # True: channel = last N COMPLETED bars (no self-confirm);
                                        # False: include the current bar in the channel
    warmup_skip_open_min: int = 15      # do not arm entries until this many min after MARKET_OPEN
                                        # (let the open settle; mirrors ORB's open-range intent)
    atr_period: int = 14                # ATR window (Wilder) for the stop cap
    sl_atr_mult: float = 1.5            # stop distance cap = sl_atr_mult × ATR
    sl_buffer_pct: float = 0.001        # extra padding beyond the channel-edge stop
    target_r_multiple: float = 2.0      # target = entry ± target_r_multiple × (entry − stop)
    min_channel_pct: float = 0.003      # skip if channel width < this fraction of price (chop guard)
    squareoff_before_close_min: int = 15
```

Notes / backtest knobs:
- `channel_period` is the headline tunable (try 10 / 20 / 40 in IS).
- `exclude_current_bar=True` is the correct default — including the current bar means the bar that
  breaks out is itself part of the channel, which makes the break self-referential and weakens the
  signal. Keep both code paths (see §4) so the choice is testable.
- `warmup_skip_open_min` guarantees we have a stable channel and avoids the volatile first minutes;
  combined with `channel_period` it sets the real warm-up (see §5).
- `min_channel_pct` is the chop guard, analogous to ORB's `min_range_pct`.

---

## 3. Decision mapping (entry / exit / stop / target / EOD)

All levels are **absolute prices**. `price` = bar close; `high`/`low` = current bar extremes.

### Entry — LONG (mirror for SHORT)
Condition (only when armed, flat, side not yet tried, channel ready, past warm-up window):
- `price > upper_channel` and channel width ≥ `min_channel_pct × price`.

On trigger:
```
stop_raw  = lower_channel * (1 - sl_buffer_pct)          # opposite channel edge + buffer
stop_cap  = price - sl_atr_mult * atr                    # ATR-capped (tighter of the two)
stop      = max(stop_raw, stop_cap)                       # LONG: higher stop = smaller risk
risk      = price - stop                                  # must be > 0; if <= 0 skip (no trade)
target    = price + target_r_multiple * risk
→ Decision(action="ENTER", side="BUY", stop=stop, target=target,
           reason=f"Donchian long breakout  UP={upper:.2f} LO={lower:.2f} N={channel_period}")
self._long_tried = True
```

### Entry — SHORT
- `price < lower_channel` and channel width ≥ `min_channel_pct × price`.
```
stop_raw  = upper_channel * (1 + sl_buffer_pct)
stop_cap  = price + sl_atr_mult * atr
stop      = min(stop_raw, stop_cap)                       # SHORT: lower stop = smaller risk
risk      = stop - price                                  # must be > 0; else skip
target    = price - target_r_multiple * risk
→ Decision(action="ENTER", side="SELL", stop=stop, target=target,
           reason=f"Donchian short breakdown  UP={upper:.2f} LO={lower:.2f} N={channel_period}")
self._short_tried = True
```

`atr` may be 0/None during warm-up; if `atr` is not ready, fall back to `stop = stop_raw` (channel
edge only). If `risk <= 0` after capping (degenerate channel), return None — do not emit a trade.

### Exits for an OPEN position (checked before entries, like ORB §4)
The engine's `_intrabar_exit` will handle stop/target via wicks using the **stored** stop/target
captured at ENTER (per the contract's engine refactor — the engine no longer reads strategy
internals). So `on_tick` only needs to emit:

1. **Stop / target on close** (belt-and-suspenders; engine wick check is primary):
   - LONG: `price <= stored_stop` → `EXIT reason="Stop-loss ₹{stop:.2f}"`;
           `price >= stored_target` → `EXIT reason="Target hit ₹{target:.2f}"`.
   - SHORT: mirror.
   The strategy stores its own `self._stop` / `self._target` at ENTER for this purpose (see §6).
2. **Opposite-channel signal exit** (the turtle exit):
   - LONG open and `price < lower_channel` → `Decision(action="EXIT", reason="Opposite channel break")`.
   - SHORT open and `price > upper_channel` → `Decision(action="EXIT", reason="Opposite channel break")`.
   This makes the strategy genuinely channel-following: it gets out when the channel reverses even
   before the fixed stop. Compute `lower_channel`/`upper_channel` from the SAME maintained channel.

### EOD square-off (unconditional — contract rule 2)
At `t >= (MARKET_CLOSE − squareoff_before_close_min)`:
```
if self.position != 0: return Decision(action="EXIT", reason="EOD square-off")
return None
```
This block runs ABOVE the channel-ready / warm-up gates (copy ORB §3 ordering) so an open position
always flattens even if indicators were reset by a mid-session restart.

### Ordering inside on_tick (exact)
1. `price <= 0` → None.
2. Future-skew guard (verbatim from ORB).
3. `today = now.date()`; if `self._session_date != today` → `_reset_session(today)`.
4. Update the rolling channel + ATR with this bar (see §4). (Always update, even pre-warm-up, so the
   buffers fill.)
5. **EOD square-off** check — unconditional, returns EXIT if position open.
6. If position open → exit checks (stored stop/target on close, then opposite-channel signal exit).
7. Warm-up / armed gate: if channel not ready OR `t < (MARKET_OPEN + warmup_skip_open_min)` → None.
8. Chop guard: if channel width `< min_channel_pct * price` → None.
9. Entries (LONG then SHORT), respecting `_long_tried` / `_short_tried`.

---

## 4. Incremental indicator maintenance (no lookahead, no cross-session leak)

### Rolling N-bar high / low — monotonic deque (O(1) amortized)
Maintain TWO `collections.deque` of `(index, value)` pairs plus a per-session bar counter `self._i`:

- **For the high channel** keep a *decreasing* deque of highs: when a new `high` arrives, pop from
  the right while `right.value <= high`, then append `(i, high)`. The left end is always the max.
- **For the low channel** keep an *increasing* deque of lows: pop from the right while
  `right.value >= low`, then append `(i, low)`. The left end is always the min.
- **Eviction (window of size N):** when the left element's index `<= i - N`, popleft. This bounds
  each deque to the last N bars.

Window definition with `exclude_current_bar`:
- `exclude_current_bar=True` (default): compute `upper_channel`/`lower_channel` from the deques
  **as they were BEFORE inserting the current bar** — i.e. read `upper`/`lower` first, THEN push the
  current bar. So the channel reflects the prior N completed bars; the current bar cannot confirm its
  own breakout. (Equivalently: window = bars `[i-N, i-1]`.)
- `exclude_current_bar=False`: push the current bar first, then read — channel includes the current
  bar (window = bars `[i-N+1, i]`).

`upper_channel` = value at left of the high-deque; `lower_channel` = value at left of the low-deque.
A simple `deque(maxlen=N)` of raw highs/lows with `max()`/`min()` is an acceptable O(N) alternative
(N is small, ≤ 40) if the monotonic version is deemed fiddly — implementer's choice, but document it.

**Channel-ready** = we have observed ≥ `N` bars this session for the chosen window
(`exclude_current_bar=True` needs N prior bars → ready when `self._i >= N`; `False` → ready when
`self._i >= N`). Until ready, channel reads are undefined → treat as not-ready (return None at the
warm-up gate).

### ATR (Wilder) — incremental
Maintain `self._prev_close`, `self._atr`, `self._atr_count`:
- True range `tr = max(high - low, abs(high - prev_close), abs(low - prev_close))` (first bar of the
  session has no `prev_close` → `tr = high - low`).
- Seed: accumulate the first `atr_period` TRs as a simple average → initial `self._atr`.
- After seeding (Wilder smoothing): `self._atr = (self._atr * (atr_period - 1) + tr) / atr_period`.
- ATR-ready = `self._atr_count >= atr_period`. If not ready at entry time, fall back to channel-edge
  stop only (no ATR cap), per §3.
- Update `self._prev_close = close` at the END of each bar's processing.

All buffers (deques, counters, ATR state, prev_close) are reset in `_reset_session` — no leakage
across days.

---

## 5. Warm-up handling

Two gates combine; entries are blocked until BOTH pass:
1. **Channel ready** — ≥ `channel_period` bars observed this session (§4).
2. **Open-settle window** — `now.time() >= MARKET_OPEN + warmup_skip_open_min`.

With defaults (`N=20`, 1-min bars, `warmup_skip_open_min=15`), the open-settle gate (≥ 09:30) is the
binding one early; on a normal session the channel is ready well before 09:30, so the first eligible
entry is ~09:30. ATR (period 14) is ready by ~09:29 and so is essentially always available for the
stop cap; the fallback path exists only for short/gappy sessions.

Channel/ATR buffers are still UPDATED during warm-up (step 4 runs unconditionally) — only entry
emission is gated.

---

## 6. State + session reset

Instance state (set in `__init__`, cleared in `_reset_session`):
```
self._session_date        # date | None
self._i                   # int  — bars seen this session (0-based counter)
self._hi_deque            # deque of (i, high)  monotonic-decreasing
self._lo_deque            # deque of (i, low)   monotonic-increasing
self._prev_close          # float | None
self._atr                 # float
self._atr_count           # int
self._long_tried          # bool
self._short_tried         # bool
# position view — updated ONLY via notify_fill/notify_flat (copy ORB verbatim)
self.position             # int
self.entry_price          # float
self._stop                # float — stored stop of the open position (for close-based exit checks)
self._target              # float — stored target of the open position
```

`_reset_session(today)` resets: `_session_date=today`, `_i=0`, both deques cleared, `_prev_close=None`,
`_atr=0.0`, `_atr_count=0`, `_long_tried=False`, `_short_tried=False`. It does NOT touch `self.position`
/ `entry_price` / `_stop` / `_target` (those are owned by notify_fill/notify_flat / set at ENTER —
a position carried across a mid-session restart must still be exitable). `notify_flat` clears
`_stop`/`_target` to 0.0.

`notify_fill` / `notify_flat`: copy ORB's bodies verbatim (update `self.position`, `self.entry_price`).
Set `self._stop = decision.stop` and `self._target = decision.target` at the point on_tick emits the
ENTER (so they are known before the fill notification, used by the close-based exit checks).

---

## 7. Unit-test cases (bars → expected Decision)

Use a small `channel_period` for tractable tests (e.g. `N=3`, `exclude_current_bar=True`,
`warmup_skip_open_min=0`, `min_channel_pct=0.0`, `sl_atr_mult` large or ATR-not-ready so the stop is
the channel edge) unless a case says otherwise. Feed bars via `on_tick(now, close, high, low)` with
IST timestamps on the same date. Times use 1-min spacing from 09:15. `notify_fill`/`notify_flat`
must be called by the test to reflect fills, exactly as the engine does.

Let helper `tk(hh, mm, close, high, low)` build a tick at that IST time.

1. **Warm-up returns None.** N=3. Feed 2 bars (09:15, 09:16) of any shape → both `on_tick`
   return None (channel not ready, need ≥ 3 prior bars).

2. **Long breakout emits ENTER BUY with correct levels.** N=3, ATR not ready (fallback to channel
   edge). Bars: 09:15 (c=100,h=101,l=99), 09:16 (c=100,h=102,l=98), 09:17 (c=100,h=103,l=97). At
   09:18 feed close=104, high=104, low=100. Prior-3 channel: upper=103, lower=97. `104 > 103` →
   `Decision(action="ENTER", side="BUY")`, `stop ≈ 97*(1-0.001)=96.903`, `target = 104 + 2*(104-96.903)`
   ≈ 118.19. Assert action/side, stop < entry, target > entry, and `_long_tried` is now True.

3. **Short breakdown emits ENTER SELL.** Symmetric to #2: build a descending channel so prior-3
   lower=97-ish and feed a close below it (e.g. upper=103/lower=97, bar close=96 < 97) →
   `Decision(action="ENTER", side="SELL")`, `stop ≈ 103*1.001`, `target = 96 - 2*(stop-96)`,
   `_short_tried` True.

4. **One-attempt-per-side: second long breakout is suppressed.** After case #2's ENTER, call
   `notify_fill("BUY", qty, 104)` then `notify_flat()` (simulate a quick round trip). Feed another
   close above the (now higher) upper channel → `on_tick` returns None for the LONG side because
   `_long_tried` is True. (A SHORT breakdown in the same session would still be allowed.)

5. **No breakout inside the channel → None.** After warm-up with upper=103/lower=97, feed
   09:18 close=100 (h=102,l=98) → within channel → None.

6. **`exclude_current_bar=True` does not let the current bar confirm itself.** N=3. Prior-3
   upper=103. At 09:18 feed a bar whose HIGH=110 but CLOSE=102 (h=110,l=101). With exclusion the
   channel is still 103 (current bar excluded) and `close=102 < 103` → None (no entry). Then verify
   that with `exclude_current_bar=False` on a fresh instance the same bar makes upper=110 (current
   bar included) and a close of 102 is below it → still None, but assert `upper_channel` differs
   (103 vs 110) to prove the flag changes the window. (Channel-introspection via a `status()`/test
   accessor.)

7. **Opposite-channel signal exit on an open LONG.** Open a LONG (case #2), call
   `notify_fill("BUY", q, 104)`. Now feed a bar whose close drops below the current lower channel
   (e.g. lower=97, close=96) → `Decision(action="EXIT", reason="Opposite channel break")` even
   though the fixed stop (96.9) may not be hit on close. (If the close also breaches the stop, the
   stop-loss EXIT reason is acceptable too — assert action=="EXIT".)

8. **Stop-loss EXIT on close for open LONG.** Open LONG at 104 with stop≈96.9. Feed close=96.5
   (h=98,l=96) → `Decision(action="EXIT", reason startswith "Stop-loss")`.

9. **Target EXIT on close for open LONG.** Open LONG at 104, target≈118.19. Feed close=119
   (h=120,l=118) → `Decision(action="EXIT", reason startswith "Target hit")`.

10. **EOD square-off is unconditional.** Open a LONG, then feed a tick at 15:16
    (`squareoff_before_close_min=15` → square-off at 15:15) with ANY price, and crucially BEFORE the
    channel is ready / on a fresh session (set `self.position` via notify_fill, reset indicators) →
    `Decision(action="EXIT", reason="EOD square-off")`. With `self.position == 0`, the same tick →
    None.

11. **Session reset clears tried flags + channel.** Run case #2 to set `_long_tried=True` on
    2024-01-01. Feed a tick dated 2024-01-02 → indicators reset; `on_tick` returns None during the
    new day's warm-up, and after re-warming a long breakout is allowed again (proves no
    cross-session leakage of `_long_tried` or the channel buffers).

12. **Future-skew guard.** A tick stamped > 2 min ahead of wall clock → None and does NOT advance
    the session date (copy ORB's test).

(10–12 are the contract-mandated safety cases; 1–9 cover the channel logic. Pick 6–10 to implement;
1, 2, 3, 4, 6, 7, 10, 11 are the highest-value set.)

---

## 8. Engine / backtest notes

- Register in the engine: `STRATEGIES["donchian"] = (DonchianBreakout, DonchianBreakoutParams)`;
  expose `--strategy donchian`. The engine captures `decision.stop`/`decision.target` at ENTER and
  uses them in `_intrabar_exit` (per the contract's engine refactor) — this strategy relies on that
  STORED-level behavior; do NOT add Donchian-specific reads to `_intrabar_exit`.
- Same harness as ORB for comparability: `2024-01-01 → 2026-06-19`, `--split-date 2026-01-01`,
  `--n 10`, `--slippage-bps 5`, `--equity 500000`, costs via `costs.py`. OOS Sharpe is the bar.
- Gate plumbing (`--gate none|kronos`) works unchanged — the engine calls `gate_fn` on the ENTER
  side, and `_long_tried`/`_short_tried` are already consumed at emit time (same semantics as ORB),
  so a gate-blocked breakout still consumes the attempt. This matches ORB and is intentional.
- IS sweep candidates: `channel_period ∈ {10,20,40}`, `target_r_multiple ∈ {1.5,2.0,3.0}`,
  `sl_atr_mult ∈ {1.0,1.5,2.0}`, `exclude_current_bar ∈ {True,False}`. Lock the best on IS, report
  OOS untouched.
- Survivorship = CEILING (current scrip master) — state it in the report, same as ORB/M3.
