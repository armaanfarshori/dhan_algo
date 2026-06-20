# 10 — Synthesis & Honest Verdict: the Hardened Intraday Options Scalper

**Status:** PLAN / SYNTHESIS — research only, PAPER. No live order paths.
**Scope:** Pulls together the per-topic specs (01–09) and the master spec
(`docs/fno_strategies/options_scalper.md`) into one honest assessment, a "bulletproof"
risk-discipline design, and the build/validation sequence with explicit go/no-go criteria.
**Subject under review:** `strategies/options_scalper.py` (`OptionsScalper`, `direction_signal`,
`ScalperParams`, `ScalpDecision`).

> **Read this first.** This document does **not** claim the scalper makes money. It claims
> two narrower, defensible things: (1) a positive-EV intraday option scalper is *possible but
> hard*, gated almost entirely by cost/spread, and is **unproven** for us; (2) we can build a
> version that is *bulletproof on the downside* — ruthless risk discipline that caps how much a
> bad day costs — which is a different and achievable goal than "green every day." "Always makes
> money" is impossible and we will not pretend otherwise. Validation is **forward-only**, because
> the historical data this strategy would need **does not exist** (master spec §5).

---

## 1. Honest assessment — is a positive-EV intraday option scalper plausible?

**Short answer: plausible in principle, but the bar is brutal and we have not cleared it.**
The whole game is whether the *average winning scalp clears the round-trip cost + spread with
margin*. Everything else (signal quality, ladder shape) is second-order to that single inequality.

### 1a. The cost/spread bar, in real numbers

From `research/backtest/fno_costs.py` (post-April-2026 NSE/SEBI rates) and the master spec §5d:

- **Brokerage:** ₹20 per executed order → a scalp is BUY + SELL = **₹40 per round-trip per leg**,
  flat, regardless of size. With a 3-rung ladder fully cycled that is up to 6 orders = ₹120 just
  in brokerage for one ladder.
- **STT:** 0.15% of premium on the **sell side** (`OPTION_STT_SELL_PCT = 0.0015`).
- **Exchange + SEBI + stamp + 18% GST** stack on top (`OPTION_EXCHANGE_PCT`, `SEBI_PCT`,
  `STAMP_BUY_PCT`, `GST_PCT`).
- **Slippage** — the killer. `slippage(premium, pct)` defaults to 0.5% for liquid mids; the spec
  itself says use **1%+** for small-target scalping because **you cross the spread both ways on
  every tranche**.

**Worked example (master spec §5d, made explicit):** ATM NIFTY premium ≈ ₹120, lot = 65.
- A +10% TP target = ₹12/unit gross = **₹780/lot gross**.
- ₹40 brokerage + STT/exchange/SEBI/stamp/GST + slippage at ~₹1.2–2.4/unit *each side*
  (≈ ₹150–310/lot on slippage alone) can erase **a third to a half** of that ₹780 before you
  count a single losing scalp.

**Implication:** the **break-even target %** at our assumed cost/slippage is large relative to
the +10% first rung. The scalper is only EV-positive if the *win rate × average net win*
genuinely exceeds *loss rate × average net loss + dead-scalp cost drag*. That is an empirical
question we **cannot answer from history** and must answer forward, in paper, with **real spreads**.

### 1b. Where the (possible) edge could come from

The design concentrates the few structural advantages a long-option intraday scalper can have:

- **ATM strike selection** (`strike_offset = 0`) — the liquidity sweet spot: tightest spread →
  least slippage (and slippage *is* the game), delta ≈ 0.5 with healthy gamma so a few underlying
  points convert into a scalp-able premium move.
- **Chop suppression** — the default `vwap_mom` signal (VWAP regime anchor + signed k-min momentum
  trigger + ATR activity filter) exists *specifically* to avoid the cost-killer: overtrading flat
  tape. Each avoided chop entry is two saved cost stacks.
- **Asymmetric exits** — laddered partial TPs lock small wins, while the trailing remainder lets
  one good move pay for several scratch round-trips. Long-only means **defined max loss per
  tranche** (the premium), no naked-short tail.
- **Theta discipline** — the 12-min time-stop refuses to let a stagnant long bleed; a stagnant
  scalp is a losing scalp.

### 1c. Where it most likely fails

- **Spread on every round-trip** dwarfs the edge if entries are even slightly too frequent.
- **Signal whipsaw** — any signal that flips in chop pays the cost stack twice per flip; the
  anchor+filter mitigate but do not eliminate this.
- **IV crush / theta** on long premium — the strategy is structurally short the things that make
  options cheap to hold.
- **Fill realism** — paper TP/stop assume you get the LTP; in size near the close or in a fast
  move you will not.

**Verdict on plausibility:** *Possible, not promised.* The honest prior is that most retail
intraday option-buying scalpers are net-negative **after costs**, and that the ones that survive
do so on ruthless cost control and risk discipline rather than signal genius. So we build for
**survival and downside control first**, and let forward paper decide if any edge survives costs.

---

## 2. The "bulletproof" design — ruthless risk discipline (PROTECTS days; does NOT guarantee green)

**Plainly stated:** "bulletproof" here means **the downside is bounded and the bad days are
small and rare**, *not* that every day is green. No intraday strategy can guarantee daily
profit. What we *can* engineer is a system where a bad day costs a known, capped amount and the
strategy stands itself down before a bad day becomes a disaster. That is the achievable goal.

The hardening is three concentric rings already present in `OptionsScalper`, plus their intended
parameterisation:

### 2a. Ring 1 — the daily governor (caps the day)

- **Daily-loss kill** (`daily_loss_cap = 8000.0`): realized net P&L is tracked tick-by-tick in
  `_update_realized_pnl`; once it crosses `−daily_loss_cap`, `_standing_down = True`, the open book
  is flattened on the next tick (`_evaluate_exits` → `_flatten_all`), and **no new ladder opens
  for the rest of the day**. This is the strategy-level analogue of the platform `RiskEngine`.
- **Max trades/day** (`max_trades = 8`): caps *ladders started* per session, bounding cost drag
  and overtrading even on a flat-but-noisy tape.
- **Re-entry cooldown** (`cooldown_min = 3`): after any full flatten, no new ladder for N minutes
  — prevents instant re-fire on the same micro-move.
- **Trade window** (`no_trade_open_min = 15`, `no_trade_close_min = 20`): no new ladders in the
  opening auction or the pre-close illiquidity/pin, where fills are worst.

> The daily governor is what makes "bulletproof" *true in the bounded sense*: the worst realized
> day is approximately `−daily_loss_cap` plus one in-flight ladder's stop, not unbounded.

### 2b. Ring 2 — the regime filter (refuses to play bad tape)

- **VWAP deadband** (`vwap_band = 0.0005`): inside `VWAP·(1±band)` the signal is FLAT → no new
  entries. Kills VWAP-hugging chop.
- **Momentum trigger** (`mom_k = 5`, `mom_thresh = 0.0008`): only enter when the trailing-k-min
  return *confirms* the anchor direction.
- **ATR activity filter** (`atr_window = 14`, `min_atr_pts = 6.0`): if the tape is dead (avg true
  range below threshold), **do not scalp at all**. This is the single most important *entry*
  guard for cost survival.
- **Signal-flip flatten**: if the anchor flips against an open ladder, flatten the whole ladder
  immediately (no same-tick reversal — re-qualify next tick to avoid ping-pong).

### 2c. Ring 3 — tight, layered exits (caps each trade)

Priority order enforced in `_evaluate_exits`:
1. **Signal-flip** → flatten ladder.
2. **Daily stand-down residual** → flatten.
3. **Hard stop** (`stop_pct = 0.20`): exit any tranche at −20% of its fill premium; all-breach →
   one `_flatten_all`.
4. **Time-stop** (`time_stop_min = 12`): exit any tranche open ≥12 min that hasn't hit TP1 (theta
   guard).
5. **TP ladder** (`tp_ladder_pct = [0.10, 0.20, 0.35]`) then **trailing remainder**
   (`trail_pct = 0.12`).
6. **Unconditional EOD square-off** (`squareoff_before_close_min = 5`): above *all* gates — no
   long premium is ever carried overnight, even after a mid-session restart with unknown state.

### 2d. The seven hard rules (mirror `orb.py`) that make it safe to run

1. Session reset on date change (`_reset_session`).
2. **Unconditional EOD square-off** (above every other gate).
3. Future-skew guard (`MAX_FUTURE_SKEW = 2 min`) — a future-stamped tick never resets the session
   or advances a timer.
4. **Position via fills only** — the tranche book changes only through `notify_fill`/`notify_flat`;
   `on_tick` never assumes an order filled (`_pending_entry` guards in-flight rungs).
5. **Long-only** — every ENTER is BUY; only SELLs are exits (bounded max loss = premium).
6. **Fail-safe on missing premium** — if a position is open and `option_premium is None`, emit
   **no** premium-based exit and warn; never fabricate a premium (Kronos fail-open spirit).
7. **PAPER only** — no live order path from this module; live wiring routes through the real
   `RiskEngine`, which owns the kill-switch and is never bypassed.

**What "bulletproof" explicitly is NOT:** a guarantee of a green day, a claim of positive
expectancy, or a substitute for the forward-paper validation in §4. It is a *downside contract*:
known max daily loss, no overnight risk, no naked tail, no fabricated fills.

---

## 3. Build sequence — harden the code + unit tests now, then forward-paper

The strategy logic exists and is pure/synchronous. The near-term work is **hardening + tests on
synthetic `on_tick` streams**, because the historical data for a backtest does not exist
(master spec §5).

### Phase A — Harden the engine and lock behaviour with unit tests (NOW, no data needed)

1. **Unit tests on synthetic `on_tick`/premium sequences** — implement the 16 deterministic cases
   from master spec §7, hand-feeding `(now, underlying_price, option_premium)` and asserting the
   returned `ScalpDecision` (or `None`). No DB, no network — same testability contract as
   `tests/` around `orb.py`. Coverage targets:
   - Warm-up blocks entry; FLAT-zone suppression; ATR filter blocks chop.
   - First entry CE/PE side selection + ATM rounding (`_round_to_step`).
   - Ladder pyramiding to `max_rungs`; TP ladder partial exits in order; trailing remainder;
     hard stop; time-stop.
   - Signal-flip flatten with no same-tick re-entry; cooldown + `max_trades` cap; daily-loss kill
     stand-down; **unconditional EOD square-off**; future-skew guard; **stale-premium fail-safe**.
2. **Test the signal helper independently** — `direction_signal(...)` for each `signal` mode
   (`vwap_mom`, `ema`, `orb`, `momentum`) over hand-built trailing windows.
3. **Run `pytest -q` + `ruff`** green; this is necessary-but-not-sufficient (see memory
   `qa-before-commit`). Land on a feature branch + PR; CI gates it.
4. **Do NOT wire a historical backtest.** The optional Black-76 intraday path is **explicitly
   conditional** on a *real* intraday NIFTY index 1-min path existing (master spec §5c). Never
   synthesise an intraday path from daily OHLCV — that fabricates the very premium moves the
   strategy trades. If built later, gate it behind an "intraday index data present" check and
   stamp the report "premiums modeled, not observed; σ held constant" — and treat any result as
   **structural sanity only, never go/no-go**.

### Phase B — Forward-paper validation (separate DATA PR — the real unlock)

The strategy is a pure `on_tick` engine; what's missing is the **intraday option-LTP feed**.
`core/fno_collector.py` is **EOD-only** today. So Phase B is primarily a **data/infrastructure
PR**, separate from the strategy file:

1. **Intraday underlying feed** — NIFTY spot 1-min (or tick) → drives signal + entry rungs.
2. **Intraday ATM option-LTP subscription** — a live WS/poll of the specific held ATM CE/PE
   `security_id`s, re-selected as spot crosses strikes → drives TP/stop/trail/time-stop. **This
   does not exist yet and is the prerequisite.**
3. **Intraday paper-trade log** — a table analogous to `fno_paper_trades` (intraday variant)
   recording every paper fill + exit with the **real spread paid**, so an honest forward track
   accrues.
4. **Report must surface, prominently:** net-of-cost **per-scalp expectancy**, the **break-even
   target %** at the assumed cost/slippage, win rate, average net win/loss, max daily loss
   actually hit, and number of dead (cost-only) scalps. These are the numbers that decide it.

---

## 4. Explicit caveat + go/no-go criteria (forward paper)

### 4a. The caveat, stated plainly

- **"Always makes money" is impossible.** No intraday option strategy guarantees a green day.
  Anyone (human or model) who claims otherwise is wrong. We engineer **bounded downside**, not
  guaranteed upside.
- **Validation is forward-only.** A faithful historical backtest is **blocked on data that does
  not exist** (no intraday option-premium series; no intraday NIFTY 1-min series in the DB —
  master spec §5a). The only honest evidence is **forward paper-trading on a live intraday feed
  with real spreads**. Any "backtest" of this strategy on current data would be fabrication.
- **Costs decide it, not the signal.** The go/no-go must be measured **net of the full cost stack
  and aggressive slippage**, not on gross premium moves.

### 4b. Go / No-Go criteria from forward paper

Run forward paper for a **pre-committed window** before judging (proposed: **≥ 20 trading
sessions** and **≥ 150 completed scalps** so per-scalp statistics are not noise). Decide on the
**net-of-cost** numbers from the §3B report. Criteria are illustrative defaults to be ratified by
the user before the run starts — pre-commit them so the verdict is not curve-fit after the fact.

**GO (promote to a larger paper allocation / consider tiny-live track) — ALL must hold:**
- **Net-of-cost per-scalp expectancy > 0** with a margin (e.g. mean net win/round-trip ≥ 1.3 ×
  modeled cost+slippage per round-trip), not marginally positive.
- **Positive net P&L across the window** with a sane distribution (not one lucky day carrying it —
  median daily ≥ 0, or a Sharpe-style consistency check).
- **Realized max daily loss ≤ `daily_loss_cap`** in practice (the governor actually held).
- **Dead/scratch scalp rate** low enough that cost drag does not dominate (e.g. cost-only
  round-trips < ~40% of all round-trips).
- **No correctness incidents** in paper (no fabricated fills, no missed EOD square-off, no
  overnight carry, fail-safe behaved on stale premium).

**NO-GO (do not advance; iterate or shelve) — ANY triggers:**
- Net-of-cost per-scalp expectancy ≤ 0, **or** positive only before costs.
- Edge depends on a handful of outlier days (fat-tailed, not repeatable).
- Daily governor breached, EOD square-off missed, or any naked-overnight/fabricated-fill event.
- Slippage in real fills materially worse than modeled (the spread eats the small targets) such
  that the break-even target % exceeds the TP ladder's first rung in practice.

**Decision owner:** the user, on the pre-committed numbers. PAPER stays `true` throughout; any
live consideration is a separate, later track behind the platform `RiskEngine`.

---

## 5. One-paragraph verdict

A positive-EV intraday NIFTY option scalper is **plausible but unproven**, and its viability is
decided almost entirely by **cost and spread**, not by signal cleverness — the average winning
scalp must clear ~₹40 brokerage + STT/fees + spread-crossed-both-ways slippage with real margin,
and that is an empirical question we **cannot** answer from history because the intraday
option-premium data does not exist. What we *can* deliver now is the **bulletproof-downside**
version: a daily governor (loss cap + max-trades + cooldown + trade window) wrapped around a
regime filter (VWAP deadband + momentum + ATR) and tight layered exits (hard stop, theta
time-stop, TP ladder + trail), all under the seven `orb.py` hard rules — which **bounds the cost
of a bad day and removes overnight/naked tail risk**, but does **not** promise a green day. The
build path is: harden the pure engine with the 16 synthetic `on_tick` unit tests now (no data
needed), then validate **forward-only** via a separate intraday-LTP-feed data PR, and promote or
shelve strictly on **pre-committed, net-of-cost** go/no-go criteria. "Always makes money" is
impossible; "small, rare, capped losing days while we find out if any edge survives costs" is the
honest, achievable target.
