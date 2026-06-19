# CPR / Pivot Points — strategy spec (Central Pivot Range breakout + classic pivots)

Implements `strategies/cpr_pivot.py`, a pure, synchronous, IO-free class mirroring
`strategies/orb.py` and obeying every rule in `_CONTRACT.md`. One position at a time, EOD
square-off unconditional, no lookahead, no cross-session leakage.

CPR (Central Pivot Range) and classic floor-trader pivots are derived **once per session
from the PRIOR trading day's High / Low / Close** — they are *static all day*, computed
before the bell, never recomputed from intraday bars. The strategy waits for the price to
break the Central Pivot Range and trades the breakout toward the next pivot level, stopping
at the level just crossed.

**Architecture note (read first):** the per-session replay (`engine.replay_security_day`)
loads only the *current* day's 1-min bars and has no prior-day context. Pivots therefore
**cannot be computed from the bar stream**. This spec defines a clean injection interface —
`seed_prior_day(prior_high, prior_low, prior_close)` — that the engine owner will call once,
before the session's first `on_tick`, with the prior daily bar's H/L/C. If it is never
called (or the prior bar is missing), the strategy **degrades gracefully: it takes no
trade** (every `on_tick` returns `None` except the unconditional EOD square-off). See §3.

---

## 1. Level math — computed ONCE from the prior day (not from intraday bars)

Given prior-day `PH` (high), `PL` (low), `PC` (close):

### 1.1 Central Pivot Range (CPR)
```
Pivot (P) = (PH + PL + PC) / 3
BC        = (PH + PL) / 2                 # Bottom Central
TC        = (P - BC) + P    = 2*P - BC    # Top Central (reflection of BC about P)
```
Note `TC` and `BC` are *not* ordered by name — when `PC` is high, `P > BC` and `TC > P > BC`;
when `PC` is low it can invert so `TC < BC`. The strategy uses
```
cpr_top    = max(TC, BC)
cpr_bottom = min(TC, BC)
cpr_width  = cpr_top - cpr_bottom
```
so the "range" is always `[cpr_bottom, cpr_top]` regardless of TC/BC ordering. A **narrow
CPR** (small `cpr_width` relative to price) is the classic trend-day signal; a **wide CPR**
is the sideways/range-day signal. We expose `cpr_width_pct = cpr_width / P` for an optional
filter (§3.4).

### 1.2 Classic floor-trader pivots (resistances / supports)
```
R1 = 2*P - PL
S1 = 2*P - PH
R2 = P + (PH - PL)
S2 = P - (PH - PL)
R3 = PH + 2*(P - PL)
S3 = PL - 2*(PH - PL)
```
These give the ordered ladder (for a normal day, ascending):
`S3 < S2 < S1 < cpr_bottom ≤ P ≤ cpr_top < R1 < R2 < R3` (S1/R1 can sit inside the CPR on
unusual days; the strategy never assumes strict ordering — it picks targets/stops by the
explicit rules in §2, not by ladder position).

All eight numbers (`P, cpr_top, cpr_bottom, R1, R2, R3, S1, S2, S3`) are stored at seed time
and are **immutable for the whole session**.

---

## 2. Trading rules — CPR breakout toward the next pivot

The ruleset is **ONE concrete, testable strategy**: a Central-Pivot-Range breakout, long
above the CPR top toward R1, short below the CPR bottom toward S1, with the stop at the level
just broken (the CPR edge) padded by `sl_buffer_pct`. One attempt per side per session
(`_long_tried` / `_short_tried` flags, exactly like ORB's breakout flags).

### 2.1 Entry (only when flat, pivots seeded, after warm-up)
Let `price = bar close`. Trigger on the **close** crossing a CPR edge (the engine fills at
the next bar's open; the intrabar wick check then manages stop/target — same model as ORB).

- **Long breakout** — first time, while flat, `price > cpr_top`:
  ```
  entry side = BUY
  stop   = cpr_bottom * (1 - sl_buffer_pct)      # below the broken range
  target = R1                                     # next resistance pivot above
  reason = f"CPR long breakout > TC={cpr_top:.2f} → R1={R1:.2f}"
  set _long_tried = True
  ```
  Guard: if `target <= price` (price already past R1 at trigger — rare), use the *next*
  pivot above price among `(R1, R2, R3)`; if none is above price, fall back to
  `target = price + cpr_width` (a non-degenerate target so the trade is well-formed).

- **Short breakdown** — first time, while flat, `price < cpr_bottom`:
  ```
  entry side = SELL
  stop   = cpr_top * (1 + sl_buffer_pct)          # above the broken range
  target = S1                                      # next support pivot below
  reason = f"CPR short breakdown < BC={cpr_bottom:.2f} → S1={S1:.2f}"
  set _short_tried = True
  ```
  Guard: if `target >= price` (price already below S1), use the *next* pivot below price
  among `(S1, S2, S3)`; if none, `target = price - cpr_width`.

Only one entry per side per session. If the long already triggered (`_long_tried`), a later
re-cross of `cpr_top` does **not** re-enter (mirrors ORB; avoids chasing late breakouts).

### 2.2 Exit (target / stop / EOD)
The engine stores `decision.stop` and `decision.target` at ENTER and runs the intrabar wick
check (`_intrabar_exit`) — so target/stop fills are detected on the bar's high/low. The
strategy ALSO emits close-based EXITs in `on_tick` (defensive, and required so the live tick
engine exits promptly), using the **same level values it sent at entry** (recomputed from the
immutable pivots + stored entry price):

- **Long open (`position > 0`)**:
  ```
  target_lvl = self._entry_target          # R1 (or the chosen pivot) captured at entry
  stop_lvl   = cpr_bottom * (1 - sl_buffer_pct)
  if price >= target_lvl: EXIT  reason=f"Target hit ₹{target_lvl:.2f}"
  if price <= stop_lvl:   EXIT  reason=f"Stop-loss ₹{stop_lvl:.2f}"
  ```
- **Short open (`position < 0`)**:
  ```
  target_lvl = self._entry_target          # S1 (or the chosen pivot) captured at entry
  stop_lvl   = cpr_top * (1 + sl_buffer_pct)
  if price <= target_lvl: EXIT  reason=f"Target hit ₹{target_lvl:.2f}"
  if price >= stop_lvl:   EXIT  reason=f"Stop-loss ₹{stop_lvl:.2f}"
  ```

`self._entry_target` is set in `notify_fill` is **wrong** — `notify_fill` only knows
side/qty/price. Instead capture the target at the moment the ENTER Decision is built: store
`self._pending_target` when you emit ENTER, and promote it to `self._entry_target` inside
`notify_fill` when `self.position` becomes non-zero. On `notify_flat` reset `self._entry_target
= 0.0`. (The stop is recomputed from the immutable CPR edge each bar, so it needs no storing;
only the target needs the entry-time pick because of the §2.1 fallback logic.)

> **Important — stops/targets must agree with the engine's stored levels.** The engine's
> `_intrabar_exit` mirrors `strategies/orb.py`'s level formulas via `orb.or_low/or_high/...`.
> Because this strategy is NOT ORB, the engine refactor (per `_CONTRACT.md` §"Engine
> refactor") must use the **stored `decision.stop` / `decision.target`** captured at ENTER,
> not ORB internals. This spec assumes that refactor: `stop` and `target` on the ENTER
> Decision are the single source of truth for the wick check. The close-based EXITs above use
> identical values so backtest and live agree.

### 2.3 EOD square-off (UNCONDITIONAL)
At `t >= 15:30 − squareoff_before_close_min`: if `self.position != 0` return
`Decision(action="EXIT", reason="EOD square-off")`, else `None`. This sits ABOVE the
seeded/warm-up gate so it fires even when pivots were never seeded (e.g. a DB-reconciled
position after a mid-session restart). Matches ORB section 3.

---

## 2.4 Order of checks inside `on_tick` (copy ORB's skeleton)

1. **Reject** `price <= 0` → `None`.
2. **Future-skew guard** — copy ORB lines 88–96 verbatim: ignore ticks > `MAX_FUTURE_SKEW`
   (2 min) ahead of wall clock; do NOT reset the session, do NOT touch state → `None`.
3. **Session reset** if `now.date() != self._session_date` → `_reset_session(today)`.
   (Note: a reset CLEARS the seeded pivots — see §3.3 — so a new day with no fresh
   `seed_prior_day` call trades nothing, as required.)
4. **EOD square-off (UNCONDITIONAL)** — the §2.3 check, BEFORE the seeded gate.
5. **Seeded gate** — if `not self._seeded` return `None` (no prior-day data → no trade).
6. **Optional warm-up / CPR-width filter** (§3.4) — if disabled, no-op.
7. **Exits** for an open position (§2.2).
8. **Entries** when flat (§2.1).
9. Else `None`.

There is no incremental indicator and no bar-count warm-up: pivots are fully known at seed
time, so the strategy is "ready" on the very first bar after seeding. (The only optional
gate is the CPR-width filter in §3.4.)

---

## 3. The `seed_prior_day` injection interface (engine wiring contract)

### 3.1 Signature
```python
def seed_prior_day(self, prior_high: float, prior_low: float, prior_close: float) -> None:
    """
    Inject the PRIOR trading day's H/L/C so today's CPR + classic pivots can be
    computed. Called ONCE by the engine BEFORE the session's first on_tick.

    Robustness:
      • If any input is <= 0, or prior_high < prior_low, treat as missing data:
        leave self._seeded = False (strategy will trade nothing today). Do NOT raise.
      • Idempotent within a session: a second call with the same/new values just
        recomputes the levels.
    """
```

### 3.2 What the engine owner must supply
- The engine, before replaying day `D` for `security_id`, fetches the **most recent daily
  bar strictly before `D`** for that security (timeframe `'1d'` / daily) — its `high`,
  `low`, `close`. Prefer the actual prior *trading* day (skip weekends/holidays), which is
  naturally satisfied by "latest daily bar with `time < D 00:00 IST`".
- It calls `strat.seed_prior_day(prior_high, prior_low, prior_close)` once, then runs the
  normal `on_tick` loop.
- If no prior daily bar exists (first day in DB, new listing, data gap), the engine simply
  **does not call** `seed_prior_day` (or calls it and the validation fails) → the strategy
  yields no trades that day, which the engine treats like any zero-trade session.
- This wiring lives in the engine refactor, NOT in the strategy. The strategy only exposes
  the method and degrades gracefully.

> Suggested engine helper (engine owner, not this spec's deliverable):
> ```python
> def load_prior_daily(security_id, day) -> Optional[tuple[float,float,float]]:
>     # SELECT high, low, close FROM bars
>     # WHERE security_id=:sid AND timeframe='1d' AND time < :day_start_ist
>     # ORDER BY time DESC LIMIT 1
> ```
> and in `replay_security_day`: if the chosen strategy exposes `seed_prior_day`, call it with
> the result before the bar loop. Strategies without the method (e.g. ORB) are unaffected.

### 3.3 Interaction with session reset
`_reset_session(today)` sets `self._seeded = False` and zeroes all level fields. Because the
backtester constructs a fresh strategy per security-day and calls `seed_prior_day` once up
front, a reset mid-loop (date change) is not expected within a single `replay_security_day`,
but the reset MUST still clear the pivots so that — in the live engine, which runs across
day boundaries — a new session never reuses yesterday's pivots without a fresh seed.

### 3.4 Optional CPR-width filter (off by default)
`cpr_width_pct = cpr_width / P`. Two optional knobs (both default disabled so the base spec
is pure CPR breakout):
- `max_cpr_width_pct` (default `0.0` = disabled): if `> 0`, skip ALL entries when
  `cpr_width_pct > max_cpr_width_pct` (only take breakouts on *narrow-CPR trend-day*
  setups).
- `min_breakout_pct` (default `0.0` = disabled): require the close to clear the CPR edge by
  at least `min_breakout_pct` of price before entering (filters marginal pokes). When `0`,
  any close beyond the edge triggers.

---

## 4. `CprPivotParams` dataclass

```python
@dataclass
class CprPivotParams:
    sl_buffer_pct: float = 0.002          # stop padding beyond the broken CPR edge
    squareoff_before_close_min: int = 15  # EOD flatten lead (matches ORB)
    max_cpr_width_pct: float = 0.0        # 0 = disabled; >0 = only trade narrow-CPR days
    min_breakout_pct: float = 0.0         # 0 = disabled; require close to clear edge by this
    target_level: str = "R1S1"           # which pivot to target: "R1S1" (default) | "R2S2"
                                          #   "R1S1" → long target R1 / short target S1
                                          #   "R2S2" → long target R2 / short target S2
```
Notes:
- `sl_buffer_pct=0.002` matches ORB's default for consistency in the comparison.
- `target_level` lets the study A/B the first vs second pivot as the take-profit without code
  changes. The §2.1 fallback (next pivot above/below price; then `± cpr_width`) applies
  whichever base level is chosen. Default `"R1S1"` = the conservative, most-popular CPR
  target.
- No ATR / no lookback period: pivots are static, so there is **no bar-count warm-up**. The
  only "warm-up" is "is the strategy seeded" (§3).

---

## 5. Warm-up / missing-data handling

- **Not seeded** (`seed_prior_day` never called or validation failed): every `on_tick`
  returns `None` EXCEPT the unconditional EOD square-off (which still flattens a
  pre-existing position). This is the graceful-degrade path required by the prompt.
- **Seeded**: the strategy is immediately ready on the first bar — no bar count needed.
- **Degenerate prior day** (`PH == PL`, i.e. `cpr_width == 0` and all classic levels
  collapse): treat as seeded but `cpr_width == 0`. With the default filters off, the first
  close above/below `P` triggers a breakout whose target may need the §2.1 `± cpr_width`
  fallback (which is 0 → degenerate). Guard: if the computed/fallback target equals the
  entry price (zero-width target), **do not enter** (`return None`) — a zero-distance target
  is not a tradeable setup. (Equivalently: require `abs(target - price) > 0`.)
- EOD square-off is never gated by seeding/warm-up (sits above the seeded gate, §2.4).

---

## 6. Session-reset list (`_reset_session(today)`)

Reset ALL of these on a date change (no cross-session leakage):
- `self._session_date = today`
- `self._seeded = False`
- `self._P = self._cpr_top = self._cpr_bottom = self._cpr_width = 0.0`
- `self._R1 = self._R2 = self._R3 = 0.0`
- `self._S1 = self._S2 = self._S3 = 0.0`
- `self._long_tried = False`
- `self._short_tried = False`
- `self._pending_target = 0.0`
- `self._entry_target = 0.0`
- (position state `self.position` / `self.entry_price` is owned by `notify_fill` /
  `notify_flat` and is NOT reset here — same as ORB.)

`notify_fill` / `notify_flat`: copy ORB verbatim for `self.position` / `self.entry_price`,
PLUS:
- in `notify_fill`, when the position becomes non-zero, set
  `self._entry_target = self._pending_target`.
- in `notify_flat`, set `self._entry_target = 0.0`.

---

## 7. Unit-test cases (input bars → expected Decision)

All tests use `security_id="T"`, default `CprPivotParams()` unless noted. Bars are
`(time, close, high, low)` on `2024-06-03` (a Monday), starting 09:15 IST, 1-minute apart,
fed via `on_tick(now, close, high=high, low=low)`. "Decision" = the return value. Where a
test needs an open position, call `notify_fill(side, qty, fill_px)` first (which promotes
`_pending_target` → `_entry_target`).

A convenient seed for most tests: `seed_prior_day(110, 90, 105)` gives
`P = (110+90+105)/3 = 101.6667`, `BC = (110+90)/2 = 100`, `TC = 2*101.6667 - 100 =
103.3333` → `cpr_top = 103.3333`, `cpr_bottom = 100`, `cpr_width = 3.3333`.
Classic: `R1 = 2*101.6667 - 90 = 113.3333`, `S1 = 2*101.6667 - 110 = 93.3333`,
`R2 = 101.6667 + 20 = 121.6667`, `S2 = 101.6667 - 20 = 81.6667`. Coders should assert with
a tolerance (e.g. `pytest.approx`, abs=0.01).

1. **Not seeded → no trade.** Do NOT call `seed_prior_day`. Feed a bar at 10:00 with a big
   move (`close=200, high=201, low=199`). *Assert:* returns `None`. Feed several more — all
   `None`.

2. **Seeded, price inside CPR → None.** `seed_prior_day(110,90,105)`. Feed 10:00 bar
   `(102, 102.5, 101.5)` (close between `cpr_bottom=100` and `cpr_top=103.33`).
   *Assert:* `None` (no breakout).

3. **Long breakout above CPR top → ENTER BUY, stop below CPR bottom, target R1.**
   `seed_prior_day(110,90,105)`. Feed 10:00 bar `(104, 104.2, 102.5)` — close `104 > 103.33`.
   *Assert:* `Decision(action="ENTER", side="BUY")`, `target ≈ 113.3333` (R1),
   `stop ≈ 100 * (1 - 0.002) = 99.8`, reason contains "CPR long breakout".

4. **Short breakdown below CPR bottom → ENTER SELL, stop above CPR top, target S1.**
   `seed_prior_day(110,90,105)`. Feed 10:00 bar `(99, 100.5, 98.8)` — close `99 < 100`.
   *Assert:* `Decision(action="ENTER", side="SELL")`, `target ≈ 93.3333` (S1),
   `stop ≈ 103.3333 * (1 + 0.002) = 103.5407`, reason contains "CPR short breakdown".

5. **One attempt per side — no re-entry after long already tried.** From #3, do NOT fill
   (stay flat), then feed another bar that re-crosses up `(105, 105.1, 104.9)`.
   *Assert:* returns `None` (`_long_tried` already set). (Mirrors ORB's tried-flag.)

6. **Long target hit → EXIT.** From #3, `notify_fill("BUY", 10, 104.0)` (promotes target
   `≈113.3333`). Feed a bar whose close reaches R1: `(114, 114.2, 113.0)`.
   *Assert:* `Decision(action="EXIT")`, reason contains "Target hit" and `113.33`.

7. **Long stop hit → EXIT.** From #3 + `notify_fill("BUY", 10, 104.0)`. Feed a bar whose
   close drops to/below the stop `99.8`: `(99.5, 102.0, 99.4)`.
   *Assert:* `Decision(action="EXIT")`, reason contains "Stop-loss" and `99.80`.

8. **Short target hit → EXIT.** From #4, `notify_fill("SELL", 10, 99.0)` (target `≈93.3333`).
   Feed `(93, 95.0, 92.8)` (close `93 <= 93.3333`).
   *Assert:* `Decision(action="EXIT")`, reason contains "Target hit" and `93.33`.

9. **EOD square-off is unconditional & seed-independent.** Two sub-cases:
   (a) Seeded + long held (`#3` + fill): call `on_tick` at `15:16` IST → `Decision(EXIT,
   reason="EOD square-off")`.
   (b) NOT seeded but `self.position` forced to `1` directly (simulate DB-reconciled
   position): call `on_tick` at `15:16` IST → still `Decision(EXIT, reason="EOD
   square-off")`. *Assert:* both EXIT regardless of seeding.

10. **Flat at EOD time → None.** Seeded, `position == 0`, `on_tick` at `15:16` IST.
    *Assert:* returns `None`.

11. **Bad prior-day data → not seeded → no trade.** `seed_prior_day(0, 0, 0)` (or
    `seed_prior_day(90, 110, 100)` with high < low). *Assert:* `self._seeded is False`; a
    subsequent breakout-magnitude bar returns `None`.

12. **Session reset clears pivots (no cross-day leakage).** Seed + take a long on day 1
    (`#3`). Then feed the first bar of day 2 (`2024-06-04`, date change) with a breakout-size
    move. *Assert:* `_session_date` updated to day 2, `_seeded is False`, `_long_tried is
    False`, and the day-2 bar returns `None` (no pivots until a fresh `seed_prior_day`).

(Optional 13) **Future-skew guard.** A tick stamped > 2 min ahead of wall clock returns
`None` and does NOT reset the session or alter `_seeded` — copy ORB's test pattern.

(Optional 14) **`target_level="R2S2"` retargets.** `CprPivotParams(target_level="R2S2")`,
seed `(110,90,105)`, long breakout bar `(104,104.2,102.5)`.
*Assert:* ENTER BUY with `target ≈ 121.6667` (R2) instead of R1.

---

## 8. Backtest / parameter notes

- Register in the engine: `STRATEGIES["cpr_pivot"] = (CprPivot, CprPivotParams)`; selectable
  via `--strategy cpr_pivot`. Requires the **`seed_prior_day` wiring** in the engine refactor
  (§3.2) — the engine must fetch the prior daily bar and call `seed_prior_day` before the bar
  loop. Strategies lacking the method (ORB et al.) are unaffected (call it only via
  `hasattr`/`getattr`).
- **Prior-day daily bars must exist in `dhan_clean`.** Per `db-bare-metal-migration-done`
  memory, daily (`'1d'`) coverage is ~92% of clean names; days with no prior daily bar
  produce zero trades (graceful degrade) — state this in the report as a coverage caveat, not
  a bug.
- Engine stores `decision.stop` / `decision.target` at ENTER (per `_CONTRACT.md` engine
  refactor) and the intrabar wick check exits at those stored levels; the close-based EXITs in
  §2.2 use identical values so live and backtest agree.
- Primary sensitivity knobs for the study: `target_level` (`R1S1` vs `R2S2`),
  `max_cpr_width_pct` (e.g. off vs `0.005` narrow-CPR-only), `sl_buffer_pct`. Keep defaults
  pure (all filters off) for the baseline run.
- Same harness as ORB/M3 for comparability: period `2024-01-01 → 2026-06-19`, `--split-date
  2026-01-01`, `--n 10`, `--slippage-bps 5`, `--equity 500000`, full `costs.py` stack. Report
  Sharpe (IS + OOS), win%, payoff, profit factor, max DD, trades, net P&L.
- Survivorship = CEILING (current scrip master) — same honest-bars caveat as the ORB/M3
  study. OOS (split-date 2026-01-01) is the bar for promotion.
- Known failure mode: false breakouts on wide-CPR range days (whipsaw between CPR edge and
  stop). The one-attempt-per-side flag and the optional `max_cpr_width_pct` / `min_breakout_pct`
  filters are the mitigations to A/B.
```
