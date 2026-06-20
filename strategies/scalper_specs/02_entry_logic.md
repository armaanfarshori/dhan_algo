# 02 — Entry Signal Logic (hardened NIFTY options scalper)

**Scope:** ENTRY only. Direction signal on the **underlying** (NIFTY spot/futures intraday) →
expressed as a **long ATM CE/PE** scalp. Take-profit / stop / trail / time-stop / EOD square-off
are exits and live in a separate spec. This document defines the concrete, testable entry triggers,
their parameters, the confidence/strength filters that keep the scalper from over-trading, and the
exact mapping into the `on_tick → ScalpDecision` interface.

**Source of truth for the code:** `strategies/options_scalper.py`
(`direction_signal`, `OptionsScalper._evaluate_entry`, `_evaluate_new_ladder`,
`_evaluate_ladder_add`, `_is_warm`, `_ingest_bar`). **Parent spec:**
`docs/fno_strategies/options_scalper.md` §1, §2a, §3.6–§3.9.

**Hard invariants (mirror `orb.py`, never violated by entry logic):**
1. **No look-ahead.** Every trigger is computed only from bars *already closed* at `now`. The
   current tick is ingested into the rolling window **before** signal evaluation, but the trigger
   reads only past closes/highs/lows relative to its window — it never peeks at a future bar.
2. **Intraday reset.** All signal state (VWAP accumulator, rolling window, ORB, bars_seen,
   counters, cooldown) is cleared on a new `now.date()` via `_reset_session`. No state crosses days.
3. **Position via fills only.** An ENTER decision is a *request*; the tranche book changes only on
   `notify_fill`. `_pending_entry` blocks a second ENTER until the first is confirmed.
4. **Long-only.** Every ENTER is `side="BUY"`. There is no entry that sells to open.

---

## 1. Entry pipeline (order of gates in `_evaluate_entry`)

`on_tick` calls `_evaluate_entry(now, underlying_price, t)` **only when** `not self._pending_entry`
and **after** exits have been evaluated. Entry then passes through gates in this exact order; the
first failing gate returns `None` (no decision):

```
on_tick
  └─ future-skew guard (tick > wall+2min → None, no state mutation)   [§6.3 parent]
  └─ session reset if date changed
  └─ UNCONDITIONAL EOD square-off (exit; pre-empts all entry)          [§3.10 parent]
  └─ _ingest_bar(price, high, low)  → updates window + VWAP + ORB
  └─ compute self._current_direction = direction_signal(...)          ← THE TRIGGER (§2)
  └─ evaluate exits on open tranches (may return EXIT)
  └─ if not _pending_entry:
       _evaluate_entry:
         G1  warm-up gate           (_is_warm)                        [§3]
         G2  trade-window gate       (no_trade_open/close)            [§4]
         G3  standing-down gate      (daily-loss kill)                [exit-spec owned]
         G4a in-ladder?  → _evaluate_ladder_add   (pyramid rung)      [§5]
         G4b flat?       → _evaluate_new_ladder   (first rung)        [§6]
              └─ cooldown gate, max-trades gate, FLAT gate, strike → ScalpDecision(ENTER)
```

Every gate is **necessary**; the strength/quality filters that stop over-trading are baked into
the **trigger itself** (§2: VWAP deadband + momentum threshold + ATR activity floor) and into the
**session governors** (§7: cooldown, max-trades, warm-up, trade-window). A setup must clear *all*
of them to fire — this is the "only fire on high-quality setups" requirement made concrete.

---

## 2. The direction triggers (`direction_signal`)

`direction_signal(closes, highs, lows, params, vwap, orb_high, orb_low) -> "LONG"|"SHORT"|"FLAT"`
is a **pure** function over a trailing window of underlying 1-min bars (newest last). It returns a
**direction only**; it never reads option premium. The result is cached on
`self._current_direction` each tick and consumed by both entry paths and the signal-flip exit.

`params.signal` selects one of four triggers. All four are testable with hand-fed `on_tick`
streams (no DB, no network).

### 2a. `"vwap_mom"` — VWAP-anchored bias + signed-momentum trigger + ATR activity filter (DEFAULT)

This is the hardened default. It deliberately stacks **three independent confirmations** so the
scalper only fires when (a) the day's regime agrees, (b) price is actually thrusting, and (c) the
tape is alive. Any one failing → `FLAT` → no new ladder.

**Confirmation 1 — VWAP regime anchor (with deadband).** Running intraday VWAP of the underlying
(typical-price proxy, unit volume; maintained in `_ingest_bar`). Let `price = closes[-1]`:

| Condition | Anchor |
|---|---|
| `price > vwap·(1 + vwap_band)` | `LONG` |
| `price < vwap·(1 − vwap_band)` | `SHORT` |
| inside the band | **`FLAT` (return immediately)** |

- `vwap_band = 0.0005` (5 bps). The deadband is the **anti-chop filter #1**: while price hugs
  VWAP, the strategy is structurally flat and cannot open a ladder, so VWAP-grazing whipsaw never
  pays the round-trip cost stack.

**Confirmation 2 — signed k-min momentum thrust.** Within the anchor's direction, require a real
trailing-`k`-minute return:

```
ret_k = (closes[-1] - closes[-1-k]) / closes[-1-k]        # k = mom_k (default 5)
LONG  requires  ret_k >  +mom_thresh                       # mom_thresh = 0.0008 (8 bps)
SHORT requires  ret_k <  -mom_thresh
```

If the window has `< k+1` bars, or the thrust is too weak / wrong-signed → **`FLAT`**. This is the
**micro-breakout / thrust** trigger: it is the scalper's actual question ("is it moving, and which
way") and it is the **anti-chop filter #2** — a flat-but-above-VWAP drift cannot trigger an entry.

**Confirmation 3 — ATR activity floor.** Require the tape to be moving enough to scalp:

```
trs = [highs[-i] - lows[-i] for i in 1..atr_window]        # atr_window = 14
avg_tr = mean(trs)
require  avg_tr >= min_atr_pts                              # min_atr_pts = 6.0 NIFTY pts
```

If `< atr_window` bars, or `avg_tr < min_atr_pts` → **`FLAT`**. This is the ADX-spirit
**anti-chop filter #3**: a dead, low-range tape produces tiny premium moves that cannot clear the
fixed cost-per-round-trip, so entries are suppressed regardless of momentum.

**Result:** returns the anchor (`LONG`/`SHORT`) only when **all three** pass. Bullish → CE; bearish
→ PE (§8 strike). This triple-gate is the core "high-quality-setups-only" mechanism — three orthogonal
filters (regime, thrust, activity) must agree.

### 2b. `"ema"` — fast/slow EMA crossover (baseline-to-beat)

`EMA(ema_fast=5) vs EMA(ema_slow=20)` over the trailing `ema_slow` closes. `fast > slow → LONG`,
`fast < slow → SHORT`, equal → `FLAT`. **Anchor only — no momentum/ATR filter.** Provided so the
backtest can quantify how much the default's filters actually save in costs. Whipsaws in chop (its
known weakness) — not the hardened default.

### 2c. `"orb"` — opening-range break (low-signal baseline)

First-`orb_minutes` (default 15) high/low form the opening range (accumulated in `_ingest_bar`,
locked after the window). `price > orb_high → LONG`, `price < orb_low → SHORT`, inside → `FLAT`.
Roughly one bias per day — too few signals for a true scalper, included for parity with `orb.py`.

### 2d. `"momentum"` — signed k-min momentum only (noisy baseline)

`ret_k > mom_thresh → LONG`, `ret_k < -mom_thresh → SHORT`, else `FLAT`. No VWAP anchor, no ATR
floor. The deliberately over-trading baseline that shows how much the anchor/filter save.

**Test seam:** `self._signal_override` (never set in production) pins the direction so entry/ladder
mechanics can be tested independently of the trigger math. `_reset_session` clears it daily.

---

## 3. Warm-up gate (`_is_warm`) — G1

No entry until the signal inputs are warm. **Both** conditions must hold:

- **Clock:** `now >= MARKET_OPEN + warmup_minutes` (default `warmup_minutes = 15` → 09:30 IST).
  Keeps the scalper out of the opening-auction noise.
- **Bar count:** `bars_seen >= max(ema_slow, mom_k + 1, atr_window)` (default `max(20, 6, 14) = 20`
  one-minute bars) so every window the trigger reads is fully populated — this is what makes the
  **no-look-ahead** guarantee real (the trigger never reads a partially-filled window).

If not warm, `_evaluate_entry` returns `None` **and** `on_tick` forces `_current_direction = "FLAT"`
(so a pre-warm signal can never leak into the signal-flip exit either). Test seam
`_bypass_time_guards` skips only the clock half (bar-count still enforced); never set in production.

---

## 4. Trade-window gate — G2

No **new** ladder (or add) outside the intraday window:

- before `MARKET_OPEN + no_trade_open_min` (default 15 → 09:30), or
- at/after `MARKET_CLOSE − no_trade_close_min` (default 20 → 15:10).

Opening auction and pre-close illiquidity/expiry-pin both give bad fills. Exits are **not** gated by
this window — only entries. Bypassed by `_bypass_time_guards` (test only).

---

## 5. In-ladder add trigger (`_evaluate_ladder_add`) — pyramiding rung — G4a

When a ladder is already open (`self._tranches` non-empty), an entry is a **pyramid add**, not a new
ladder. Gates, in order:

1. **Rung cap:** `_rungs_requested >= max_rungs` (default 3) → `None`. Caps exposure at 3 lots.
2. **Direction still agrees:** `_current_direction == _ladder_direction`. If the anchor went `FLAT`
   or flipped, **no add** (a flip is handled by the exit spec, which flattens; it never adds).
3. **Favorable-move trigger** (measured in **underlying points** from the previous rung's level,
   `_last_rung_underlying`, so spacing is signal-native and premium-noise-free):

   ```
   move = underlying_price - _last_rung_underlying
   favorable = move    (LONG)    |    favorable = -move    (SHORT)

   ladder_mode == "pyramid"        (DEFAULT): add only if favorable >=  rung_spacing_pts
   ladder_mode == "scale_in_dips"           : add only if favorable <= -rung_spacing_pts
   ```

   `rung_spacing_pts = 10.0`. **Pyramid is the hardened default** — it adds only when the thesis is
   already working (winner), which is the anti-blow-up posture for long options (theta + delta both
   bleed if you average into adverse moves). `scale_in_dips` is provided for A/B only.

On pass: increment `_rungs_requested`, set `_last_rung_underlying = underlying_price`, compute lots
via `_tranche_lots_for_rung` (`flat` → `tranche_lots`; `decreasing` → 1, halve, floor 1), emit
ENTER (§9). The strike/option_type are **reused from the ladder context** — you ladder the *same*
contract, never a new strike.

---

## 6. New-ladder trigger (`_evaluate_new_ladder`) — first rung — G4b

When flat (`self._tranches` empty). Gates, in order:

1. **Cooldown gate:** if `now < _cooldown_until` → `None`. Set on every full flatten to
   `flatten_time + cooldown_min` (default 3 min). Prevents instant re-fire on the same micro-move
   — a key over-trading governor. (Tz-aware comparison; naive stamps coerced to IST.)
2. **Max-trades gate:** `_trades_today >= max_trades` (default 8 **ladders started**/day) → `None`.
   Hard ceiling on daily ladder count regardless of how many signals fire.
3. **FLAT gate:** `_current_direction == "FLAT"` → `None`. Only `LONG`/`SHORT` open a ladder.
4. **Strike + commit** (§8): compute ATM strike & option_type, fix the ladder context
   (`_ladder_direction/option_type/strike`, `_ladder_anchor_underlying`, `_last_rung_underlying`,
   `_rungs_requested = 1`), `_trades_today += 1`, emit ENTER (§9).

This is the only place `_trades_today` increments and the ladder context is initialised; both reset
daily.

---

## 7. The over-trading defense, summarized (why a scalper survives)

A setup must clear **every** layer below to produce a first-rung ENTER. The layers are independent,
so each one alone meaningfully cuts trade count:

| Layer | Param(s) | What it suppresses |
|---|---|---|
| VWAP deadband | `vwap_band=0.0005` | entries while price hugs fair value (chop) |
| Momentum thrust | `mom_k=5`, `mom_thresh=0.0008` | drift without a real move |
| ATR activity floor | `atr_window=14`, `min_atr_pts=6.0` | dead/low-range tape (targets can't clear costs) |
| Warm-up | `warmup_minutes=15`, 20-bar count | opening noise + unwarm windows |
| Trade window | `no_trade_open_min=15`, `no_trade_close_min=20` | auction + pre-close bad fills |
| Cooldown | `cooldown_min=3` | instant re-fire after a flatten |
| Max trades/day | `max_trades=8` | daily ladder-count blow-out |
| Daily-loss kill | `daily_loss_cap=8000` | trading after the day is already lost |
| `_pending_entry` | — | duplicate ENTER before a fill confirms |

---

## 8. Strike selection at first rung (§1d parent)

```
atm = _round_to_step(underlying_price, step)               # step = 50 grid
LONG : option_type = "CE", strike = atm + strike_offset*step
SHORT: option_type = "PE", strike = atm - strike_offset*step
```

`strike_offset` in signed grid steps (default `0` = ATM — the liquidity sweet spot, tightest spread,
delta≈0.5 + healthy gamma). `-1` = one step ITM (higher delta, more 1:1 tracking); `+1` = one step
OTM (cheaper/higher gamma, worse spread — generally not for scalping). Strike is **frozen at first
rung** and reused for every add.

---

## 9. ScalpDecision mapping (the interface contract)

Both entry paths return a frozen `ScalpDecision`. Entry decisions are always `action="ENTER"`,
`side="BUY"` (long-only):

```python
# New ladder, first rung (_evaluate_new_ladder)
ScalpDecision(
    action="ENTER", side="BUY",
    option_type="CE" | "PE",          # from direction (LONG→CE, SHORT→PE)
    strike=<ATM ± strike_offset*step>,
    lots=<_tranche_lots_for_rung(1)>, # flat→tranche_lots ; decreasing→1
    reason="Ladder open [LONG|SHORT] signal=<mode> underlying=<px>",
)

# Pyramid add (_evaluate_ladder_add)
ScalpDecision(
    action="ENTER", side="BUY",
    option_type=<ladder context>,      # reused — same contract
    strike=<ladder context>,
    lots=<_tranche_lots_for_rung(rung)>,
    reason="Ladder add rung <n> (+<favorable> pts in favor)",
)
```

When `_evaluate_entry` returns a non-`None` ENTER, `on_tick` sets `_pending_entry = True` and
returns it. The book does not change until `notify_fill("BUY", lots, premium, now)` appends the
tranche (and clears `_pending_entry`). `notify_fill` rejects `premium <= 0` (a zero entry would make
stop/TP fire instantly). A return of `None` means **no entry this tick** — the caller does nothing.

---

## 10. Entry-trigger test matrix (deterministic `on_tick`, no DB)

Each row maps a hand-fed underlying sequence to the expected ENTER (or `None`). Defaults from
parent §4; times IST; step 50; lot 65.

| # | Setup fed to `on_tick` | Expected ScalpDecision |
|---|---|---|
| E1 | Clean LONG (price>VWAP+band, `ret_5>+8bps`, `avg_tr>=6`) at **09:20** (pre warm-up) | `None` (G1 warm-up) |
| E2 | Same LONG, warm, in window, flat book, underlying 22030 | `ENTER BUY CE strike=22050 lots=1` |
| E3 | Symmetric SHORT (price<VWAP−band, `ret_5<−8bps`, ATR ok), underlying ~21970 | `ENTER BUY PE strike=ATM lots=1` |
| E4 | Price **inside** VWAP±band (deadband), ATR+momentum otherwise OK | `None` (trigger FLAT) |
| E5 | Strong momentum but `avg_tr < min_atr_pts` (flat tape); then ATR ≥ threshold | `None`, then `ENTER` |
| E6 | Strong momentum, `price>VWAP+band`, but `ret_5` below `mom_thresh` | `None` (thrust too weak) |
| E7 | After E2 fills (anchor 22030); underlying → 22040 (+10), bias LONG | `ENTER BUY CE` (same strike) rung 2 |
| E8 | Continue E7 → 22050 (+20) → rung 3; then 22060 (+30) | rung-3 `ENTER`, then `None` (max_rungs=3) |
| E9 | Open ladder LONG; anchor flips SHORT | `None` from entry path (exit spec flattens; no add) |
| E10 | Full flatten at 11:00; fresh LONG at 11:02 (<cooldown), then 11:03 | `None`, then `ENTER` |
| E11 | After `max_trades=8` ladders started; 9th qualifying signal | `None` (cap) |
| E12 | Qualifying signal at/after 15:10 (`MARKET_CLOSE−no_trade_close_min`) | `None` (trade window) |
| E13 | Qualifying ENTER emitted but `notify_fill` not yet called; next qualifying tick | `None` (`_pending_entry`) |
| E14 | Future-stamped tick (+5 min) with a clean LONG | `None`; no window/VWAP/counter mutation |
| E15 | `signal="momentum"`: `ret_5>thresh` on a flat-VWAP, low-ATR tape | `ENTER` (no anchor/ATR gates — baseline) |

These complement parent §7 (which covers the full lifecycle including exits); E1–E15 isolate the
**entry trigger + gate** behavior specifically.
