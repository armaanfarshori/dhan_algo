# 07 — Multi-Index Data Ingestion (BANKNIFTY · FINNIFTY · MIDCPNIFTY · SENSEX · BANKEX)

**Scope:** extend the NIFTY-only F&O data foundation (`core/fno_backfill.py` + `core/fno_collector.py`,
schema in Alembic 009/010) to ingest the five additional index-option underlyings the orchestrator
needs. This is the DATA INGESTION plan called out in `_CONTEXT.md` HARD REALITY #1.

> ⚠️ **TRUSTED-MACHINE LIVE-DATA TASK.** Every operation here is a *live* Dhan v2 API call
> (`optionchain`, `optionchain/expirylist`, `charts/historical`). Per CLAUDE.md and the F&O handoff,
> these run **off-hours only** (the `_assert_off_hours` guard already enforces 09:15–15:30 IST refusal)
> and **from the trusted machine** that holds the Dhan creds. Data reads are not IP-whitelisted (only
> order placement is), so any IP *can* read — but the off-hours guard + token ownership keep this lane
> on the trusted box. The Mac / PR-lane Claudes plan + write code; they do NOT run ingestion.
> No order paths touched anywhere. PAPER only.

---

## 1. Current state (what exists, NIFTY-only)

The pipeline already generalises cleanly because **every table is keyed by symbol / scrip / security_id**,
not hard-coded to NIFTY:

| Table | Key | Per-index field already present |
|---|---|---|
| `index_bars` (010) | `(security_id, timeframe, time)` | `security_id`, `symbol` |
| `option_chain_snapshot` (010) | `(snapshot_time, underlying_scrip, expiry_date, strike, option_type)` | `underlying_scrip`, `underlying_seg` |
| `option_atm_iv` (009) | `(symbol, expiry_date, time)` | `symbol` |
| `expiry_calendar` (009) | `(symbol, expiry_date)` | `symbol` |
| `futures_bars` (009) | `(symbol, timeframe, time)` | `symbol` |
| `fno_instruments` (010) | `(security_id)` | `underlying_symbol`, `segment`, `exch_id` — **already holds all FUT/OPT rows for every underlying**, BSE included |

**=> No schema migration is required for multi-index option/IV/expiry/bar data.** The only hard-coding
to remove lives in three Python module-level dicts and the collector defaults:

- `core/fno_backfill.py`: `SYMBOL_SCRIP = {"NIFTY": 13}`, `INDEX_SECURITY_IDS = {...}`,
  `SYMBOL_STRIKE_STEP = {"NIFTY": 50}`, and `nifty_atm_strike()` (already generic — takes `step`).
- `core/fno_collector.py`: hard-coded `nifty_id="13"`, `vix_id="21"`, single-symbol run, and the
  realized-vol recompute pinned to `"13"`.

---

## 2. Per-index Dhan API registry

Dhan's option-chain endpoints take `UnderlyingScrip` (the IDX_I / BSE feed id of the **underlying index**,
NOT the option contract) and `UnderlyingSeg`. NSE indices use `IDX_I`; BSE indices use `BSE` (segment for
SENSEX/BANKEX underlyings in Dhan v2). `charts/historical` for the spot index uses the same id with the
matching index segment.

> **Scrip ids below must be CONFIRMED on the trusted machine** before any run — they are the well-known /
> documented Dhan values but the authoritative source is the detailed scrip master we already ingest
> (`fno_instruments`). See §2.1 for the confirmation query. Treat the table as the registry seed, not gospel.

| Index | Exch | `UnderlyingScrip` (expected) | `UnderlyingSeg` (chain) | Index-bars segment | Strike step ₹ | Lot size* | Expiry day* | VIX? |
|---|---|---|---|---|---|---|---|---|
| NIFTY | NSE | 13 | `IDX_I` | `IDX_I` | 50 | 75 | Tue (current) | ✅ India VIX (21) |
| BANKNIFTY | NSE | 25 | `IDX_I` | `IDX_I` | 100 | 35 | (monthly only since 2024) | ❌ |
| FINNIFTY | NSE | 27 | `IDX_I` | `IDX_I` | 50 | 65 | Tue | ❌ |
| MIDCPNIFTY | NSE | 442 | `IDX_I` | `IDX_I` | 25 | 140 | Mon | ❌ |
| SENSEX | BSE | 51 | `BSE` (BSE_FNO for contracts) | `BSE` / `IDX_I`† | 100 | 20 | Fri/Tue‡ | ❌ |
| BANKEX | BSE | 52 | `BSE` | `BSE` / `IDX_I`† | 100 | 30 | (varies) | ❌ |

\* Lot size, strike step, and expiry weekday **drift** (SEBI changed expiry weekdays and lot sizes through
2024–25; weekly expiries for non-NIFTY indices were largely discontinued in Nov 2024 → mostly monthly now).
**Do not hard-code these as truth — derive lot_size + step from `fno_instruments` (the scrip master) at
runtime; derive expiry weekday from the actual `expiry_calendar` we build.** The table values are seeds /
sanity checks only. This mirrors the existing `classify_expiry()` note ("never assume a fixed day").

† BSE index spot bars: confirm the working segment on the trusted box — Dhan has historically accepted
`IDX_I` for some BSE index feed ids and `BSE` for others. Try `BSE` first, fall back to `IDX_I`; record
whichever returns rows in the registry. (This is the single most likely live blocker — see §6.)

‡ SENSEX weekly expiry moved Fri→Tue then was reduced; rely on `expiry_calendar`.

### 2.1 Confirm scrip ids from the data we already have

```sql
-- Underlying ids actually referenced by Dhan's FUT/OPT contracts (authoritative)
SELECT underlying_symbol, segment, exch_id,
       MIN(underlying_security_id) AS underlying_id,
       MIN(lot_size) AS lot, COUNT(*) AS n
FROM fno_instruments
WHERE underlying_symbol IN
      ('BANKNIFTY','FINNIFTY','MIDCPNIFTY','SENSEX','BANKEX','NIFTY')
GROUP BY underlying_symbol, segment, exch_id
ORDER BY underlying_symbol;
```
NOTE (010 comment): `fno_instruments.underlying_security_id` is a **distinct id-space** from the
option-chain `UnderlyingScrip` (NIFTY = 26000 vs 13). So this query confirms **lot size, segment, and that
contracts exist**, and gives the *contract-master* underlying id; the **option-chain `UnderlyingScrip`**
(13/25/27/442/51/52) must be validated by actually calling `get_fno_expiry_list(scrip, seg)` per index and
checking it returns expiries. The registry stores BOTH ids explicitly.

---

## 3. Available-vs-forward-only matrix

This is the crux. Per the foundation (`get_fno_option_chain` docstring: *"LIVE SNAPSHOT ONLY — Dhan exposes
no historical option-chain/IV"*), the availability is **identical to NIFTY** for every new index:

| Data product | Source endpoint | NIFTY today | New indices | Backfillable? |
|---|---|---|---|---|
| **Spot index OHLCV** (`index_bars`) | `charts/historical` (IDX_I/BSE) | history | history | ✅ **YES — backfill multi-year** (subject to BSE-segment confirmation, †) |
| **Index futures OHLCV+OI** (`futures_bars`) | `charts/historical` (NSE_FNO/BSE_FNO, FUTIDX) | history | history (per-contract) | ✅ YES — but per-contract roll caveat (Open Q#2) applies to all |
| **Option chain snapshot** (`option_chain_snapshot`) | `optionchain` (live) | forward-only | forward-only | ❌ **NO — FORWARD-ONLY**, must collect daily going forward |
| **ATM IV** (`option_atm_iv`) | derived from live chain | forward-only | forward-only | ❌ **NO — FORWARD-ONLY** |
| **Expiry calendar** (`expiry_calendar`) | `optionchain/expirylist` (live) | forward (current set) | forward (current set) | ⚠️ current+future expiries only; no historical expiry archaeology |
| **Realized vol** (`realized_vol_20d`) | derived from `index_bars` | computable over history | computable over history | ✅ YES (follows index_bars) |
| **Implied vol via VIX** | India VIX `index_bars` | history (id 21) | **NONE except NIFTY** | ❌ no per-index vol index |

**Bottom line:**
- **Index/futures bars + realized vol = BACKFILLABLE** for all five (multi-year history). This lets the
  orchestrator's *realized-vol regime + trend + DTE* signals work on day one for the new indices.
- **Option chains + ATM IV + VRP = FORWARD-ONLY** for all five. The new indices accrue real IV only from
  the day the collector starts snapshotting them. Until then they have **no IV-rank / VRP / vol-gate-via-IV**
  history → the orchestrator must treat them as "IV-cold" (see §5).
- This matches NIFTY's own honesty caveat (`_CONTEXT.md` #2: real-IV forward paper-log is the truth test).
  The new indices are simply earlier on the same forward-only clock.

---

## 4. Code changes

### 4.1 Introduce an index registry (single source of truth)

New module-level structure (place in `core/fno_backfill.py`, or a tiny new `core/fno_indices.py` imported by
both backfill + collector — preferred, keeps registry separate from I/O):

```python
# core/fno_indices.py  (NEW — pure data, no I/O, unit-testable)
from dataclasses import dataclass

@dataclass(frozen=True)
class IndexSpec:
    symbol: str            # logical name, key for option_atm_iv / expiry_calendar / *_bars.symbol
    underlying_scrip: int  # option-chain UnderlyingScrip (13, 25, 27, 442, 51, 52)
    underlying_seg: str    # "IDX_I" (NSE) | "BSE"
    index_security_id: str # IDX_I/BSE feed id for spot charts/historical (often == scrip, as str)
    index_segment: str     # exchange_segment for spot bars: "IDX_I" | "BSE"
    fno_segment: str       # contract segment: "NSE_FNO" | "BSE_FNO"
    strike_step: int       # SEED only — prefer scrip-master-derived
    vix_security_id: str | None  # "21" for NIFTY, None otherwise

INDEX_REGISTRY: dict[str, IndexSpec] = {
    "NIFTY":      IndexSpec("NIFTY",      13,  "IDX_I", "13",  "IDX_I", "NSE_FNO", 50,  "21"),
    "BANKNIFTY":  IndexSpec("BANKNIFTY",  25,  "IDX_I", "25",  "IDX_I", "NSE_FNO", 100, None),
    "FINNIFTY":   IndexSpec("FINNIFTY",   27,  "IDX_I", "27",  "IDX_I", "NSE_FNO", 50,  None),
    "MIDCPNIFTY": IndexSpec("MIDCPNIFTY", 442, "IDX_I", "442", "IDX_I", "NSE_FNO", 25,  None),
    "SENSEX":     IndexSpec("SENSEX",     51,  "BSE",   "51",  "BSE",   "BSE_FNO", 100, None),
    "BANKEX":     IndexSpec("BANKEX",     52,  "BSE",   "52",  "BSE",   "BSE_FNO", 100, None),
}
```
- Keep the **legacy dicts** (`SYMBOL_SCRIP`, `SYMBOL_STRIKE_STEP`, `INDEX_SECURITY_IDS`) as thin views
  derived from the registry so existing tests / callers don't break, then migrate them off.
- `nifty_atm_strike()` is already step-parameterised — rename to `atm_strike()` (keep an alias) and pass
  `spec.strike_step`. `extract_atm_iv` already takes `step`.

### 4.2 `core/fno_backfill.py`

- `snapshot_option_chain` / `build_expiry_calendar` already accept `underlying_scrip` + `underlying_seg`
  overrides and `step` flows through — wire them from `INDEX_REGISTRY[symbol]` instead of the NIFTY dicts.
  Remove the `SYMBOL_SCRIP[symbol]` KeyError path (registry lookup with a clear error for unknown symbol).
- `backfill_index_bars` already takes `security_id` + `symbol`; pass `exchange_segment` from
  `spec.index_segment` (currently hard-coded `"IDX_I"` inside the function → parameterise it; default IDX_I).
- `backfill_futures_bars` already takes `exchange_segment`? — it hard-codes `"NSE_FNO"`. Parameterise to
  `spec.fno_segment` so BSE futures work.
- CLI: add `--symbol` choices from the registry; loop helpers `--all-indices`.

### 4.3 `core/fno_collector.py`

- `run_eod_collection` currently takes one `symbol` + `nifty_id` + `vix_id`. Refactor to accept a
  **list of symbols** (default = full registry) and, per symbol, resolve ids from `INDEX_REGISTRY`:
  - index bars (spot) for `spec.index_security_id` via `spec.index_segment`
  - VIX bars **only when `spec.vix_security_id` is not None** (NIFTY only) — skip the VIX step otherwise
  - expiry calendar + option-chain snapshots (nearest `n_expiries`) per symbol
- Keep the per-step `_step` isolation; one index failing must not abort the others. Aggregate the summary
  per symbol (`{symbol: {...}}`).
- Realized-vol recompute: loop `compute_index_realized_vol(spec.index_security_id)` for every index, not
  just `"13"`.
- Paper-log step: gate per-index — only run `record_paper_entry`/`resolve_paper_trades` for indices that
  have enough forward IV history (NIFTY now; others once warm). Make symbol a parameter; default NIFTY-only
  until the new indices accrue IV (see §5 + Blocker B4).

### 4.4 `ml/fno_vol_gate.py` — the VIX gap (most important non-mechanical change)

The gate's default `source="vix"` joins `index_bars` realized vol against **India VIX** (`vix_id="21"`).
**Only NIFTY has a vol index.** For the other five there is no VIX feed, so:

1. **Realized-vol fallback path.** `samples_from_db` already supports a non-VIX realized path
   (it has a `source` switch + the gate interface is "unchanged, only the implied source changes").
   For non-NIFTY indices the **implied** baseline must come from the **collected ATM straddle IV**
   (`option_atm_iv.straddle_iv`) rather than VIX — i.e. add/confirm a `source="atm_iv"` path that joins
   `realized_vol_20d` (from `index_bars`) against `option_atm_iv.straddle_iv` per `symbol`.
2. This means **the gate cannot run on a new index until that index has forward ATM-IV history** (it is
   IV-cold). Until warm: either stand the index aside in the orchestrator, or run a realized-only regime
   proxy (no VRP). Document as a known limitation — do NOT fabricate a synthetic VIX.
3. `k≈0.9` was calibrated on NIFTY VIX-vs-realized; **re-calibrate per index** once each has ≥ the
   re-arm sample threshold of ATM-IV-vs-realized pairs. Store `k` per symbol (registry or a small config),
   never share NIFTY's k blindly.

### 4.5 Tests

- Pure-registry unit tests (no creds/DB): registry completeness, `atm_strike` per step, vix-gap handling
  (BANKNIFTY → no VIX step), collector summary keyed per symbol with an injected fake `now` + stub client.
- Reuse the existing fake-`now` + stub-client patterns already in the fno tests.

---

## 5. Orchestrator-facing consequence (IV-cold indices)

The router (`_CONTEXT.md` selection layer) must know, per index, whether IV-derived signals are available:

- **NIFTY:** full (realized + VIX + collected ATM IV + history) → all signals.
- **NSE new (BANKNIFTY/FINNIFTY/MIDCPNIFTY):** realized-vol + trend + DTE from day one (backfilled bars);
  **VRP / IV-rank / vol-gate only after forward ATM-IV warms up** (weeks of daily snapshots). No VIX ever.
- **BSE (SENSEX/BANKEX):** same as NSE-new, plus the BSE-segment data risk (§6 B1) must clear first.

Recommend the orchestrator carry an explicit per-index `iv_warm: bool` (e.g. ≥ N forward ATM-IV samples,
reuse the n≥30 re-arm convention) and stand aside / fall back to realized-only regime for cold indices.

---

## 6. Cron & rate-limit considerations

- **Existing cron:** EOD collector runs post-close on the trusted machine (the `*45` IST job referenced in
  CLAUDE.md / `docs/fno-revalidation-plan.md`). One process, NIFTY only.
- **New load:** 6 indices × (1 expiry-list + `n_expiries` chain calls + 1–2 historical bar calls).
  With `n_expiries=4` that's ~6×6 ≈ **36 chain calls + ~12 history calls** per run.
- **`optionchain` is rate-limited to 1 request / 3 s** (client + `get_fno_option_chain` docstring). 36 chain
  calls ⇒ **≥ ~110 s** just spacing the chain pulls; plan the collector to **serialise across indices with
  ≥3 s spacing** (do NOT parallelise the chain endpoint). Total run a few minutes — fine post-close.
- **`charts/historical`** tolerates more but space defensively (~1 req/s, per CLAUDE.md DH-904 note for
  intraday; daily is lighter). Keep history calls sequential.
- **100K calls/day budget** is untroubled (tens of calls/day here). The constraint is the 3-s/req chain
  throttle, not the daily cap. Route all calls through `core/client.py`'s rate limiter; flush API-usage
  deltas as the trader/backfill already do (`core/api_usage.py`).
- **Keep ONE cron**, looping the registry sequentially (simpler, respects the shared 3-s throttle) rather
  than parallel per-index crons that would collide on the throttle. Off-hours guard (`_assert_off_hours`)
  stays in force.
- **Forward-only ⇒ never miss a day.** Each missed post-close run is permanently lost IV (no backfill).
  Add a monitor/alert (reuse `scripts/health_alert.py` pattern + Telegram via `core/notify.py`) so a failed
  collector run for any index is noticed same-day. The index-bars lookback window self-heals; **IV does
  not.**

---

## 7. Blockers / open questions (flag before running)

- **B1 (highest):** BSE index spot-bar segment for SENSEX/BANKEX (`IDX_I` vs `BSE`) and whether
  `charts/historical` returns rows for BSE index feed ids — **must be probed live on the trusted box**.
  If unavailable, BSE indices get option chains forward-only but possibly no spot-bar history → no
  realized-vol regime → orchestrator can't trade them until resolved. Probe first.
- **B2:** Confirm option-chain `UnderlyingScrip` ids (25/27/442/51/52) by calling `get_fno_expiry_list`
  per index — the contract-master underlying id ≠ chain scrip id (010 comment). Registry stores both.
- **B3:** Weekly expiries for non-NIFTY indices were largely discontinued (Nov 2024) → most are
  **monthly-only**. The orchestrator's weekly-cycle assumptions (NIFTY-derived) must adapt to monthly DTE
  for these. Driven by the real `expiry_calendar`, not assumed.
- **B4:** New indices are **IV-cold** at launch — no VRP/IV-rank/vol-gate history. They contribute
  realized-vol-only regime signals until forward ATM IV warms (weeks). Don't promote them to live-style
  selection until warm + per-index `k` re-calibrated.
- **B5:** Per-contract futures roll caveat (Open Q#2, `docs/fno-handoff.md`) applies to every index — store
  distinct per-contract symbols (e.g. `BANKNIFTY-2406`) to avoid PK collisions / realized-vol contamination
  across rolls.
- **B6:** Lot sizes / strike steps drift — derive from `fno_instruments` at runtime; treat the §2 table as
  seeds. Re-sync the scrip master (`sync_fno_instruments`) before each registry-driven run.

---

## 8. Deliverable summary

1. `core/fno_indices.py` — `IndexSpec` + `INDEX_REGISTRY` (6 indices), pure/testable.
2. `core/fno_backfill.py` — registry-driven scrip/seg/step; parameterise hard-coded `IDX_I`/`NSE_FNO`.
3. `core/fno_collector.py` — multi-symbol loop, VIX step conditional on `spec.vix_security_id`, per-index
   realized-vol recompute, per-symbol summary, per-index paper-log gating.
4. `ml/fno_vol_gate.py` — `source="atm_iv"` per-symbol implied baseline (VIX fallback for non-NIFTY);
   per-index `k`.
5. One sequential off-hours cron over the registry (respect 3-s `optionchain` throttle) + missed-run alert.
6. Tests: registry + vix-gap + multi-symbol collector (fake-now/stub-client).
**No Alembic migration needed** — schema is already symbol/scrip/security_id-keyed.

All of the above is **trusted-machine, off-hours, read-only, PAPER**. The PR lane writes the code; the
trusted machine confirms ids (B1/B2) and runs ingestion.
