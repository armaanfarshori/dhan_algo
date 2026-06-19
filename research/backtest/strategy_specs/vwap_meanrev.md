# Strategy spec — VWAP Mean-Reversion (`vwap_mr`)

Target file: `strategies/vwap_meanrev.md` → implementation `strategies/vwap_meanrev.py`.
Mirror `strategies/orb.py` exactly (pure, synchronous, IO-free; ticks in, `Decision` out).
Reuse `Decision` from `strategies/orb.py` — **do not redefine it**.

This document is the complete build spec. A coder implements it with zero further research.

---

## 1. Idea

Anchor a **session VWAP** (cumulative typical-price × volume / cumulative volume, reset every
session). Compute a **dispersion band** around VWAP from the running stdev of `(typical_price − vwap)`.
The session VWAP behaves as a mean: when price stretches `k` band-widths *above* VWAP we **fade it
short** (expecting reversion down to VWAP); when it stretches `k` band-widths *below* we **buy**
(expecting reversion up to VWAP). The reversion **target is VWAP itself**; the protective **stop is
placed a further `stop_band_mult` band-widths beyond the entry extreme** (i.e. further from VWAP).
EOD square-off is unconditional.

This is the opposite posture to ORB (breakout): it profits when intraday excursions revert. Run on
the same engine/costs/universe/OOS split for a direct comparison.

---

## 2. **Interface change REQUIRED — volume must reach the strategy**

> **FINAL (shipped) interface — this is what the code does now:**
> `on_tick(now, price, high=None, low=None, volume=None)`. The backtest engine passes the bar's
> volume **directly** into `on_tick` (`engine.py` step 3:
> `strategy.on_tick(ts, bar_close, high=bar_high, low=bar_low, volume=float(bar["volume"]))`).
> There is **no** separate `on_bar_volume`/`notify_bar` setter — Option A below was adopted as-is.
> Strategies that take prior-day levels expose `seed_prior_day(prior_high, prior_low, prior_close)`.

`on_tick(now, price, high, low)` does **not** carry volume. VWAP and the band **cannot** be computed
without per-bar volume. This is a hard blocker that the wiring (engine + live runner) owner must
resolve. Two acceptable options — **pick option A**; it is the least intrusive and keeps ORB
byte-for-byte unchanged:

### Option A (CHOSEN) — add an optional `volume` kwarg to `on_tick`
Extend the shared signature to:

```python
def on_tick(self, now: datetime, price: float,
            high: Optional[float] = None, low: Optional[float] = None,
            volume: Optional[float] = None) -> Optional[Decision]:
```

- `volume` = the **current bar's traded volume** (the `volume` column already loaded by
  `engine.load_day_bars` — it is in the DataFrame but currently dropped on the call).
- `volume` is **optional with default `None`** → ORB ignores it; ORB's signature, body and tests
  remain unchanged (it simply never reads the new kwarg). No regression.
- **Backtest wiring** (`research/backtest/engine.py`, step 3): change
  `decision = strat.on_tick(ts, bar_close, high=bar_high, low=bar_low)`
  → `... , low=bar_low, volume=float(bar["volume"]))`. The `volume` column is already in `df`.
- **Live wiring** (`engine/runner.py` / `StrategyRunner`): pass the just-closed bar's volume from
  `BarBuilder`. The bar builder already accumulates per-minute volume; thread it into the `on_tick`
  call. If a live path genuinely cannot supply volume for a tick, it passes `None` and VWAP-MR
  treats that bar as **volume = 0** (see §5 warm-up / degenerate handling) so it never crashes —
  but the backtest (the comparison that matters) always has real volume.

> **Required interface change, stated for the wirer:** add `volume: Optional[float] = None` to the
> `on_tick` signature in the strategy protocol; have the backtest engine pass `float(bar["volume"])`
> and the live runner pass the closed bar's volume. ORB is unaffected (optional kwarg, unused).

### Option B (NOT chosen, documented for completeness) — `notify_bar(volume)` hook
A separate `notify_bar(self, volume: float)` called by the engine immediately **before** each
`on_tick`, stashing volume on `self._last_volume`. Rejected because it splits one bar's data across
two calls (ordering-fragile) and still needs engine changes. Option A is cleaner.

**Fields the strategy needs from each bar:** `now` (IST datetime), `price` (= bar **close**),
`high`, `low`, and **`volume`**. Typical price is derived as `(high + low + close) / 3` (see §5);
if `high`/`low` are `None` it falls back to `close` for both (degenerate-but-safe).

---

## 3. Exact rules → `Decision`

All levels are absolute prices. `Decision` carries `stop` and `target`; the engine's
strategy-agnostic `_intrabar_exit` uses the **stored** stop/target for wick detection (per the
contract's engine-refactor note), so the band/VWAP values at *entry time* define the exit levels and
**do not float** with later bars. The reversion exit-to-VWAP is additionally emitted as an `on_tick`
EXIT (close crossing VWAP), because VWAP drifts after entry and the stored static `target` is only a
backstop.

Let, on the current bar (after the incremental update of §5):
- `vwap` = session VWAP
- `band` = band width (a price distance, ≥ 0; see §5 for the two methods)
- `upper = vwap + k * band`, `lower = vwap - k * band`  (entry trigger levels, `k = entry_band_mult`)

### 3.1 ENTRY (only when flat: `self.position == 0`)
Guarded by warm-up (`_ready`, §6) **and** a minimum-band filter (`band >= price * min_band_pct`;
skip dead-flat sessions where reversion edge ≈ costs). One entry per direction per session is
allowed but re-entry after a completed round trip in the same session **is permitted** (unlike ORB's
one-shot tried-flags) — capped by `max_entries_per_session`.

**SHORT (fade the stretch up):** if `price >= upper`:
```
entry_ref = price                       # signal price (engine fills next-bar open)
target    = vwap                         # revert down to the mean
stop      = upper + stop_band_mult * band   # further ABOVE, beyond the band
Decision(action="ENTER", side="SELL", stop=stop, target=target,
         reason=f"VWAP-MR short  px={price:.2f} vwap={vwap:.2f} band={band:.2f}")
```

**LONG (fade the stretch down):** if `price <= lower`:
```
target = vwap                            # revert up to the mean
stop   = lower - stop_band_mult * band   # further BELOW, beyond the band
Decision(action="ENTER", side="BUY", stop=stop, target=target,
         reason=f"VWAP-MR long  px={price:.2f} vwap={vwap:.2f} band={band:.2f}")
```

If both `price >= upper` and `price <= lower` were ever simultaneously true (impossible unless
`band==0`), the min-band filter has already returned None. SHORT is tested before LONG.

### 3.2 EXIT — reversion to VWAP (signal exit via `on_tick`)
While in a position, emit an EXIT when the **close crosses back to/through VWAP** (target met):
- LONG (`position > 0`): if `price >= vwap` → `Decision(action="EXIT", reason="VWAP target")`.
- SHORT (`position < 0`): if `price <= vwap` → `Decision(action="EXIT", reason="VWAP target")`.

This is the *primary* profit exit (VWAP moves after entry; the static stored `target` from §3.1 is a
backstop the engine's wick check may also catch). The stored static **stop** remains the protective
exit — the engine's `_intrabar_exit` fires it on a wick beyond the stored stop level.

### 3.3 STOP (protective)
Carried in the ENTER `Decision.stop` (formulas above). The engine stores it and detects the wick hit
intrabar (gap-aware, stop-beats-target). The strategy does **not** need to re-emit the stop in
`on_tick` (the engine owns intrabar wick stops), but **as a belt-and-braces** also emit a close-based
stop EXIT so a live tick stream that closes beyond the stop is caught:
- LONG: if `price <= stored_stop` → `Decision(action="EXIT", reason="VWAP-MR stop")`.
- SHORT: if `price >= stored_stop` → `Decision(action="EXIT", reason="VWAP-MR stop")`.

The strategy stores its own `self._stop` / `self._target` at the moment it emits ENTER (it cannot
rely on reading them back from the engine). On `notify_flat` these reset to `0.0`.

**Exit precedence inside `on_tick` when in a position** (evaluate in this order, return first match):
1. EOD square-off (§3.4) — unconditional, above everything.
2. Protective stop (§3.3) — realise loss before considering target.
3. VWAP reversion target (§3.2).

### 3.4 EOD square-off (unconditional)
At `t >= 15:30 − squareoff_before_close_min`, if `self.position != 0` return
`Decision(action="EXIT", reason="EOD square-off")`. **Must not depend on `_ready`/band/VWAP** — a
DB-reconciled position after a mid-session restart must still flatten. Place this check *before* the
warm-up gate, exactly as ORB section 3.

### 3.5 No new entries near the close
Do not open a new position within `no_entry_before_close_min` of the close (no time for reversion).
Implement as: in the ENTRY block, require `t < (15:30 − no_entry_before_close_min)`.

---

## 4. `VwapMeanRevParams` dataclass (fields + defaults)

```python
from dataclasses import dataclass

@dataclass
class VwapMeanRevParams:
    band_method: str = "resid_std"   # "resid_std" | "typical_std"  (see §5)
    entry_band_mult: float = 2.0     # k — entry trigger = vwap ± k*band
    stop_band_mult: float = 1.0      # protective stop = trigger ± this*band beyond the band
    warmup_bars: int = 20            # bars required before any signal (band statistic stable)
    min_band_pct: float = 0.0015     # skip if band < this fraction of price (dead session)
    max_entries_per_session: int = 3 # re-entry cap after completed round trips
    squareoff_before_close_min: int = 15
    no_entry_before_close_min: int = 30
    ewma_alpha: float = 0.0          # 0 => use plain cumulative session stats;
                                     # >0 => EWMA the residual variance (recency-weighted band)
```

Defaults chosen to be sane and comparable to ORB; the backtest will tune `entry_band_mult`,
`stop_band_mult`, `band_method`, and `min_band_pct`.

---

## 5. Incremental indicator math (no lookahead, no cross-session leakage)

All state is per-session and reset on date change (§7). Updated **once per bar inside `on_tick`,
BEFORE** entry/exit logic reads `vwap`/`band`. Use the **just-closed bar's** values only.

Per bar inputs: `close = price`, `high`, `low`, `volume` (`v`). If `high`/`low` is `None`, use
`close` for it. If `volume` is `None` or `< 0`, treat `v = 0.0` (bar contributes nothing to VWAP;
prevents crash on degenerate live ticks).

**Typical price:** `tp = (high + low + close) / 3.0`.

**Cumulative VWAP (Welford-free, exact):**
```
self._cum_pv  += tp * v          # Σ (tp · volume)
self._cum_vol += v               # Σ volume
vwap = self._cum_pv / self._cum_vol   if self._cum_vol > 0 else tp
```
(When no volume yet — first bar(s) of session — `vwap` falls back to `tp`; band is 0 → no signal,
which is correct because warm-up also blocks.)

**Band — two methods (selected by `band_method`):**

### `resid_std` (DEFAULT, recommended) — stdev of the VWAP residual
Track the running mean/variance of `resid = (tp − vwap)` across the session's bars using **Welford's
online algorithm** (numerically stable, single pass). Per bar, after `vwap` is updated:
```
resid = tp - vwap
self._n      += 1
delta        = resid - self._mean
self._mean   += delta / self._n
delta2        = resid - self._mean
self._m2     += delta * delta2
var          = self._m2 / (self._n - 1)   if self._n >= 2 else 0.0
band         = sqrt(max(var, 0.0))
```
`var` uses sample stdev (`n-1`). `band = 0` until `n >= 2`.

**Optional EWMA variant** (`ewma_alpha > 0`): replace the cumulative residual variance with an
exponentially weighted one so the band tracks recent dispersion:
```
a = ewma_alpha
if self._ew_init is False: self._ew_mean = resid; self._ew_var = 0.0; self._ew_init = True
else:
    diff = resid - self._ew_mean
    incr = a * diff
    self._ew_mean += incr
    self._ew_var = (1 - a) * (self._ew_var + diff * incr)
band = sqrt(max(self._ew_var, 0.0))
```
(Default `ewma_alpha=0.0` → plain Welford path above.)

### `typical_std` (alternative) — rolling stdev of typical price
Same Welford recurrence but fed `tp` (not the residual). Captures absolute price dispersion rather
than dispersion *around* VWAP. Selected only when `band_method == "typical_std"`. Implement with the
same Welford fields (one stat path; the only difference is whether you feed `resid` or `tp`).

> Implementation note: keep ONE Welford block parameterised by the fed value (`resid` vs `tp`) to
> avoid duplicate code. EWMA likewise wraps that fed value.

**Why residual-std is the default:** the band should measure how far price typically strays *from
VWAP*; that is exactly `std(tp − vwap)`. `typical_std` is offered for the A/B but tends to overstate
the band when VWAP itself is trending.

---

## 6. Warm-up handling

- `_ready` is True only when `self._n >= params.warmup_bars` **and** `self._cum_vol > 0` **and**
  `band > 0`. Until then `on_tick` returns `None` for entries (but EOD square-off and exits for an
  already-open reconciled position still work — they sit above the warm-up gate, like ORB).
- Rationale: a band from < ~20 bars is noisy; entering on it fades noise, not a real stretch.
- The min-band filter (`band >= price * min_band_pct`) is an additional, separate gate applied at
  entry time even after `_ready`.

---

## 7. Session-reset list (reset ALL of these on `now.date()` change → `_reset_session`)

| State | Reset to | Purpose |
|---|---|---|
| `self._session_date` | `today` | date tracking |
| `self._cum_pv` | `0.0` | VWAP numerator Σ(tp·v) |
| `self._cum_vol` | `0.0` | VWAP denominator Σv |
| `self._n` | `0` | Welford count |
| `self._mean` | `0.0` | Welford running mean |
| `self._m2` | `0.0` | Welford sum of squared deltas |
| `self._ew_mean` | `0.0` | EWMA mean (if used) |
| `self._ew_var` | `0.0` | EWMA variance (if used) |
| `self._ew_init` | `False` | EWMA init flag |
| `self._entries_this_session` | `0` | re-entry cap counter |
| `self._stop` | `0.0` | stored protective stop |
| `self._target` | `0.0` | stored VWAP target snapshot |

`self.position` / `self.entry_price` are **NOT** reset on date change (a position can be carried by
reconciliation across a restart that happens to straddle midnight; they are owned by
`notify_fill`/`notify_flat` only). VWAP/band are session-local; position is not.

**Also copy from ORB verbatim:**
- The `price <= 0` guard (return None).
- The `MAX_FUTURE_SKEW` future-stamped-tick guard (return None; never let a future tick reset the
  session or pollute VWAP). Same IST wall-clock comparison code.
- `notify_fill` / `notify_flat` bodies (increment `self.position`; set/clear `entry_price`). On
  `notify_flat`, also reset `self._stop` and `self._target` to `0.0`.
- A `status()` dict for the dashboard: `security_id, vwap, band, upper, lower, position,
  entry_price, ready, entries_this_session`.

`max_entries_per_session`: increment `self._entries_this_session` when an ENTER `Decision` is
emitted; block entries once it reaches the cap. (Counting at emit time matches ORB's tried-flag
semantics — conservative if the engine rejects the order, which is acceptable.)

---

## 8. Unit-test cases (input bars → expected Decision)

Tests live in `tests/test_vwap_meanrev.py`. Drive bars at 1-minute spacing starting `09:15` IST on a
fixed weekday (e.g. `2024-02-01`). Each `on_tick` call passes `(now, close, high, low, volume)`.
Use small synthetic series; assert on `action`, `side`, and approximate `stop`/`target`. Patch
`MAX_FUTURE_SKEW`-relevant wall clock if needed (or use a `now` near real time as ORB tests do).
Set `warmup_bars` small (e.g. `3`) in most tests so signals are reachable quickly.

1. **Warm-up blocks entries.** Feed `warmup_bars − 1` flat bars (price=100, vol=1000) then one bar
   far above; expect every `on_tick` → `None` (not enough bars). Assert `_ready is False`.

2. **SHORT entry on stretch above VWAP.** With `warmup_bars=3`, `entry_band_mult=2.0`: feed bars
   `[100, 101, 99]` (building VWAP≈100 and a band), then a bar at `110` (≥ vwap + 2·band). Expect
   `Decision(action="ENTER", side="SELL")` with `target ≈ vwap` and `stop > 110` (above by
   `stop_band_mult·band`). Assert `target < entry signal price` and `stop > entry signal price`.

3. **LONG entry on stretch below VWAP.** Symmetric to test 2: warm-up around 100, then a bar at `90`
   (≤ vwap − 2·band). Expect `Decision(action="ENTER", side="BUY")` with `target ≈ vwap > 90` and
   `stop < 90`.

4. **No signal inside the band.** Warm-up around 100 with a non-trivial band, then a bar at `101`
   (inside ±2·band). Expect `None` (no entry).

5. **VWAP reversion target exit (LONG).** Force a long: after entry (`notify_fill("BUY", q,
   entry_px)`), feed a bar whose close `>= current vwap`. Expect `Decision(action="EXIT",
   reason contains "VWAP target")`.

6. **VWAP reversion target exit (SHORT).** Symmetric: after `notify_fill("SELL", q, entry_px)`, feed
   a bar whose close `<= current vwap`. Expect `EXIT` with reason "VWAP target".

7. **Protective stop exit precedence (SHORT).** After a SHORT entry, set `self._stop` (via the
   emitted ENTER) and feed a bar whose close `>= stored_stop` AND also `<= vwap` (so both stop and
   target would trigger). Expect the **stop** EXIT (`reason` "VWAP-MR stop"), proving stop precedence
   over target in §3.3's ordering.

8. **EOD square-off is unconditional.** Put the strategy in a position, then call `on_tick` at
   `15:20` (with `squareoff_before_close_min=15` → cutoff 15:15). Expect `Decision(action="EXIT",
   reason="EOD square-off")` **even with `_ready is False`** (feed no warm-up bars first — only the
   position via `notify_fill`). Mirrors ORB's EOD test.

9. **No new entry near close.** With `no_entry_before_close_min=30` (cutoff 15:00), warm up earlier,
   then feed a stretch bar at `15:10`. Expect `None` (entry suppressed) while a position would still
   be allowed to exit. (Verify a stretch at `14:00` on the same setup WOULD enter, as a control.)

10. **Session reset wipes VWAP/band.** Feed a full warmed-up session on day 1 (record `vwap`,
    `band` via `status()`), then a bar on day 2 (`now.date()` advanced). Assert `_cum_vol`,
    `_n`, `_mean`, `_m2` reset to 0 and `_ready is False` again on the first day-2 bar; assert
    `self.position` is **unchanged** by the reset (set a position before the day change to confirm it
    survives).

11. **(Optional) Zero/None volume safety.** Feed a bar with `volume=None` then `volume=0`; assert no
    exception and `vwap` falls back to `tp` (no division by zero), entries blocked while
    `_cum_vol == 0`.

12. **(Optional) `min_band_pct` filter.** Warm up with a tiny band (`band < price·min_band_pct`),
    then a "stretch" bar; expect `None` because the band filter rejects the dead session.

---

## 9. Backtest / wiring notes

- **Registry:** add `"vwap_mr": (VwapMeanRev, VwapMeanRevParams)` to the engine's `STRATEGIES`
  registry and `--strategy vwap_mr` to `research/backtest/__main__.py` (default stays `orb`).
- **Volume threading:** the engine must pass `volume=float(bar["volume"])` in its `on_tick` call
  (the column is already loaded in `load_day_bars`). This is the one engine change beyond the
  strategy-agnostic refactor already mandated by the contract. ORB ignores the kwarg.
- **Stored stop/target:** the engine's refactored `_intrabar_exit` already uses
  `decision.stop`/`decision.target` captured at ENTER — VWAP-MR's static stop and the VWAP-at-entry
  target snapshot flow through unchanged. The dynamic VWAP-reversion exit comes from `on_tick` EXIT,
  which the engine already handles (step 3 / EXIT branch). No special engine logic for VWAP-MR.
- **Re-entry:** unlike ORB (one shot per direction), VWAP-MR allows multiple round trips per session
  (`max_entries_per_session`). The engine already supports re-entry after a flat (it sets `pending`
  whenever flat and an ENTER arrives) — no engine change needed.
- **Same harness for comparability:** period `2024-01-01 → 2026-06-19`, `--split-date 2026-01-01`,
  `--n 10`, `--slippage-bps 5`, `--equity 500000`, costs via `research/backtest/costs.py` (do not
  bypass). Report the same panel (Sharpe IS+OOS, win%, payoff, profit factor, max DD, trades, net
  P&L). Survivorship = CEILING (current scrip master) — state it. OOS is the bar.
- **Gate plumbing:** keep `--gate none|kronos` working so VWAP-MR can later be Kronos-gated for an
  A/B (the gate's `direction` is `decision.side`, already provided).
- **Tuning knobs** (priority order): `entry_band_mult` (1.5 / 2.0 / 2.5), `band_method`
  (`resid_std` vs `typical_std`), `stop_band_mult` (0.75 / 1.0 / 1.5), `min_band_pct`
  (0.001 / 0.0015 / 0.0025). `ewma_alpha` left at 0 for the first pass; try `0.05` if the static
  band lags trending sessions.

---

## 10. Sanity invariants (assert in code / review)

- For a SHORT: `target (=vwap) < entry signal price < stop`. For a LONG: `stop < entry signal price
  < target (=vwap)`. (Mean-reversion geometry — the opposite of a breakout.)
- `band >= 0` always; entries impossible while `band == 0`.
- VWAP is bounded by the session's price range (Σpv/Σv of typical prices).
- No state from a prior session leaks (covered by test 10).
- ORB tests remain byte-for-byte green (the `volume` kwarg is optional and unused by ORB).
