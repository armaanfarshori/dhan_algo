# Bollinger Band Mean-Reversion — strategy spec

Intraday mean-reversion around a rolling mean. Compute Bollinger Bands on the bar **close**:
a simple moving average `SMA(period)` (the *middle band*) with an envelope `± k * stdev` of the
last `period` closes (the *upper* / *lower* bands). **Fade the edges back to the mean**: go LONG
when close pierces the lower band, go SHORT when close pierces the upper band, and take profit at
the middle band (the SMA). A protective stop sits a configurable distance **beyond** the band that
triggered the entry (fixed-fraction or ATR-based). EOD square-off is unconditional.

The pure class lives in `strategies/bollinger_meanrev.py` and mirrors `strategies/orb.py` exactly
(synchronous, IO-free, ticks-in / Decisions-out — the same code path used live and in the
backtester). It reuses `Decision` from `strategies.orb`; do NOT redefine it.

The key fidelity point versus ORB: the **target moves every bar** (it tracks the live SMA), so it
cannot be a frozen absolute level at ENTER time. We handle this exactly like the contract describes
for signal-exit strategies (rule 4): ENTER carries a **far placeholder target** so the engine's
intrabar wick logic never fires on the target; the *real* take-profit (close crossing the moving
SMA) is emitted from `on_tick` as an EXIT. The protective **stop**, by contrast, IS a fixed
absolute level set at entry, so it flows through the engine's stored-stop wick detection normally.

---

## 1. Parameters — `BollingerMeanRevParams`

```python
@dataclass
class BollingerMeanRevParams:
    period: int = 20                    # SMA / stdev lookback (bars) — the Bollinger window
    k: float = 2.0                      # band width in stdevs: bands = sma ± k*stdev
    stop_method: str = "band_pct"       # "band_pct" | "atr"  — how the protective stop is placed
    stop_band_pct: float = 0.003        # band_pct: stop is 0.3% beyond the entry band
    stop_atr_mult: float = 1.5          # atr: stop is this many ATR beyond the entry band
    atr_period: int = 14               # ATR lookback (bars), only used when stop_method == "atr"
    enable_short: bool = True          # symmetric short side on/off
    exit_on_opposite_band: bool = True # also exit if price tags the OPPOSITE band (overshoot guard)
    squareoff_before_close_min: int = 15
    min_price: float = 1.0             # ignore degenerate sub-₹1 ticks
    min_band_width_pct: float = 0.0    # skip entries when band half-width < this fraction of price
```

**Parameter notes for the backtest.**
- Defaults are the textbook Bollinger setup: `period=20`, `k=2.0`. These are sensible on 1-min bars
  (≈ last 20 minutes of price) and a natural first A/B vs. ORB; sweep `k ∈ {1.5, 2.0, 2.5}` and
  `period ∈ {20, 30, 50}` later.
- `stop_method="band_pct"` is the default (volume/ATR-independent, deterministic). Set
  `stop_method="atr"` for a volatility-adaptive stop — recommended as the second sweep knob, since a
  fixed-percent stop is too tight on volatile names and too loose on quiet ones.
- `enable_short=False` gives a long-only sweep — run this first; intraday equity short
  mean-reversion is the riskier side (NSE single-stock shorts can squeeze hard).
- `exit_on_opposite_band=True` adds an "if the trade ran straight past the mean to the far band,
  bank it" guard. With it off, the only profit exit is the SMA tag; with it on, a long also exits if
  close ≥ the upper band (rare, but caps a runaway).
- `min_band_width_pct` (default 0 = off) lets you skip dead-flat tapes where the bands collapse and
  every tiny wiggle pierces a band. Try `0.001`–`0.002` if churn is high.
- Keep the stop strictly **outside** the band that triggered entry: a long enters at/below the lower
  band and its stop is *below* the lower band, so noise inside the channel doesn't stop you out — the
  whole thesis is "price reverts from the edge", so the stop must allow a small further excursion.

---

## 2. Indicators — incremental computation (no lookahead, no cross-session leakage)

All indicators are reset every session (section 4) and updated once per **closed bar** inside
`on_tick`, using the bar **close** (ATR additionally uses high/low — see 2.3). Warm-up returns
`None` until the bands are ready (`period` closes seen; plus `atr_period+1` bars if `stop_method=="atr"`).

### 2.1 Rolling SMA (middle band) — deque + running sum (chosen)

A plain rolling mean of the last `period` closes, maintained incrementally:

1. Maintain `closes = collections.deque(maxlen=period)` and `close_sum: float = 0.0`.
2. On each closed bar with close `c`: if the deque is **full** (`len(closes) == period`),
   subtract the value about to be evicted — `close_sum -= closes[0]`; then `closes.append(c)`;
   `close_sum += c`. (Append on a full deque evicts `closes[0]` automatically, so subtract its old
   value FIRST.)
3. SMA is **ready** once `len(closes) == period`; `sma = close_sum / period`.

This is O(1) per bar and numerically fine for a 20–50-element running sum of ₹-scale prices (the
catastrophic-cancellation regime that breaks a naive running sum needs millions of adds of wildly
different magnitudes; we re-derive the sum each session and the window is tiny).

### 2.2 Rolling stdev (band half-width) — sum-of-squares over the SAME deque (chosen)

We compute the **population** standard deviation of the last `period` closes (divisor `N = period`,
not `N-1`; this is the conventional Bollinger definition). We track a running sum of squares
**parallel to** the running sum in 2.1, evicting in lockstep with the same deque:

1. Maintain `sq_sum: float = 0.0` alongside `close_sum`.
2. On each closed bar, in the SAME full/append/add step as 2.1:
   - if the deque is full: `close_sum -= old; sq_sum -= old * old` where `old = closes[0]`
     (the value about to be evicted).
   - after `closes.append(c)`: `close_sum += c; sq_sum += c * c`.
3. Once `len(closes) == period` (`N = period`):
   - `mean = close_sum / N`            (this IS `sma`)
   - `var = sq_sum / N - mean * mean`  (population variance via E[x²] − E[x]²)
   - **`var = max(var, 0.0)`** — clamp tiny negatives from floating-point cancellation (a flat
     window where every close is identical can yield `var = -1e-9`); never `sqrt` a negative.
   - `stdev = math.sqrt(var)`

**Numerical-stability note.** The `E[x²]−E[x]²` form is O(1) and exact enough for a `period`-sized
window of intraday prices (values within a few percent of each other, summed ~20–50 times). Its only
hazard is the small-negative-variance artifact on near-constant windows, which the `max(var, 0.0)`
clamp removes. We deliberately re-initialise `close_sum`/`sq_sum` every session (deque cleared in
`_reset_session`), so error never accumulates across days. (If a future profile shows precision
issues, swap to Welford's online algorithm with a deque of (value) and the standard add/remove
update — but for this window size the sum-of-squares form is correct and simpler.)

Bands, once ready:
- `mid = sma`
- `upper = mid + k * stdev`
- `lower = mid - k * stdev`
- `half_width = k * stdev`  (used for `min_band_width_pct`)

### 2.3 ATR (only if `stop_method == "atr"`) — Wilder smoothing over the bar stream

ATR needs high/low/close. True Range for bar `i` with high `h`, low `l`, and previous close `pc`:
`tr = max(h - l, abs(h - pc), abs(l - pc))`. (On the FIRST bar of the session, `pc` is unknown — use
`tr = h - l`.) Then Wilder-smooth with `n = atr_period`:

1. Maintain `prev_close_atr`, running `atr`, and a `tr_count` of true ranges seen this session.
2. On each closed bar: compute `tr` (using `prev_close_atr`, or `h-l` on the first bar).
   - **Seed** (`tr_count <= n`): accumulate `tr` into `_tr_seed_sum`; once `tr_count == n`,
     `atr = _tr_seed_sum / n`.
   - **Steady state** (`tr_count > n`): `atr = (atr * (n - 1) + tr) / n`.
3. Set `prev_close_atr = c`. ATR is **ready** once `tr_count >= n`.

When `stop_method == "band_pct"`, ATR is not computed and not part of the warm-up gate.

---

## 3. Entry / exit rules → `Decision`

Order of checks inside `on_tick` (mirror ORB's structure precisely):

0. **Guards.** `price <= 0` or `price < p.min_price` → `None`. Future-skew guard (copy ORB's
   `MAX_FUTURE_SKEW` block verbatim — ignore ticks > 2 min ahead of wall clock, and do NOT let such
   a tick reset the session or update indicators). Then `today = now.date()`, `t = now.time()`.

1. **Session reset** if `now.date() != self._session_date` → `_reset_session(today)` (section 4).

2. **Update indicators** with `bar_close = price`, plus `high`/`low` for ATR if `stop_method=="atr"`
   (section 2). After updating, compute `sma`/`upper`/`lower`/`half_width` (or `None`) and whether
   the band set (and ATR, if needed) is ready.

3. **EOD square-off — unconditional, ABOVE every readiness gate** (ORB section 3). Compute
   `squareoff = 15:30 − squareoff_before_close_min`. If `t >= squareoff`:
   - if `self.position != 0` → `Decision(action="EXIT", reason="EOD square-off")`
   - else → `None`
   (Must NOT depend on the bands being ready — a reconciled position must always flatten.)

4. **Warm-up gate.** If the bands are not ready (`sma is None`), or `stop_method=="atr"` and `atr` is
   not ready → `None`.

5. **Manage an OPEN position (exits).** Return after this block when a position is open — never
   evaluate entries while in a trade (one position at a time).
   - If `self.position > 0` (LONG):
     - **Take-profit at the mean:** if `price >= mid` →
       `Decision(action="EXIT", reason=f"Mean reversion target ₹{mid:.2f}")`.
     - **Overshoot guard** (if `p.exit_on_opposite_band`): elif `price >= upper` →
       `Decision(action="EXIT", reason=f"Opposite band ₹{upper:.2f}")`.
     - **Protective stop (close-based path):** elif `price <= self.stop_price` →
       `Decision(action="EXIT", reason=f"Stop-loss ₹{self.stop_price:.2f}")`.
     - else → `None`.
   - If `self.position < 0` (SHORT):
     - **Take-profit at the mean:** if `price <= mid` →
       `Decision(action="EXIT", reason=f"Mean reversion target ₹{mid:.2f}")`.
     - **Overshoot guard** (if `p.exit_on_opposite_band`): elif `price <= lower` →
       `Decision(action="EXIT", reason=f"Opposite band ₹{lower:.2f}")`.
     - **Protective stop:** elif `price >= self.stop_price` →
       `Decision(action="EXIT", reason=f"Stop-loss ₹{self.stop_price:.2f}")`.
     - else → `None`.

   `self.stop_price` is the absolute stop level frozen at the entry bar (set in step 6 and stored on
   the instance). The engine ALSO catches the stop intrabar from `decision.stop`; emitting it here
   keeps the live close-path and backtest agree. The MEAN target is intentionally close-based only
   (it moves every bar), which is why ENTER ships a far placeholder target (see below).

6. **Entries** (only when `self.position == 0`). Mean-reversion may re-enter after a clean exit, so
   we do NOT use one-shot tried-flags; we gate purely on live band conditions. Compute the stop
   distance from `p.stop_method`:
   - `band_pct`: `stop_dist = entry_band * p.stop_band_pct` measured off the triggering band level.
   - `atr`: `stop_dist = p.stop_atr_mult * atr`.

   **Band-width filter** (optional): if `p.min_band_width_pct > 0` and `half_width < price *
   p.min_band_width_pct` → `None` (channel too tight; skip).

   - **LONG** when `price <= lower`:
     - `self.stop_price = lower - stop_dist`  (with `entry_band = lower` for `band_pct`)
     - sanity: if `self.stop_price >= price` (degenerate, stop not below entry) → `None`.
     - `target = price * (1 + 10 * p.stop_band_pct)` if `stop_method=="band_pct"` else
       `price + 10 * stop_dist` — a **far placeholder** (real exit is the SMA tag via on_tick; this
       just satisfies contract rule 4 so the engine has a numeric target that never fires).
     - `Decision(action="ENTER", side="BUY", stop=self.stop_price, target=target,
        reason=f"BB long: px={price:.2f} <= lower={lower:.2f} (mid={mid:.2f})")`
   - **SHORT** (only if `p.enable_short`) when `price >= upper`:
     - `self.stop_price = upper + stop_dist`  (with `entry_band = upper` for `band_pct`)
     - sanity: if `self.stop_price <= price` → `None`.
     - `target = price * (1 - 10 * p.stop_band_pct)` if `stop_method=="band_pct"` else
       `price - 10 * stop_dist` — far placeholder.
     - `Decision(action="ENTER", side="SELL", stop=self.stop_price, target=target,
        reason=f"BB short: px={price:.2f} >= upper={upper:.2f} (mid={mid:.2f})")`
   - else → `None`.

   Long and short are mutually exclusive (a single close cannot be both ≤ lower and ≥ upper unless
   `k=0`, which we never use). If both somehow qualify, take LONG first.

**Position state** is tracked only via `notify_fill` / `notify_flat` (copy ORB's implementations
verbatim — they set `self.position` and `self.entry_price`). Never assume an emitted ENTER was
executed. `notify_flat` should also reset `self.stop_price = 0.0`. Set `self.stop_price` at the
moment you emit the ENTER (it is the absolute level passed as `decision.stop`); the engine stores
`decision.stop` independently, so even if the ENTER is rejected, a stale `self.stop_price` is
harmless because step 5 only runs when `self.position != 0`.

**Engine note on the placeholder target.** Because the take-profit is a *moving* level (the SMA),
it cannot be a frozen absolute target. ENTER therefore carries a wide far target so the engine's
intrabar wick logic effectively never triggers on the target; the trade exits on the SMA tag
(close-based, via on_tick), the protective stop (absolute, caught intrabar by the engine OR
close-based here), the opposite-band overshoot guard, or EOD square-off. This matches contract
rule 4 ("signal-exit only … still provide a protective stop; set target to a wide far level"). The
engine's stored stop/target refactor handles this with no special-casing.

---

## 4. Session reset list (`_reset_session(today)`)

Reset ALL of the following on every date change (and seed `self._session_date = today`):
- `closes = collections.deque(maxlen=period)`, `close_sum = 0.0`, `sq_sum = 0.0`
- `sma = None`, `upper = None`, `lower = None` (or recompute lazily; just ensure not-ready until
  `len(closes) == period`)
- ATR state (only relevant for `stop_method=="atr"`, but reset unconditionally for safety):
  `atr = None`, `prev_close_atr = None`, `tr_count = 0`, `_tr_seed_sum = 0.0`
- (Position state `self.position` / `self.entry_price` / `self.stop_price` is NOT reset here — it is
  owned by notify_fill/notify_flat and may legitimately carry a reconciled position into EOD
  square-off; mirror ORB, which also does not reset position in `_reset_session`.)

Warm-up after a reset: no signal until `len(closes) == period` (and, for ATR stops,
`tr_count >= atr_period`). On 1-min bars with default `period=20` that is the first ~20 bars
(~09:15–09:35 IST); with `stop_method=="atr"` and `atr_period=14` the band warm-up (20) is the
binding constraint.

---

## 5. Unit-test cases (input bars → expected Decision)

All times IST, same trading day unless noted. Use a **tiny `period`** in tests so warm-up is short
and the SMA/stdev are hand-checkable. Suggested test params unless stated:
`BollingerMeanRevParams(period=3, k=2.0, stop_method="band_pct", stop_band_pct=0.01,
enable_short=True, exit_on_opposite_band=True, squareoff_before_close_min=15, min_price=1.0,
min_band_width_pct=0.0)`. Feed `on_tick(ts, close)` (high/low only needed for ATR tests). Assert the
RETURNED Decision (or None).

For `period=3`, population stdev of closes `[a,b,c]` is
`sqrt((a²+b²+c²)/3 − ((a+b+c)/3)²)`; bands are `mean ± 2*stdev`. Compute the exact numbers in the
test to derive the trigger prices.

1. **Warm-up returns None.** Bars at 09:15, 09:16 (two closes only, `period=3`). Each `on_tick` →
   `None` (bands not ready). Assert both calls return `None` and `sma is None` after the 2nd bar.

2. **Long entry when close pierces the lower band.** Feed three closes to arm the bands, e.g.
   `[100, 100, 100]` → `sma=100`, `stdev=0`, so the band has zero width — instead use a series with
   spread so the lower band is below the next close's trigger, e.g. `[101, 100, 99]` →
   `mean=100`, `stdev=sqrt((101²+100²+99²)/3 − 100²)=sqrt(2/3)≈0.8165`, `lower=100−2*0.8165≈98.367`.
   Then feed a 4th close at `98.0` (the deque rolls to `[100,99,98]`; recompute its `lower` in the
   test and choose the 4th close strictly ≤ that rolled `lower`). Expect
   `Decision(action="ENTER", side="BUY")` with `stop ≈ lower*(1−0.01)` (stop strictly below the
   triggering close). The load-bearing assertions: `action=="ENTER"`, `side=="BUY"`,
   `stop < price`.

3. **No long when close is inside the channel.** With bands armed and `lower≈98.37`, feed a 4th
   close at `99.5` (above the lower band, below the upper). Expect `None` (no edge tagged).

4. **Short entry when close pierces the upper band.** Symmetric to test 2: arm bands, then feed a
   close ≥ the (rolled) upper band. Expect `Decision(action="ENTER", side="SELL")` with
   `stop ≈ upper*(1+0.01)` (stop strictly above the triggering close). Variant: with
   `enable_short=False`, the SAME bars return `None` (covers the short toggle).

5. **Long take-profit at the mean.** Enter long (call `notify_fill("BUY", qty, entry_px)` to set
   `self.position`/`self.entry_price`; also set `self.stop_price` as the strategy would). Then feed
   closes that rise back to/above the current `mid` (SMA). Expect
   `Decision(action="EXIT", reason startswith "Mean reversion target")`. A close still below `mid`
   (and above the stop) returns `None`.

6. **Long protective stop (close-based path).** Enter long with `entry_px` and a known
   `self.stop_price` (e.g. arm bands so `lower≈98.37`, enter at `98.0`, `stop = lower*0.99 ≈ 97.38`).
   Feed a close at `97.0` (≤ stop). Expect `Decision(action="EXIT", reason startswith "Stop-loss")`.
   (The engine also catches this intrabar via the stored stop; this covers the on_tick close path.)

7. **Short take-profit at the mean.** Enter short (`notify_fill("SELL", …)`), feed closes that fall
   back to/below `mid`. Expect `Decision(action="EXIT", reason startswith "Mean reversion target")`.

8. **Opposite-band overshoot exit.** Enter long, then feed a close that jumps to ≥ the current
   `upper` band (skipping past the mean) with `exit_on_opposite_band=True`. Expect
   `Decision(action="EXIT", reason startswith "Opposite band")`. (Because the mean-target check
   runs first and `price >= mid` would also be true here, assert this case with the SMA reached too —
   either reason is acceptable; if you want to isolate the opposite-band branch, set a series where
   the close is ≥ upper; mid-check fires first by design — document that the mean target is the
   normal exit and opposite-band is a belt-and-suspenders cap. The robust assertion: action=="EXIT"
   and position will flatten.)

9. **EOD square-off is unconditional and beats everything.** Set position open (`notify_fill`). Call
   `on_tick` at `15:16` (≥ 15:30−15min) with ANY price, even before the bands are ready / right
   after a fresh session reset. Expect `Decision(action="EXIT", reason="EOD square-off")`. Variant:
   with `self.position == 0` at 15:16 → expect `None`.

10. **Session reset clears indicators.** Arm bands + take a signal on day 1. Then call `on_tick`
    with a timestamp on day 2 (new date), early bars only. Expect `None` (bands reset → warm-up
    again); assert `sma is None` and `len(closes) < period` immediately after the first day-2 bar.

11. **Future-skew tick ignored.** Call `on_tick` with `now` > 2 min ahead of the wall clock during
    warm-up. Expect `None` AND assert it did NOT advance `closes` / `close_sum` / `sq_sum` (state
    unchanged), exactly like ORB's skew guard.

12. **ATR-stop entry.** With `stop_method="atr"`, `period=3`, `atr_period=2`, `stop_atr_mult=1.5`,
    feed bars (with high/low) to arm BOTH bands and ATR, then a close ≤ lower band. Expect
    `Decision(action="ENTER", side="BUY")` with `stop ≈ lower − 1.5*atr`. Assert the stop equals the
    ATR-derived level (compute the expected `atr` from the fed true ranges in the test). Before ATR
    is ready (fewer than `atr_period` true ranges), even a lower-band tag returns `None` (warm-up).

---

## 6. Implementation checklist (parity with ORB)

- Reuse `from strategies.orb import Decision`. Do NOT redefine it.
- Copy ORB's constants (`IST`, `MARKET_OPEN`, `MARKET_CLOSE`, `MAX_FUTURE_SKEW`) and the
  future-skew guard block byte-for-byte.
- `__init__(self, security_id, params: Optional[BollingerMeanRevParams] = None)`; set `self.p`,
  `self.security_id`, all indicator state (deque/sums/ATR), `self.position = 0`,
  `self.entry_price = 0.0`, `self.stop_price = 0.0`, `self._session_date = None`.
- `notify_fill` / `notify_flat`: identical to ORB, plus `notify_flat` resets `self.stop_price = 0.0`.
- Use `import math` for `sqrt`; use `collections.deque(maxlen=period)` for the rolling window.
- Provide a `status()` dict (security_id, sma, upper, lower, atr, position, entry_price, stop_price)
  for parity/debugging.
- Register the strategy: `STRATEGIES["bollinger_mr"] = (BollingerMeanRev, BollingerMeanRevParams)`
  in `research/backtest/registry.py`, and add `bollinger_mr` to the `--strategy` CLI choices
  (the engine refactor is owned by the refactor coder; this strategy only needs to satisfy the same
  `on_tick`/`notify_*` interface — which the current engine already invokes via the registry).
- Backtest run is the standard harness (contract §"Backtest harness"): period
  2024-01-01→2026-06-19, `--split-date 2026-01-01`, `--n 10`, `--slippage-bps 5`, `--equity 500000`,
  costs via costs.py. Run a long-only (`enable_short=False`) and a symmetric variant, and a
  `stop_method="atr"` variant; **OOS Sharpe is the bar**.
