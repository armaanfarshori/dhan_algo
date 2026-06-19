# RSI(2) Mean-Reversion (Connors-style) — strategy spec

Intraday mean-reversion. Buy short-term oversold dips **only while the longer trend is up**, and
(optionally) sell short-term overbought spikes only while the trend is down. Exit when the
short-term oscillator snaps back, or a protective stop trips, or EOD square-off fires.

This is the classic Larry Connors "RSI(2)" idea, adapted to 1-minute intraday bars: the 2-period
RSI is a very fast, very noisy oscillator that pins to extremes; we fade those extremes in the
direction of a slow trend filter. The pure class lives in `strategies/rsi2_meanrev.py` and mirrors
`strategies/orb.py` exactly (sync, IO-free, ticks-in / Decisions-out). It reuses `Decision` from
`strategies.orb`.

---

## 1. Parameters — `Rsi2MeanRevParams`

```python
@dataclass
class Rsi2MeanRevParams:
    rsi_period: int = 2                 # Connors RSI(2)
    rsi_oversold: float = 10.0          # long entry: RSI <= this AND trend up
    rsi_overbought: float = 90.0        # short entry: RSI >= this AND trend down
    rsi_exit_long: float = 65.0         # exit long when RSI rises back above this
    rsi_exit_short: float = 35.0        # exit short when RSI falls back below this
    trend_period: int = 50             # SMA length (bars) for the trend filter
    stop_pct: float = 0.006            # protective stop: 0.6% beyond entry
    target_pct: float = 0.0            # 0 ⇒ no hard target; exit is RSI-signal-driven
    enable_short: bool = True          # symmetric short side on/off
    squareoff_before_close_min: int = 15
    min_price: float = 1.0             # ignore degenerate sub-₹1 ticks
```

**Parameter notes for the backtest.**
- Defaults give a **symmetric** strategy (long oversold-in-uptrend, short overbought-in-downtrend).
  Set `enable_short=False` for a long-only sweep — recommended as the first A/B knob, since
  intraday equity short mean-reversion is the riskier side.
- `target_pct=0.0` means the trade is **signal-exit-only** (RSI snap-back) with a protective stop;
  this is true to Connors (no profit target — you wait for the bounce). A non-zero `target_pct`
  adds a hard take-profit for sweeps that want one.
- `trend_period=50` on 1-min bars ≈ the last ~50 minutes of price — a fast intraday regime filter,
  NOT a multi-day SMA (we never cross sessions). At market open it warms up over the first 50 bars.
- Keep `rsi_period=2`. Connors-style behavior depends on the 2-period RSI's tendency to slam to
  0/100; longer periods kill the edge.

---

## 2. Indicators — incremental computation (no lookahead, no cross-session leakage)

Both indicators are reset every session (section 4) and updated once per **closed bar** inside
`on_tick`, using the bar **close** only. (high/low are used solely for the protective-stop level,
not for indicators.) Warm-up returns `None` until both indicators are ready.

### 2.1 RSI(2) — Wilder smoothing (chosen)

We use **Wilder's smoothing** (the standard RSI definition, and what Connors' published RSI(2)
uses), not a simple moving average of gains/losses. Recurrence, with `n = rsi_period = 2`:

1. Maintain previous close `prev_close`, running `avg_gain`, `avg_loss`, and a `count` of deltas
   seen this session.
2. On each new closed bar with close `c` (skip the very first bar of the session — no prior close):
   - `delta = c - prev_close`
   - `gain = max(delta, 0.0)`, `loss = max(-delta, 0.0)`
   - **Seed** (first `n` deltas, i.e. `count <= n`): accumulate `gain`/`loss`; once `count == n`,
     set `avg_gain = sum_gain / n`, `avg_loss = sum_loss / n`.
   - **Steady state** (`count > n`), Wilder recurrence:
     - `avg_gain = (avg_gain * (n - 1) + gain) / n`
     - `avg_loss = (avg_loss * (n - 1) + loss) / n`
3. RSI value (only once `count >= n`):
   - if `avg_loss == 0`: `rsi = 100.0`
   - elif `avg_gain == 0`: `rsi = 0.0`
   - else: `rs = avg_gain / avg_loss; rsi = 100.0 - 100.0 / (1.0 + rs)`
4. Always set `prev_close = c` at the end.

RSI is **ready** once `count >= n` (i.e. after `n+1 = 3` closes have been seen: 1 to seed
`prev_close`, then `n` deltas). Until then `rsi` is `None`.

Edge cases: a flat tape (all deltas 0) ⇒ `avg_gain == avg_loss == 0`; treat as `rsi = 100.0` is
ambiguous, so explicitly: if `avg_gain == 0 and avg_loss == 0` ⇒ `rsi = 50.0` (neutral, no signal).

### 2.2 Trend filter — simple SMA(`trend_period`) of close (chosen)

A plain rolling SMA of the last `trend_period` closes, maintained incrementally with a deque +
running sum:

1. Maintain `closes` (a `collections.deque(maxlen=trend_period)`) and `close_sum`.
2. On each closed bar: if the deque is full, `close_sum -= closes[0]`; append `c`;
   `close_sum += c`.
3. SMA is **ready** once `len(closes) == trend_period`; value `= close_sum / trend_period`.
4. **Trend up** ⇔ `c > sma`. **Trend down** ⇔ `c < sma`.

**Why SMA over VWAP:** VWAP needs reliable per-bar volume and is heavily anchored to the open
print, which on thin NSE names is noisy and would couple the regime call to volume-data quality.
A close SMA is volume-independent, deterministic, and trivially leakage-free per session. (A VWAP
variant can be added later as a parameter sweep if SMA underperforms.)

---

## 3. Entry / exit rules → `Decision`

Order of checks inside `on_tick` (mirror ORB's structure precisely):

0. **Guards.** `price <= 0` or `price < p.min_price` → `None`. Future-skew guard (copy ORB's
   `MAX_FUTURE_SKEW` block verbatim — ignore ticks > 2 min ahead of wall clock, do NOT let them
   reset the session). Then `today = now.date()`, `t = now.time()`.

1. **Session reset** if `now.date() != self._session_date` → `_reset_session(today)` (section 4).

2. **Update indicators** with `bar_close = price` (section 2). Compute `rsi` (or `None`),
   `sma` (or `None`), and whether each is ready.

3. **EOD square-off — unconditional, ABOVE every readiness gate** (ORB section 3). Compute
   `squareoff = 15:30 − squareoff_before_close_min`. If `t >= squareoff`:
   - if `self.position != 0` → `Decision(action="EXIT", reason="EOD square-off")`
   - else → `None`
   (Must not depend on RSI/SMA being ready — a reconciled position must always flatten.)

4. **Warm-up gate.** If `rsi is None` or `sma is None` → `None` (not enough bars yet).

5. **Manage an OPEN position (exits).** The protective stop levels below are the SAME absolute
   levels passed to the engine at ENTER (engine does intrabar wick detection from `decision.stop`/
   `decision.target`), but we ALSO emit them here so the live path and close-based path agree.
   - If `self.position > 0` (LONG):
     - `stop = self.entry_price * (1 - p.stop_pct)`
     - if `p.target_pct > 0`: `target = self.entry_price * (1 + p.target_pct)` and
       `price >= target` → `Decision(action="EXIT", reason=f"Target hit ₹{target:.2f}")`
     - if `price <= stop` → `Decision(action="EXIT", reason=f"Stop-loss ₹{stop:.2f}")`
     - if `rsi >= p.rsi_exit_long` → `Decision(action="EXIT", reason=f"RSI exit {rsi:.1f}")`
     - else → `None`
   - If `self.position < 0` (SHORT):
     - `stop = self.entry_price * (1 + p.stop_pct)`
     - if `p.target_pct > 0`: `target = self.entry_price * (1 - p.target_pct)` and
       `price <= target` → `Decision(action="EXIT", reason=f"Target hit ₹{target:.2f}")`
     - if `price >= stop` → `Decision(action="EXIT", reason=f"Stop-loss ₹{stop:.2f}")`
     - if `rsi <= p.rsi_exit_short` → `Decision(action="EXIT", reason=f"RSI exit {rsi:.1f}")`
     - else → `None`
   (When a position is open, never evaluate entries — one position at a time. Return after the
   exit check.)

6. **Entries** (only when `self.position == 0`). At most ONE entry attempt per session per side is
   NOT enforced (mean-reversion may re-enter after a clean exit) — instead we gate purely on the
   live conditions. Long takes priority if both somehow qualify (they cannot, since trend can't be
   both up and down).
   - **LONG**: `rsi <= p.rsi_oversold AND price > sma` (oversold dip in an uptrend):
     - `stop = price * (1 - p.stop_pct)`
     - `target = price * (1 + p.target_pct)` if `p.target_pct > 0` else a far level
       `price * (1 + 10 * p.stop_pct)` (wide placeholder so the engine has a numeric target; real
       exit is the RSI snap-back via on_tick — see contract rule 4).
     - `Decision(action="ENTER", side="BUY", stop=stop, target=target,
        reason=f"RSI2 long: rsi={rsi:.1f} < {p.rsi_oversold} & px>{sma:.2f}")`
   - **SHORT** (only if `p.enable_short`): `rsi >= p.rsi_overbought AND price < sma`:
     - `stop = price * (1 + p.stop_pct)`
     - `target = price * (1 - p.target_pct)` if `p.target_pct > 0` else
       `price * (1 - 10 * p.stop_pct)` (far placeholder).
     - `Decision(action="ENTER", side="SELL", stop=stop, target=target,
        reason=f"RSI2 short: rsi={rsi:.1f} > {p.rsi_overbought} & px<{sma:.2f}")`
   - else → `None`

**Position state** is tracked only via `notify_fill` / `notify_flat` (copy ORB's implementations
verbatim — they set `self.position` and `self.entry_price`). Never assume an emitted ENTER was
executed.

**Engine note on the placeholder target:** because `target_pct=0` by default, ENTER carries a wide
far target so the engine's intrabar wick logic effectively never triggers a target — the trade
exits on the RSI snap-back (close-based, via on_tick) or the protective stop. This satisfies
contract rule 4 ("signal-exit only … still provide a protective stop; set target to a wide far
level"). The engine's stored-stop/target refactor handles this with no special-casing.

---

## 4. Session reset list (`_reset_session(today)`)

Reset ALL of the following on every date change (and seed `self._session_date = today`):
- `prev_close = None`
- `avg_gain = 0.0`, `avg_loss = 0.0`, `_delta_count = 0`, `_seed_gain = 0.0`, `_seed_loss = 0.0`
- `rsi = None`
- `closes = deque(maxlen=trend_period)`, `close_sum = 0.0`, `sma = None`
- (Position state `self.position` / `self.entry_price` is NOT reset here — it is owned by
  notify_fill/notify_flat and may legitimately carry over a reconciled position into EOD
  square-off; mirror ORB, which also does not reset position in `_reset_session`.)

Warm-up after a reset: no signal until `_delta_count >= rsi_period` AND `len(closes) ==
trend_period`. On 1-min bars with default params that is the first ~50 bars (~09:15–10:05 IST);
SMA(50) is the binding constraint.

---

## 5. Unit-test cases (input bars → expected Decision)

All times IST, same trading day unless noted. Use a tiny `trend_period` and `rsi_period=2` in
tests so warm-up is short and arithmetic is checkable. Suggested test params unless stated:
`Rsi2MeanRevParams(rsi_period=2, rsi_oversold=10, rsi_overbought=90, rsi_exit_long=65,
rsi_exit_short=35, trend_period=3, stop_pct=0.01, target_pct=0.0, enable_short=True)`.
Feed `on_tick(ts, close)` (no high/low needed). Assert the RETURNED Decision (or None).

Note: with `rsi_period=2` and `trend_period=3`, the strategy is ready after **3 closes**
(SMA(3) needs 3; RSI(2) needs 3 closes = 2 deltas). RSI is computed Wilder-style; the first RSI is
the seed-average ratio.

1. **Warm-up returns None.** Bars at 09:15, 09:16 (two closes only). Each `on_tick` → `None`
   (SMA(3) not ready; RSI not ready). Assert both calls return `None`.

2. **Oversold long entry in uptrend.** Construct a rising series so SMA is below price, then a
   sharp 1-bar dip that drives RSI(2) ≤ 10 while close is still > SMA(3). E.g. closes
   `[100, 101, 103]` (warm-up, all None), then `104` (RSI high — no signal), then a dip to `101.5`
   where `101.5 > sma3([103,104,101.5])≈102.83` is **false** → adjust series so the dip stays above
   SMA. Concretely use closes `[100, 102, 104, 106, 104.0]`: at the last bar RSI(2) is low (one
   down move after strong ups) but `104.0 > sma3([106,…])`? Verify in the test by computing SMA;
   the assertion is: **when `rsi <= 10` and `close > sma`, expect**
   `Decision(action="ENTER", side="BUY")` with `stop ≈ close*(1-0.01)` and a far target. (Pick the
   exact series in code so both conditions hold; the load-bearing assertion is the ENTER/BUY +
   stop level.)

3. **Oversold but DOWNTREND → no long.** Same deep dip driving RSI(2) ≤ 10, but arrange closes so
   `close < sma` (a falling series, e.g. `[110, 108, 106, 104, 102]`). Expect `None` (trend filter
   blocks the long; and since `enable_short` requires RSI ≥ 90 for shorts, no short either).

4. **Overbought short entry in downtrend.** Falling series so price < SMA, then a 1-bar pop that
   drives RSI(2) ≥ 90 while close still < SMA(3). Expect
   `Decision(action="ENTER", side="SELL")` with `stop ≈ close*(1+0.01)`. With `enable_short=False`,
   the SAME bars must return `None` (covers the short-toggle).

5. **Long exit on RSI snap-back.** Enter long (call notify_fill("BUY", qty, entry_px) to set
   position), then feed rising closes until `rsi >= rsi_exit_long (65)`. Expect
   `Decision(action="EXIT", reason startswith "RSI exit")`. Before RSI crosses 65, exit calls
   return `None`.

6. **Long exit on protective stop (close-based path).** Enter long at entry_px=100 (notify_fill).
   Feed a close at `99.0` with `stop_pct=0.01` ⇒ stop=99.0; `price <= stop` → expect
   `Decision(action="EXIT", reason startswith "Stop-loss")`. (Engine also catches this intrabar via
   the stored stop; this test covers the on_tick close path.)

7. **Short exit on RSI snap-back.** Enter short (notify_fill("SELL", …)), feed falling closes until
   `rsi <= rsi_exit_short (35)`. Expect `Decision(action="EXIT", reason startswith "RSI exit")`.

8. **EOD square-off is unconditional and beats everything.** Set position open (notify_fill). Call
   `on_tick` at `15:16` (≥ 15:30−15min) with ANY price, even before indicators are ready / right
   after a fresh session reset. Expect `Decision(action="EXIT", reason="EOD square-off")`. Variant:
   with `self.position == 0` at 15:16 → expect `None`.

9. **Session reset clears indicators.** Warm up + take a signal on day 1. Then call `on_tick` with
   a timestamp on day 2 (new date), early bars only. Expect `None` (indicators reset → warm-up
   again), and assert internal `rsi is None` / `sma is None` immediately after the first day-2 bar.

10. **Future-skew tick ignored.** Call `on_tick` with `now` set > 2 min ahead of wall clock during
    the OR/warm-up window. Expect `None` AND assert it did NOT advance `_delta_count` / `closes`
    (state unchanged), exactly like ORB's skew guard.

---

## 6. Implementation checklist (parity with ORB)

- Reuse `from strategies.orb import Decision`. Do NOT redefine it.
- Copy ORB's constants (`IST`, `MARKET_OPEN`, `MARKET_CLOSE`, `MAX_FUTURE_SKEW`) and the
  future-skew guard block byte-for-byte.
- `__init__(self, security_id, params: Optional[Rsi2MeanRevParams] = None)`; set `self.p`,
  `self.security_id`, all indicator state, `self.position = 0`, `self.entry_price = 0.0`,
  `self._session_date = None`.
- `notify_fill` / `notify_flat`: identical to ORB.
- Provide a `status()` dict (security_id, rsi, sma, position, entry_price) for parity/debugging.
- Register in the engine: `STRATEGIES["rsi2_mr"] = (Rsi2MeanRev, Rsi2MeanRevParams)` and add
  `rsi2_mr` to the `--strategy` CLI choices (engine refactor is owned by the refactor coder; this
  strategy only needs to satisfy the same `on_tick`/`notify_*` interface).
- Backtest run is the standard harness (contract §"Backtest harness"): period 2024-01-01→2026-06-19,
  `--split-date 2026-01-01`, `--n 10`, `--slippage-bps 5`, `--equity 500000`, costs via costs.py.
  Run a long-only (`enable_short=False`) and a symmetric variant; OOS Sharpe is the bar.
