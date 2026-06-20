# 04 — Index-Agnostic Generalization: the F&O Index Registry

**Spec for:** the orchestration engine + the modules it sits on (collector, cost model, strategy builders).
**Status:** DESIGN ONLY. This is the **code-generalization** plan. The actual per-index **DATA** is a
separate ingestion task — see `05_data_ingestion.md` (to be written). **Today only NIFTY has data.**
**Owner lane:** orchestration layer (we own this). We do NOT edit `fno_strategies.py`/`fno_condor.py`
internals — we make them accept a param object and call them.

---

## 0. The problem (what is hardcoded today)

Every F&O module assumes NIFTY:

| Module | Hardcoded NIFTY assumption |
|---|---|
| `core/fno_backfill.py` | `SYMBOL_SCRIP={"NIFTY":13}`, `INDEX_SECURITY_IDS={"NIFTY":"13","INDIAVIX":"21"}`, `SYMBOL_STRIKE_STEP={"NIFTY":50}`, `underlying_seg="IDX_I"` default, `nifty_atm_strike(step=50)` |
| `research/backtest/fno_costs.py` | `NIFTY_LOT=65`; exchange = NSE rate `0.0003553`; STT NSE schedule |
| `research/backtest/fno_condor.py` | `step=50` default in `build_condor`; `lot=NIFTY_LOT`; `cycles_from_db(nifty_id="13", vix_id="21")` — VIX **is** the IV source |
| `ml/fno_vol_gate.py` | index-agnostic already — `gate_decision(realized_vol, implied_vol, k)` takes plain numbers (this is the lever for the no-VIX fallback) |

The single hardest dependency is **VIX as the weekly-IV proxy**: `cycles_from_db` reads
`index_bars` for `vix_id="21"` and sets `straddle_iv = vix_close / 100`. **India VIX is NIFTY-only.**
No other index has a published vol index, so every non-NIFTY index needs a different IV source.

---

## 1. Registry schema — `IndexSpec`

One frozen dataclass per F&O index, collected in a `INDEX_REGISTRY: dict[str, IndexSpec]`.
Proposed home: **`research/backtest/fno_index_registry.py`** (pure, no I/O, importable by both
`core/` and `research/` — mirror the placement of `fno_costs.py`). Keep it dependency-free so it
unit-tests without creds/DB.

```python
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum

class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"

class IVSource(str, Enum):
    VOL_INDEX = "vol_index"        # a published implied-vol index exists (India VIX → NIFTY)
    OPTION_CHAIN_ATM = "atm_iv"    # derive IV from our own ATM option-chain snapshot
    REALIZED_PROXY = "realized"    # NO IV available → fall back to scaled realized vol (degraded)

@dataclass(frozen=True)
class IndexSpec:
    # ── identity ────────────────────────────────────────────────────────────
    symbol: str                  # registry key, e.g. "BANKNIFTY"  (== expiry_calendar.symbol,
                                 #                                    option_atm_iv.symbol)
    display_name: str            # "Nifty Bank"
    exchange: Exchange           # NSE | BSE

    # ── option-chain / expiry-list endpoint params (UnderlyingScrip + UnderlyingSeg) ──
    underlying_scrip: int        # IDX_I scrip id for get_fno_option_chain / get_fno_expiry_list
    underlying_seg: str          # "IDX_I" (both NSE & BSE indices use IDX_I for chain lookups)

    # ── index-bar feed (IDX_I OHLCV → index_bars.security_id) ────────────────
    index_security_id: str       # IDX_I security id of the SPOT index (string — Dhan needs string)

    # ── F&O option segment (where the option legs actually trade) ────────────
    option_segment: str          # "NSE_FNO" | "BSE_FNO"  (cost model + live order routing)
    option_instrument: str       # "OPTIDX"

    # ── contract economics ───────────────────────────────────────────────────
    lot_size: int                # units per lot (cost model + condor sizing)
    strike_step: int             # ATM strike grid spacing (₹)

    # ── expiry calendar ──────────────────────────────────────────────────────
    expiry_weekday: int | None   # 0=Mon … 4=Fri; None = no weekly (monthly-only). ADVISORY only —
                                 # the real expiry set is read from expiry_calendar / chain endpoint;
                                 # this drives the synthetic-cycle ISO-boundary picker for backtests
                                 # and sanity-checks ingested expiries. NIFTY weekday changed in
                                 # 2024–25 — never treat as immutable; classify from actual dates.
    has_weekly: bool             # whether weekly expiries currently exist for this index

    # ── volatility / IV source (THE multi-index hard part) ───────────────────
    iv_source: IVSource
    vol_index_security_id: str | None   # IDX_I id of the vol index (India VIX="21"); None unless VOL_INDEX
    iv_realized_scale: float            # multiplier applied to realized vol when iv_source==REALIZED_PROXY
                                        # (term-structure fudge; default 1.10 — see §5)

    # ── data availability flag (drives the orchestrator's per-index enablement) ──
    has_data: bool               # True ONLY for indices we have ingested. NIFTY=True, all others=False
                                 # until ingestion lands. The orchestrator MUST skip has_data=False.
```

### Consumption contract (one rule)

> **No module reads a bare NIFTY constant any more.** Every collector/cost/strategy call takes an
> `IndexSpec` (or its fields) and reads `spec.lot_size`, `spec.strike_step`, `spec.underlying_scrip`,
> etc. The orchestrator resolves `spec = INDEX_REGISTRY[symbol]` once per cycle and threads it down.
> The existing NIFTY constants (`NIFTY_LOT`, `step=50`) stay only as **static fallbacks/defaults** so
> nothing breaks before the refactor; new code paths must prefer the spec.

---

## 2. Concrete per-index values

**CRITICAL — read before using any number below.** Only **NIFTY** is verified against the live Dhan
detailed scrip master (2026-06-19). Every other row is a **best-effort placeholder** marked
`[verify-me]`: `underlying_scrip` / `index_security_id` / `vol_index_security_id` MUST be confirmed
from the live Dhan scrip master, and `lot_size` / `strike_step` from the current NSE/BSE contract
circular, **before** that index is enabled. Lot sizes and steps are revised periodically by the
exchanges — do not hardcode-and-forget; prefer reading `lot_size`/`step` from the populated
`fno_instruments` table at runtime once ingestion exists, with the registry as the static fallback.

| symbol | exch | underlying_scrip | index_security_id | option_segment | lot_size | strike_step | expiry_weekday | iv_source | vol_index_id | has_data |
|---|---|---|---|---|---|---|---|---|---|---|
| **NIFTY** | NSE | **13** ✓ | **"13"** ✓ | NSE_FNO | **65** ✓ | **50** ✓ | Thu* | VOL_INDEX | **"21"** ✓ (India VIX) | **True** |
| BANKNIFTY | NSE | 25 `[verify-me]` | "25" `[verify-me]` | NSE_FNO | 35 `[verify-me]` | 100 `[verify-me]` | Wed* `[verify-me]` | OPTION_CHAIN_ATM | None | False |
| FINNIFTY | NSE | 27 `[verify-me]` | "27" `[verify-me]` | NSE_FNO | 65 `[verify-me]` | 50 `[verify-me]` | Tue* `[verify-me]` | OPTION_CHAIN_ATM | None | False |
| MIDCPNIFTY | NSE | 442 `[verify-me]` | "442" `[verify-me]` | NSE_FNO | 120 `[verify-me]` | 25 `[verify-me]` | Mon* `[verify-me]` | OPTION_CHAIN_ATM | None | False |
| SENSEX | BSE | 51 `[verify-me]` | "51" `[verify-me]` | BSE_FNO | 20 `[verify-me]` | 100 `[verify-me]` | Fri* `[verify-me]` | OPTION_CHAIN_ATM | None | False |
| BANKEX | BSE | 12311 `[verify-me]` | "12311" `[verify-me]` | BSE_FNO | 30 `[verify-me]` | 100 `[verify-me]` | Mon* `[verify-me]` | OPTION_CHAIN_ATM | None | False |

`* expiry_weekday` — NSE rationalised single-weekly per index in 2024–25 and **changed the weekday
more than once**; `live_feed.py` already references IDX_I ids `25` and `51`, consistent with the
BANKNIFTY/SENSEX guesses above but NOT a confirmation. Treat the weekday column as advisory and
classify the real expiry set with `classify_expiry()` from ingested dates.

Notes:
- `underlying_seg="IDX_I"` for **all** indices' chain/expiry lookups (NSE and BSE alike). Only the
  **option leg segment** differs: `NSE_FNO` vs `BSE_FNO`.
- Only NIFTY and (separately) India VIX exist in `index_bars` today. The grep confirms `IDX_I`
  ids `13` (NIFTY), `25`, `51` are valid feed ids; `21` is India VIX (NIFTY-only).

---

## 3. How each module consumes the registry

### 3a. Collector (`core/fno_backfill.py`)
Replace the three module-level NIFTY dicts with registry lookups:
- `SYMBOL_SCRIP[symbol]` → `spec.underlying_scrip`
- `INDEX_SECURITY_IDS[symbol]` → `spec.index_security_id`; the VIX entry becomes
  `spec.vol_index_security_id` (only present for VOL_INDEX indices)
- `SYMBOL_STRIKE_STEP.get(symbol, 50)` → `spec.strike_step`
- `underlying_seg="IDX_I"` default and `nifty_atm_strike(spot, step)` → pass `spec.underlying_seg`,
  `spec.strike_step`. Rename `nifty_atm_strike` → `atm_strike` (keep alias) — it's already generic.
- Futures backfill: `exchange_segment` becomes `spec.option_segment` for OPTIDX, or keep NSE_FNO/
  FUTIDX per `spec.exchange` for the futures leg.
- New collector entry point: `collect_index(spec: IndexSpec, ...)` that, per index, runs
  index-bars + (if VOL_INDEX) vol-index bars + expiry calendar + chain/ATM-IV snapshots. The CLI
  gains `--symbol BANKNIFTY` resolving through the registry; `--security-id` becomes optional
  (defaults from the spec).
- **`has_data` gate:** the collector is the producer; it ignores `has_data` (it's how data gets
  there). Everything downstream honors `has_data`.

### 3b. Cost model (`research/backtest/fno_costs.py`)
- `NIFTY_LOT` stays as a named fallback but `condor_costs(legs, ...)` already takes per-leg
  `qty_units`, so callers pass `spec.lot_size`-derived quantities — **no signature change needed**.
- **Exchange-rate split:** `OPTION_EXCHANGE_PCT` is the **NSE** rate. BSE option txn charges differ
  → make the per-exchange rate a function of `spec.exchange`:
  ```python
  EXCHANGE_OPTION_PCT = {Exchange.NSE: 0.0003553, Exchange.BSE: 0.0003250}  # BSE [verify-me]
  ```
  STT/SEBI/stamp/GST are statutory (same both exchanges) — unchanged. Add an `exchange` kwarg to
  `condor_costs` (default NSE for back-compat) that selects the exchange rate.

### 3c. Strategy builders (`research/backtest/fno_condor.py` + `fno_strategies.py`)
- `build_condor(..., step=50)` → caller passes `step=spec.strike_step`.
- `price_condor(..., lot=NIFTY_LOT)` / `resolve_condor(..., lot=NIFTY_LOT)` → pass `lot=spec.lot_size`.
  Internally `fno_condor.py` hardcodes `NIFTY_LOT` in several `* NIFTY_LOT` spots (lines ~429–454) —
  those must be parameterised to `lot` when generalising; for now the orchestrator only runs NIFTY so
  this is a follow-up flagged here, not a blocker.
- `cycles_from_db(nifty_id="13", vix_id="21")` is the **deepest NIFTY coupling** — see §4.

### 3d. Vol-gate (`ml/fno_vol_gate.py`)
No change. `gate_decision(realized_vol, implied_vol, k)` is already index-agnostic — it consumes two
numbers. The registry's job is to **supply the right `implied_vol`** per index (VIX, ATM-IV, or
realized-proxy). This is the clean seam that makes the no-VIX fallback a data-sourcing concern, not a
gate concern.

### 3e. Orchestrator (the layer we own)
Per cycle, per index:
```python
for symbol in enabled_symbols:
    spec = INDEX_REGISTRY[symbol]
    if not spec.has_data:
        continue                       # skip un-ingested indices (TODAY: everything but NIFTY)
    cycles = cycles_for_index(spec)    # generalised loader (see §4)
    ...                                # gate + strategy selection per existing flow
```

---

## 4. The IV-source seam — generalising `cycles_from_db`

`cycles_from_db` currently does, per boundary day: `straddle_iv = vix_map[date] / 100`. Generalise the
**`straddle_iv` resolution** behind `spec.iv_source`:

1. **`VOL_INDEX`** (NIFTY only): read the vol index from `index_bars` at
   `spec.vol_index_security_id`, `straddle_iv = close / 100`. Exactly today's path.
2. **`OPTION_CHAIN_ATM`** (BANKNIFTY/FINNIFTY/MIDCPNIFTY/SENSEX/BANKEX, once ingested): read
   `option_atm_iv.straddle_iv` for `(symbol, expiry/entry date)` — already a fraction (normalised by
   `extract_atm_iv`). This is **higher fidelity** than VIX (real per-expiry ATM IV, no 30-day VIX term
   mismatch) but **forward-only**: no historical option chains for these indices → these indices can
   only be **paper-logged forward**, not back-tested on history. State this loudly in the data spec.
3. **`REALIZED_PROXY`** — see §5.

Make `cycles_from_db` take a `spec: IndexSpec` (or `symbol`) instead of `nifty_id`/`vix_id`, and
dispatch the `straddle_iv` lookup on `spec.iv_source`. The NIFTY/`vix_id` kwargs become the
`VOL_INDEX` branch's internals. The realized-vol and spot lookups already key off
`spec.index_security_id` — only the IV source changes.

---

## 5. The no-VIX fallback (`REALIZED_PROXY`)

**Reality:** only NIFTY has a vol index. The other indices' true IV must come from their own ATM
option chain (`OPTION_CHAIN_ATM`). But until each index's chain has been snapshotted for enough
cycles, there is **no implied-vol series at all** → the vol-gate cannot run honestly.

The fallback, used ONLY when no IV series exists, is to **synthesise an implied-vol estimate from
realized vol**:

```
implied_vol_proxy = realized_vol_20d * spec.iv_realized_scale     # default scale ≈ 1.10
```

Rationale: index options almost always carry a **variance risk premium** — implied > realized on
average — so a flat ~10% uplift on trailing realized vol is a conservative stand-in. **This is a
degraded mode and must be flagged as such on every trade it produces.**

Hard rules for `REALIZED_PROXY`:
- It **defeats the entire edge.** The vol-gate's signal IS `realized < k·implied`. If
  `implied := realized·1.10` and `k=0.9`, then `realized < 0.9·1.10·realized = 0.99·realized` is
  **always false** → the gate **always STANDS ASIDE**. That is the correct, safe behaviour: with no
  real IV, you cannot measure VRP, so you do not sell premium. **`REALIZED_PROXY` is effectively a
  "stand aside until real IV exists" mode**, not a tradable signal.
- Therefore `REALIZED_PROXY` is a **placeholder for registry completeness**, not a trading path.
  Treat any index in `REALIZED_PROXY` as **not promotable** — paper-log only, and only to exercise the
  plumbing. The orchestrator should label these cycles `iv_source=REALIZED_PROXY` and exclude them
  from go/no-go.
- The instant an index accumulates a usable `option_atm_iv` history, flip its `iv_source` to
  `OPTION_CHAIN_ATM`. That is the real unlock — not the proxy.

(If a future, less-degenerate proxy is wanted, the right move is a **per-index empirical VRP constant**
fitted from that index's own realized-vs-ATM-IV once data exists — i.e. still a data task, not a
formula. Do not invent a VRP multiplier without data.)

---

## 6. Data-availability — the single most important flag

```
TODAY:  has_data = True  ONLY for NIFTY.
        index_bars + option chains + India VIX exist for NIFTY (13) + VIX (21) ONLY.
        BANKNIFTY/FINNIFTY/MIDCPNIFTY/SENSEX/BANKEX have NO rows anywhere.
```

- This spec is **code generalization only**. It lets the engine *accept* any index. It does **not**
  create data. Wiring the registry changes nothing about what runs until ingestion lands.
- The orchestrator **must** `continue` on `spec.has_data == False`. Flipping `has_data=True` is a
  deliberate step taken by the **data-ingestion task** (`05_data_ingestion.md`) after that index's
  index-bars + expiry-calendar + ATM-IV history is populated and sanity-checked.
- Historical option chains for non-NIFTY indices are **likely unavailable** from Dhan → those indices
  are **forward-paper-only** (start snapshotting now, backtest later when enough cycles accrue). NIFTY
  remains the only index with a real historical backtest. (This is HARD REALITY #1 in `_CONTEXT.md`.)

---

## 7. Implementation order (when ingestion exists — not now)

1. Add `research/backtest/fno_index_registry.py` (`IndexSpec`, `INDEX_REGISTRY`, enums). NIFTY row
   verified; all others `has_data=False`, fields `[verify-me]`. Pure + unit-tested (no DB/creds).
2. Refactor `core/fno_backfill.py` to resolve params from a spec (keep old dicts as a thin shim that
   builds from the registry so existing CLI calls still work).
3. Add per-exchange rate to `fno_costs.py` (`condor_costs(..., exchange=...)`, default NSE).
4. Thread `spec.strike_step` / `spec.lot_size` through `fno_condor.py` builders; parameterise the
   inline `* NIFTY_LOT` sites to `lot`.
5. Generalise `cycles_from_db` to dispatch `straddle_iv` on `spec.iv_source` (VOL_INDEX path == today).
6. Orchestrator threads `spec` per index and honors `has_data`.
7. **No `has_data` is flipped to True** for any non-NIFTY index until `05_data_ingestion.md` populates
   and verifies it.

---

## 8. Open questions (hand to data-ingestion lane)

- **Q-R1 [verify-me]:** confirm every `underlying_scrip` / `index_security_id` /
  `vol_index_security_id` from the live Dhan detailed scrip master before enabling an index.
- **Q-R2 [verify-me]:** confirm current `lot_size` + `strike_step` per index from the live NSE/BSE
  contract circular (these are revised; prefer runtime `fno_instruments`).
- **Q-R3:** confirm each index's CURRENT weekly expiry weekday (NSE/BSE changed these in 2024–25);
  do BANKNIFTY/FINNIFTY/MIDCPNIFTY still have *weekly* expiries, or monthly-only after rationalisation?
- **Q-R4:** confirm BSE (SENSEX/BANKEX) option transaction-charge rate for the cost model.
- **Q-R5:** does Dhan expose ANY historical option chain for non-NIFTY indices? If no (expected),
  those indices are forward-paper-only — confirm and document the start-snapshotting date.
```
