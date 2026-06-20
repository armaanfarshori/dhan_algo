# Orchestrator Spec 01 — Regime Taxonomy + Signal Set

**Scope:** Define the exact, point-in-time-computable inputs the F&O Strategy Orchestration Engine's
router uses, per cycle (and per index), to decide WHICH defined-risk GO strategy to deploy — or to
stand aside. This spec defines the SIGNALS only (computation + discretization + data source +
no-look-ahead). The routing matrix (signal-bucket → strategy) is a separate spec; this one produces
the bucketed `RegimeState` that the router consumes.

Read first: `orchestrator_specs/_CONTEXT.md`, `ml/fno_vol_gate.py`, `core/fno_derived.py`,
`research/backtest/fno_strategies.py`, `research/backtest/fno_condor.cycles_from_db`.

---

## 0. Design constraints (inherited, do NOT relitigate here)

1. **NIFTY only, historically.** Signal definitions must be index-agnostic in *form* (parameterised
   by `nifty_id`/`vix_id`/`step`/`lot`/calendar) but the only backtestable instance today is NIFTY
   (`index_bars` security_id `"13"`) + India VIX (`"21"`). Other indices are forward-only once
   ingested. Every signal below is sourced ONLY from data that exists for NIFTY today.
2. **One cycle = one weekly NIFTY expiry.** The unit of decision is a cycle as produced by
   `cycles_from_db(mode="weekly")` (historical) / `mode="expiry_calendar"` (forward). A cycle is
   evaluated ONCE, at `entry_date` (boundary-day CLOSE). The router runs once per cycle.
3. **Point-in-time only.** Every signal must be computable from data observable at/before
   `entry_date`'s close. No `expiry_spot`, no future bar, no full-series statistic that peeks past
   `entry_date`. `expiry_spot` exists in the cycle dict for *resolution* — the router MUST NOT read it.
4. **Reuse, don't reimplement.** `realized_vol_20d` comes from `core/fno_derived`; the vol-gate
   decision comes from `ml/fno_vol_gate.gate_decision`; `implied_move` from `core/fno_derived`. New
   signals (IV rank, trend) are computed here but on the SAME source columns.
5. **Fail-safe = STAND_ASIDE.** Any signal that is `None`/degenerate makes its bucket `UNKNOWN`; the
   router treats a regime containing any `UNKNOWN` load-bearing signal as stand-aside (mirrors the
   gate's fail-open contract — a missing signal never silently routes to an aggressive strategy).

---

## 1. Canonical inputs available per cycle

From `fno_condor.cycles_from_db` (the ONLY assembler the orchestrator calls — never re-query bars):

| Field | Type | Meaning | PIT-safe at entry? |
|---|---|---|---|
| `entry_date` | `date` | Boundary day = decision day | yes (this IS "now") |
| `expiry_date` | `date` | Next boundary = settlement proxy | yes (known forward) |
| `spot` | `float` | NIFTY close at `entry_date` | yes |
| `realized_vol_20d` | `float` | NIFTY trailing 20d annualised vol at `entry_date` | yes (backward-looking) |
| `straddle_iv` | `float` | India VIX close / 100 at `entry_date` (annualised fraction) | yes |
| `dte` | `int` | `(expiry_date − entry_date).days` (calendar) | yes |
| `expiry_spot` | `float` | NIFTY close at `expiry_date` | **NO — resolution only, router must not read** |

**Two derived columns the router additionally needs but `cycles_from_db` does NOT yet carry** — IV
rank and the trend pair (prior close + a short MA). These require a small read-only extension to the
cycle assembler OR a sidecar query. See §7 (Implementation hooks). Until then, signals 3 and 4 below
degrade to `UNKNOWN` and the router stands aside on regimes that depend on them — fail-safe by design.

Units convention (inherited): `realized_vol_20d` and `straddle_iv` are both **annualised fractions**
(0.12 = 12% p.a.). VIX is quoted in percent → `cycles_from_db` already divides by 100. Do not divide
again.

---

## 2. Signal catalogue (the router's input vector)

Six signals. Each section gives: definition, formula on existing columns, discretization (buckets),
edge cases → `UNKNOWN`, and PIT note.

The router consumes a frozen dataclass:

```python
@dataclass(frozen=True)
class RegimeState:
    # raw (for logging / calibration)
    rv: float | None            # realized_vol_20d
    iv: float | None            # straddle_iv (VIX/100)
    vrp_ratio: float | None     # rv / iv
    iv_rank: float | None       # 0..1, percentile of iv in trailing window
    trend_slope: float | None   # normalised short-MA slope (fraction)
    dte: int | None
    # bucketed (what the routing matrix keys on)
    vol_gate: str               # SELL_PREMIUM | BUY_PREMIUM | STAND_ASIDE  (from gate_decision)
    vrp_bucket: str             # RICH | NEUTRAL | CHEAP | UNKNOWN
    iv_level: str               # LOW | MID | HIGH | UNKNOWN
    iv_rank_bucket: str         # LOW | MID | HIGH | UNKNOWN
    trend_bucket: str           # UP | FLAT | DOWN | UNKNOWN
    dte_bucket: str             # ULTRA_SHORT | SHORT | NORMAL | LONG | UNKNOWN
```

`vol_gate` is the SAME string `gate_decision` returns (do not recompute the gate's logic — call it).

---

### Signal 1 — Vol-gate state (PRIMARY GATE)

**What:** The existing VRP gate's verdict — the master on/off and direction switch. Premium-SELLING
strategies are eligible only when `SELL_PREMIUM`; debit/long-vol only when `BUY_PREMIUM`.

**Compute:**
```python
from ml.fno_vol_gate import gate_decision, DEFAULT_K  # k ≈ 0.9
vol_gate = gate_decision(rv, iv, k=DEFAULT_K)
```
- `rv = cycle["realized_vol_20d"]`, `iv = cycle["straddle_iv"]`.
- Logic (do not duplicate, just know it): `rv > iv → BUY_PREMIUM`; `rv < k·iv → SELL_PREMIUM`; else
  `STAND_ASIDE`. With `k = 0.9`, IV must be ≥ ~111% of RV to call SELL.

**Buckets:** the three string constants `SELL_PREMIUM | BUY_PREMIUM | STAND_ASIDE` (no further
discretization — they already ARE the bucket).

**Edge cases:** `gate_decision` is fail-open → returns `STAND_ASIDE` on any `None`/degenerate input.
That is the desired behaviour; no extra handling needed.

**PIT:** clean — both inputs are `entry_date`-close observables.

**Why primary:** the GO evidence in `_CONTEXT.md` is *gated* ROM. The gate is the orchestrator's
spine; every other signal only refines WHICH premium-seller to pick inside a `SELL_PREMIUM` cycle.

---

### Signal 2 — VRP magnitude (RICHNESS)

**What:** *How rich* implied is vs realized, beyond the binary gate. The gate says "sell"; VRP
magnitude says "sell aggressively (wide condor / closer shorts)" vs "sell defensively (narrow,
defined, far OTM)". This is the continuous strength behind the gate.

**Compute:**
```python
vrp_ratio = rv / iv          # < 1 means implied richer than realized (premium-seller's friend)
# Optional spread form (vol points): vrp_spread = iv - rv
```
Use the **ratio**, not the spread, as the primary bucket key: the ratio is scale-free across vol
regimes (the spot·√(dte/365) move-scaling cancels — same argument the gate docstring makes), so a
fixed threshold means the same thing at VIX 11 and VIX 25. Report `vrp_spread` only for logging.

**Buckets** (ratio thresholds; aligned with `k=0.9` so RICH ⊂ SELL_PREMIUM region):
| Bucket | Condition | Reading |
|---|---|---|
| `RICH` | `vrp_ratio < 0.80` | implied ≥ 25% over realized — strong sell |
| `NEUTRAL` | `0.80 ≤ vrp_ratio < k` (≈0.90) | mild sell — gate still says SELL but thin edge |
| `CHEAP` | `vrp_ratio ≥ k` | no premium edge (gate = STAND_ASIDE or BUY) |
| `UNKNOWN` | `rv` or `iv` `None`/≤0 | fail-safe |

Thresholds (0.80 / `k`) are the **defaults**; they MUST be overridable and SHOULD be calibrated by
`fno_vol_gate.calibrate_threshold`-style quantiles on the trailing distribution once enough cycles
exist (see §6). Hard-coding 0.80 is a Phase-0 placeholder, not a claim.

**Edge cases:** `iv ≤ 0` → `UNKNOWN`. `rv = 0` (flat market) → `vrp_ratio = 0` → `RICH` (correct: a
dead-flat tape with any positive IV is maximally rich — but flag for the router, because realized=0
is often a data gap, see Signal 3 cross-check).

**PIT:** clean (same inputs as Signal 1).

**Caveat to carry forward:** `rv` is 20-day BACKWARD; `iv` (VIX) is 30-day FORWARD; the cycle is
~5–10 DTE. The horizon mismatch (documented in `fno_vol_gate` + `cycles_from_db`) means RICH can be
a stale-realized artefact at a vol-regime turn. The router must NOT treat VRP magnitude as a
standalone GO — it is a *modifier* on top of the gate, never an override of it.

---

### Signal 3 — IV level (ABSOLUTE) + IV rank (RELATIVE)

Two related but distinct signals. **Level** is the absolute IV; **rank** is its percentile in a
trailing window. Both matter: level sets margin/sizing reality (high VIX ⇒ wider real SPAN ⇒ ROM
optimism per honesty-ledger §4), rank sets mean-reversion expectation.

#### 3a. IV level
**Compute:** `iv_level_value = iv` (= VIX/100, already a fraction). For human logging keep
`vix_pts = iv * 100`.

**Buckets** (in VIX points, the intuitive unit; NIFTY-calibrated regime bands):
| Bucket | VIX pts | `iv` fraction |
|---|---|---|
| `LOW` | `< 12` | `< 0.12` |
| `MID` | `12 – 18` | `0.12 – 0.18` |
| `HIGH` | `> 18` | `> 0.18` |
| `UNKNOWN` | `iv` None/≤0 | — |

Bands are NIFTY-specific historical regimes (India VIX has spent most post-2015 time 10–16, with
event spikes 20+). They are config defaults, per-index in the registry; for a new index they must be
re-fit from its own VIX history (or set `UNKNOWN`/no-trade until enough history accrues).

**Why it gates strategy choice:** per honesty-ledger §4, `span_pct≈0.12` understates true SPAN in
HIGH-vol regimes → ROM optimism. The router should bias toward **defined-risk** structures and away
from anything `naked_short`/`spread_naked_mix` when `iv_level == HIGH`. (All GO strategies in
`_CONTEXT.md` are already defined-risk, so in practice HIGH mostly tightens wing widths / reduces
size — handled by the routing spec, but the SIGNAL must be present.)

#### 3b. IV rank (percentile)
**What:** Where today's IV sits in its own recent distribution. RICH-by-VRP + HIGH-rank = classic
mean-reversion premium sell; RICH + LOW-rank = vol could still climb (be defensive).

**Compute** (trailing-window percentile rank, PIT — window ENDS at `entry_date`, exclusive of future):
```python
# iv_hist = list of straddle_iv (VIX/100) for the WINDOW trailing trading days
#           STRICTLY up to and including entry_date (no future bars).
# Recommended WINDOW = 252 trading days (~1y), min_periods = 60.
def iv_rank(iv_today: float, iv_hist: list[float]) -> float | None:
    vals = [v for v in iv_hist if v is not None and v > 0]
    if len(vals) < 60:
        return None
    below = sum(1 for v in vals if v < iv_today)
    return below / len(vals)        # 0.0 .. 1.0
```
NOTE: this is **IV rank as a percentile** (fraction of trailing days with lower IV), which is robust
to outliers. The alternative "IV-Rank = (iv−min)/(max−min)" is more outlier-sensitive; prefer the
percentile form. Pick one and name it `iv_rank` consistently.

**Buckets:**
| Bucket | percentile |
|---|---|
| `LOW` | `< 0.30` |
| `MID` | `0.30 – 0.70` |
| `HIGH` | `> 0.70` |
| `UNKNOWN` | `< 60` trailing obs, or `iv` None |

**Edge cases:** insufficient history (early in the series, or a freshly-ingested index) → `UNKNOWN`.
Data gaps in the VIX series: filter `None`/≤0 before ranking (done above). A `realized_vol_20d == 0`
that produced a spurious `RICH` (Signal 2) is partly cross-checked here — true low-vol regimes show
`iv_level LOW` + low VIX, whereas a data gap shows inconsistent neighbours; log both so calibration
can catch it.

**PIT — this is the highest look-ahead risk in the whole spec.** `iv_rank` MUST use only VIX closes
`≤ entry_date`. Do NOT compute a single percentile over the full series and index into it (that
leaks the future distribution). The cycle assembler currently has no trailing IV history attached;
§7 specifies the read-only extension that supplies `iv_hist` per cycle.

---

### Signal 4 — Trend (index direction + slope)

**What:** NIFTY directional state. Distinguishes the *symmetric* premium sells (iron_condor,
broken_wing_condor — want FLAT) from the *directional defined-risk* sells (bull_put_spread,
credit_put_spread — want NOT-DOWN / mildly UP). This is what lets the orchestrator pick a put-side
credit spread in an uptrend vs a balanced condor in chop.

**Compute (slope of a short moving average, normalised, PIT):**
```python
# closes = NIFTY index closes (security_id "13", tf "1d") for the trailing
#          MA_WINDOW+1 trading days ending at entry_date (inclusive, no future).
# Use the SAME log-return convention as core/fno_derived for consistency.
MA_WINDOW = 20
def trend_slope(closes: list[float]) -> float | None:
    if len(closes) < MA_WINDOW + 1:
        return None
    # short MA today vs short MA MA_WINDOW days ago, normalised by spot
    ma_now  = mean(closes[-MA_WINDOW:])
    ma_prev = mean(closes[-(MA_WINDOW+? ): -?])   # see note
    return (ma_now - ma_prev) / ma_now            # fractional slope
```
Two acceptable, equivalent-in-spirit estimators — pick ONE and document it in code:
- **(A) MA-vs-MA:** `(SMA20_today − SMA20_{t−20}) / spot` — smooth, robust.
- **(B) spot-vs-MA:** `(spot − SMA20_today) / SMA20_today` — more responsive, what most desks use as
  "above/below the 20DMA".
Recommend **(B)** for the weekly cycle (responsive, single-MA, fewer bars needed: `MA_WINDOW+1`),
and report (A) as a secondary log field. Both are PIT (only past closes).

**Buckets** (fractional thresholds; tune in calibration):
| Bucket | condition (estimator B) | reading |
|---|---|---|
| `UP` | `slope > +0.01` (spot >1% above 20DMA) | bullish — favour put-side credit / bull_put_spread |
| `FLAT` | `−0.01 ≤ slope ≤ +0.01` | range — favour symmetric condor |
| `DOWN` | `slope < −0.01` | bearish — favour call-side credit / stand aside on put sells |
| `UNKNOWN` | `< MA_WINDOW+1` closes | fail-safe |

**Edge cases:** insufficient closes → `UNKNOWN`. A single gap day inside the window: SMA tolerates it
(use available closes, but require ≥ `MA_WINDOW` non-null); if too many nulls → `UNKNOWN`.

**PIT:** clean as long as `closes` ends at `entry_date`. Same look-ahead caution as Signal 3 — supply
trailing closes via the §7 hook, never a full-series MA.

**Honesty note:** the GO evidence in `_CONTEXT.md` shows directional/long-premium strategies as
NO-GO. Trend here is therefore used to pick AMONG defined-risk *credit* structures (where to place
the short, which side to skew the broken wing), NOT to greenlight a directional debit trade. The
router must keep `sell_premium=False` strategies disabled unless the gate says `BUY_PREMIUM` AND a
future spec promotes them — out of scope here.

---

### Signal 5 — DTE (time to expiry)

**What:** Calendar days to settlement. Governs theta profile and the realized-vs-implied horizon
fidelity. Weekly cycles cluster ~5–10 DTE but the boundary mechanics in `cycles_from_db` can yield
short stubs (holiday-shortened weeks) or longer gaps.

**Compute:** `dte = cycle["dte"]` (already `(expiry_date − entry_date).days`, calendar days).

**Buckets:**
| Bucket | DTE (calendar) | reading |
|---|---|---|
| `ULTRA_SHORT` | `dte ≤ 2` | expiry-week stub — gamma-heavy; router should reduce size or stand aside |
| `SHORT` | `3 – 5` | the sweet spot for weekly premium decay |
| `NORMAL` | `6 – 10` | standard weekly |
| `LONG` | `> 10` | holiday gap / monthly-ish — VRP horizon mismatch worsens |
| `UNKNOWN` | `dte` None or `< 0` | fail-safe |

**Edge cases:** `dte == 0` (entry==expiry, degenerate boundary pair) → treat as `UNKNOWN` and the
upstream `implied_move` guard already zeroes the trade; router stands aside. `dte < 0` (mis-ordered
pair) → `UNKNOWN`.

**PIT:** clean (both dates known at entry).

---

### Signal 6 — Cross-checks / data-quality flags (not routed on, but veto-capable)

Not a routing key — a **veto layer**. The router multiplies the chosen strategy by a "deployable"
boolean. Any of these → force stand-aside regardless of buckets:

- `iv` or `rv` is `None`/≤0 (already → `UNKNOWN` upstream).
- `vrp_ratio` extreme (`< 0.3` or `> 3.0`) → almost always a data artefact (e.g. `rv` from a gapped
  series); log + veto.
- `spot ≤ 0` (corrupt bar) → veto.
- `dte_bucket == UNKNOWN`.
- Stale entry: `entry_date` older than the most recent available NIFTY bar by > N days in forward
  mode (live only — guards against acting on a stale cycle). Not applicable to historical backtest.

These mirror the defensive guards already in `run_strategy_backtest` (the required-field + sanity
guards at lines ~1368–1387) — the orchestrator must apply the SAME guards BEFORE routing so it never
proposes a strategy the engine would silently drop.

---

## 3. The regime vector → what the router keys on

The router's lookup key is the tuple:

```
(vol_gate, vrp_bucket, iv_level, iv_rank_bucket, trend_bucket, dte_bucket)
```

with `vol_gate` as the dominant axis. Concretely the routing spec will collapse this to a small set
of named *regimes* (e.g. "calm premium-rich range", "elevated mean-reverting", "trending-up
premium", "uncertain/stand-aside"), but the SIGNAL layer's job ends at producing the fully bucketed
`RegimeState`. Load-bearing axes for the GO strategies in `_CONTEXT.md`:

- **iron_condor / broken_wing_condor** (symmetric): want `vol_gate=SELL_PREMIUM`, `trend=FLAT`,
  prefer `vrp=RICH`, any `iv_level` (defined-risk), `dte ∈ {SHORT,NORMAL}`.
- **bull_put_spread / credit_put_spread** (put-side directional credit): want `SELL_PREMIUM`,
  `trend ∈ {UP, FLAT}` (never `DOWN`), `vrp ∈ {RICH,NEUTRAL}`, `dte ∈ {SHORT,NORMAL}`.
- **any** in `iv_level=HIGH`: keep defined-risk only, tighten wings (routing spec) — never widen into
  naked. `iv_rank=HIGH` strengthens the mean-reversion case for selling.

This mapping is illustrative; the authoritative matrix lives in the routing spec.

---

## 4. No-look-ahead correctness — summary checklist

Every signal is computed from observables `≤ entry_date` close. Enforce in implementation:

1. **Never read `cycle["expiry_spot"]`** in the router / signal layer. It exists only for resolution.
   (Add an assertion or simply don't pass it into `RegimeState`.)
2. **Trailing windows end at `entry_date`** — `iv_rank` (Signal 3b) and `trend_slope` (Signal 4) must
   slice histories `[…, entry_date]`, never the full series. The §7 hook supplies per-cycle trailing
   slices precisely to make this impossible to get wrong.
3. **`realized_vol_20d` is already backward-looking** (`core/fno_derived.realized_vol_series` uses a
   trailing rolling window with `min_periods=window`; the bar at `entry_date` summarises returns
   ENDING at `entry_date`). PIT-clean by construction — reuse it, don't recompute.
4. **Calibrated thresholds (k, VRP bands, IV-level bands) must be fit on a TRAINING slice only**
   (chronological, e.g. first 70%) and FROZEN for the test slice — never fit on the full history then
   evaluate on it. This matches the 70/30 IS/OOS discipline already in `run_strategy_backtest`.
5. **Bucket boundaries are config, not magic numbers** — store per-index in the registry so a
   look-ahead-free re-fit per index is possible and auditable.

---

## 5. Edge-case matrix (consolidated)

| Condition | Affected signal(s) | Behaviour |
|---|---|---|
| `iv` (VIX) None / ≤ 0 | 1,2,3 | all → `UNKNOWN`/`STAND_ASIDE`; veto |
| `rv` None / ≤ 0 | 1,2 | gate `STAND_ASIDE`; `vrp_bucket UNKNOWN`; veto if rv computed but 0 from a gap |
| `rv == 0` genuine flat tape | 2 | `RICH` (correct) but log + cross-check vs `iv_level` |
| `< 60` trailing IV obs | 3b | `iv_rank_bucket = UNKNOWN`; router stands aside on rank-dependent regimes |
| `< MA_WINDOW+1` closes | 4 | `trend_bucket = UNKNOWN`; stands aside on trend-dependent regimes |
| `dte ≤ 0` | 5 | `UNKNOWN`; engine guard zeroes trade anyway; stand aside |
| `dte > 10` | 5 | `LONG`; allowed but flag horizon-mismatch widening in routing |
| `vrp_ratio` <0.3 or >3.0 | 6 | data-artefact veto (force stand-aside) |
| Forward-mode stale cycle | 6 | veto (live only) |
| New index, no VIX history | 3,4 | `UNKNOWN` until history accrues → forward-only, no-trade |

---

## 6. Calibration hooks (Phase-0 defaults are placeholders)

- **`k` (vol-gate):** already calibratable via `fno_vol_gate.calibrate_threshold` (quantile of
  `rv/iv`). Default `0.9`. The orchestrator should inherit the engine's `k`, not invent its own.
- **VRP bands (0.80 / k):** fit as quantiles of trailing `vrp_ratio` (e.g. 30th pct → RICH/NEUTRAL
  boundary) on the training slice.
- **IV-level bands (12/18 VIX pts):** re-fit per index from its VIX terciles on training data.
- **IV-rank window (252) / trend MA (20) / DTE bands:** structural defaults; expose as config, sweep
  in the orchestrator backtest, FREEZE on OOS.

Calibration is a separate task; this spec only declares the knobs and that they must be fit
look-ahead-free.

---

## 7. Implementation hooks (read-only extension to the cycle assembler)

The router needs two trailing histories per cycle that `cycles_from_db` does not currently attach:
`iv_hist` (VIX/100 closes ≤ entry_date) and `closes_hist` (NIFTY closes ≤ entry_date). Three options,
in order of preference:

1. **Extend `cycles_from_db` (preferred, but coordinate — fno owns that file).** It already builds
   `nifty_map`/`vix_map` (date→close) in `_build_bar_maps`. Attach, per cycle, the trailing slice of
   each map up to `entry_date`. This is the cleanest PIT-correct source and reuses the existing
   single DB read. Per `_CONTEXT.md` lanes: **do not edit `fno_strategies.py` internals**, but
   `cycles_from_db` lives in `fno_condor.py` — propose this via the bus / a PR to fno, do not patch
   it unilaterally.
2. **Sidecar read in the orchestrator** (no fno edit): one read-only query of `index_bars`
   (security_id `"13"` and `"21"`, tf `"1d"`) into two date→close maps at orchestrator start, then
   per cycle slice `≤ entry_date`. Self-contained; one extra query; fully PIT-safe. **Recommended
   until the assembler extension lands.**
3. **Recompute from `cycle["spot"]`/`straddle_iv` across the cycle list** — only works for cycle
   boundary days, too sparse for a 252-day IV rank or 20-day MA. **Rejected** (insufficient density).

The signal functions themselves (`iv_rank`, `trend_slope`, the bucketers) are **pure + DB-free**
(like `core/fno_derived`'s pure metrics) and unit-testable with hand-fed lists. Only the assembler /
sidecar touches the DB. Keep that separation.

### Suggested module layout
```
research/backtest/orchestrator/regime.py
    RegimeState (frozen dataclass)
    vrp_ratio(rv, iv) -> float|None
    iv_rank(iv_today, iv_hist) -> float|None
    trend_slope(closes) -> float|None
    bucket_vrp / bucket_iv_level / bucket_iv_rank / bucket_trend / bucket_dte
    compute_regime(cycle, iv_hist, closes_hist, k=DEFAULT_K, cfg=RegimeConfig()) -> RegimeState
    RegimeConfig  (all thresholds/windows — per-index overridable)
```
`compute_regime` calls `ml.fno_vol_gate.gate_decision` for `vol_gate` (never reimplements it) and
applies the §6 vetoes. It returns a fully-bucketed `RegimeState`; the routing spec's `route(state)`
consumes it.

---

## 8. What this spec deliberately does NOT do

- Does not define the regime→strategy routing matrix (next spec).
- Does not add live execution / order paths (PAPER only; out of scope).
- Does not promote any NO-GO strategy. Directional/debit stay disabled at the SIGNAL level by keeping
  `trend` a *modifier within credit structures*, never a debit greenlight.
- Does not touch `fno_strategies.py` internals or edit `cycles_from_db` unilaterally — it specifies a
  read-only extension to be coordinated with the fno lane.
```
