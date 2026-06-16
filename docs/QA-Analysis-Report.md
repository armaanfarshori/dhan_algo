# QA Analysis Report — Tessera (`dhan_algo`)

**Date:** 2026-06-15
**Scope:** Full codebase QA sweep (apps, engine, core, ml, strategies, research, tests)
**Method:** Evidence-based source review against the actual code (aiohttp backend, JSX frontend, raw SQLAlchemy, TimescaleDB)

> ✅ **STATUS UPDATE (2026-06-16): all risk-register findings (C1, C2, H1–H4, M1–M6) are RESOLVED.** The suite is at **195 passed, 0 xfailed**. See §8 (Remediation status) for the per-finding detail. The "Severity note" and amendments immediately below are the *original* review framing, kept for history. A new **Time Rendering & Timestamp Analysis** section (findings T1–T8, two of them High) is appended at the end.

> 🔁 **NEW — Re-Review (2026-06-16 EOD), after the full Tessera dashboard redesign:** a **16-agent parallel audit + Opus-led UI/UX testing** surfaced **~160 findings** (a fresh set, IDs `QR-*`), concentrated in the new React dashboard and its contract with the engine. Highlights: a kill-switch flatten-path gap, an API-usage double-count on shutdown, a cluster of **API↔dashboard field-name mismatches** (gate pill shows "live" in shadow; kill-switch indicator stuck "armed"; profit-factor "n/a"), broken financial metrics (drawdown computed on reversed order → 1123%, unstable Sharpe 11.61), WCAG-AA contrast failures on all small labels, and the dashboard being **absent from CI**. See **§9 — Re-Review** at the end. *These are newly-found and NOT yet remediated.*

> Severity note: several findings (C1, H3, H4, M6) bite only in **live mode**, which the platform is deliberately not in yet — treat them as "fix before M8 (tiny live)," not "fix tonight." C2, H1, and H2 can affect paper/backfill today.

> **Amendment (2026-06-15, post-test):** Two corrections after writing the test suite.
> 1. **C2 was overstated.** `log_trade_exit` *does* carry an order-id guard
>    (`AND (:order_id IS NULL OR dhan_order_id=:order_id)`), so the close-all only
>    fires when the exit has **no order_id** — i.e. the PAPER path. In LIVE, an
>    order-id-scoped exit instead closes only its own row, leaving sibling rows
>    from a multi-entry position **OPEN forever**. Both behaviors are real; the
>    earlier "no order-id match" wording was wrong.
> 2. **C2 and H3 are now FIXED** (commit on 2026-06-15) — see "Remediation status"
>    at the end of this report. Their xfail tests were flipped to passing.
>    Residual trade-model limitations (partial-reduce marks a trade CLOSED early;
>    multi-entry P&L apportionment) are documented there and tracked.

---

## 1. Executive Summary

Tessera is a paper-mode-default intraday NSE trading platform with a genuinely clean post-rewrite architecture: pure strategy logic, a swappable executor abstraction, DB-persisted state, and disciplined separation between the trading process and the read-only dashboard. Engineering hygiene is above average for a solo trading project — 85 fast unit tests, pure-function stats, and strong safety defaults (paper-first, single kill-switch owner, fail-open gate).

However, **the test suite covers the safe, pure parts of the system and almost none of the dangerous, side-effecting parts.** The modules that place real orders, refresh auth tokens, and write trade records — the ones that move money or corrupt state — have effectively zero direct tests, and two of them contain latent correctness bugs that would only fire in live mode or under multi-fill conditions. The system is well-built for the paths it exercises in paper mode and under-defended on the paths it will hit the first time real capital and network turbulence are involved.

**Top 5 highest risks:**

1. **Duplicate live orders on network turbulence** — `DhanClient._request` retries POST `orders` on timeouts and 429/5xx, and `LiveExecutor` sends no `correlation_id`, so there is no idempotency key. A lost response after a successful placement places the order twice. (`core/client.py:142-183`, `engine/execution.py:77-90`)
2. **Trade-table P&L corruption from `log_trade_exit`** — it closes *every* open row for a security with one `UPDATE ... WHERE security_id AND status='OPEN'`, no order-id match, no `LIMIT`. Any multi-entry or flip scenario double-counts P&L — and the risk engine's daily-loss halt now reads from this very table. (`core/journal.py:232-236`)
3. **Non-atomic `.env` rewrite on every token refresh** — a regex substitution with no temp-and-rename; a kill mid-write corrupts `.env`, and the next boot has no DB credentials. The "only one process refreshes" lock described in the docstring is not implemented. (`core/token_manager.py:77-93`, `:40`)
4. **Daily/hourly/minute rate limits are defined but unenforced** — only `per_sec` is applied, so the 100k/day data and 7k/day order caps can be silently blown, contradicting the documented guarantee. (`core/client.py:35-45`)
5. **Zero tests on the money/IO paths** — no tests for client retry/idempotency, live-feed reconnect, the trade lifecycle, or any API endpoint. The two correctness bugs above are untested and invisible to CI.

---

## 2. Repository Overview

Intraday Opening-Range-Breakout (ORB) strategy on dynamically-screened NSE equities, with a Kronos OHLCV foundation model as a shadow-mode gate, on TimescaleDB. Two processes (`apps/trader.py` = order flow, `apps/api.py` = aiohttp dashboard) share only the DB and a heartbeat file. Stats are concentrated in pure modules (`strategies/orb.py`, `ml/calibration.py`, `research/backtest/`).

Tech: Python/asyncio, **aiohttp** (not FastAPI), raw SQLAlchemy `text()`, React 18 JSX, PyTorch/HF. ~6,300 LOC of app code, 85 tests.

Architecture strengths worth preserving:
- Pure, IO-free strategy (`strategies/orb.py`) shared identically by live runner and backtester.
- Executor abstraction (`engine/execution.py`) — paper/live differ in exactly one place.
- DB-persisted portfolio with boot + broker reconciliation (`engine/portfolio.py`).
- Single kill-switch owner; fail-open gate; paper-first hard default.

---

## 3. Identified Use Cases

1. Token lifecycle — generate/renew/refresh, cross-process sharing (`core/token_manager.py`)
2. Historical backfill (`backfill.py`)
3. Live market-data ingestion — feed → bars (`core/live_feed.py`, `engine/bar_builder.py`)
4. Watchlist screening + fallback (`core/nse_screener.py`, `core/watchlist.py`)
5. Strategy decisioning — OR build/lock/seed/exit (`strategies/orb.py`, `engine/runner.py`)
6. Kronos gate + calibration (`ml/kronos_gate.py`, `ml/calibration.py`)
7. Order execution — paper/live (`engine/execution.py`)
8. Portfolio state & reconciliation (`engine/portfolio.py`)
9. Risk management — sizing, budgets, halts, kill-switch (`engine/risk.py`)
10. Dashboard API — read endpoints, mode, kill-switch, postback (`apps/api.py`)
11. Backtester (`research/backtest/`)

---

## 4. Detailed Analysis (selected high-signal use cases)

### UC7 — Order execution (`engine/execution.py`)
Happy path is sound: `PaperExecutor` applies adverse slippage and journals identically to live; `LiveExecutor._confirm_fill` polls `get_order_by_id` and books the broker's actual average price (`:133-140`), correctly returns `None` on REJECTED/CANCELLED (`:141-145`).

Failure-path concerns:
- The **unconfirmed-after-~8s fallback books a phantom fill at the reference price** (`:148-152`) — flagged CRITICAL and reconciled, but reconcile only runs on boot, so a rejected-late order leaves a tracked-but-nonexistent position for the rest of the session.
- The **duplicate-order risk** from the retry layer below it (Risk C1).
- No partial-fill modeling in paper, so paper P&L is optimistic on size.

### UC1 — Token lifecycle (`core/token_manager.py`)
`_write_token` rewrites `.env` via `re.sub` with no atomic replace (`:86-93`); the lock file declared at `:40` is never used. `handle_auth_error` (`:209`) has no guard against concurrent invocation — several in-flight requests hitting DH-901 simultaneously (`client.py:157-161`) each trigger a token generation and a racing `.env` rewrite. Token-file reads/writes are unsynchronized across the trader and backfill (mitigated only by `read_current_token` swallowing parse errors).

### UC8 — Portfolio (`engine/portfolio.py`)
`apply_fill` weighted-average and realized-P&L math is correct for open/add/reduce/close. The **flip branch is the gap**: when a fill crosses zero (`:104-105`), it sets the remainder's avg to the fill price and calls only `log_trade_exit` — the newly-opened opposite position is never recorded via `log_trade_entry`, so the trades table loses it. ORB doesn't flip directly today (it exits then re-enters as separate fills), so this is latent, not active. `reconcile_with_broker` has a `float(None)` hazard at `:191-192` if a broker row has `netQty>0` but only `sellAvg` populated.

### UC3 — Market data (`engine/bar_builder.py`)
`on_tick` stamps bars with `datetime.now(IST)` (`:66`), i.e. server receive time, not the exchange tick timestamp — so clock drift or a GC pause mis-buckets a bar, and these bars feed both Kronos and the backtest. Cumulative-volume delta handling is correct including resets (`:69-71`). The re-queue on flush failure (`:129`) is correct but `_pending` is unbounded under a prolonged DB outage.

### UC10 — Dashboard API (`apps/api.py`)
Mode POST correctly refuses with 409 (`:280-290`) — good. `killswitch_handler` (`:293-298`) and every read endpoint are unauthenticated (known, gated on M6; tunnel-only mitigates). `postback_handler` (`:775-783`) only logs — the authoritative broker fill notification is **not** wired into reconciliation, and it has no signature verification.

---

## 5. Risk Register

| ID | Sev | Location | Risk | Impact |
|----|-----|----------|------|--------|
| C1 | **Critical** | `core/client.py:142-183`, `place_order:224`, `execution.py:79-86` | POST `orders` retried on network error/429/5xx with empty `correlation_id` (no idempotency) | Duplicate live order → double position & double risk |
| C2 | **Critical** *(FIXED)* | `core/journal.py:232-236` | `log_trade_exit` closed every OPEN row for a security when the exit had no order_id (paper path); order-id-scoped exits left multi-entry siblings OPEN | P&L double-count → corrupts trades table that feeds the risk daily-loss halt. **Fixed:** exit now closes exactly one (latest matching) open row |
| H1 | High | `core/client.py:35-45` | Only `per_sec` enforced; per_min/hour/day defined but ignored | 100k/day data & 7k/day order caps can be blown; contradicts documented guarantee |
| H2 | High *(FIXED)* | `core/token_manager.py:77-93` | Non-atomic `.env` rewrite on every token refresh | Corrupted `.env` → boot fails (no DB creds). **Fixed:** the rotating token is now written only to dhan_token.json, atomically (temp + os.replace); `.env` is never rewritten. (M1 concurrent-refresh race still open.) |
| H3 | High *(FIXED)* | `engine/portfolio.py:95-109` | Flip recorded only the closing exit, not the new opposite entry | Untracked position in trades table; mis-stated history/P&L. **Fixed:** flip now journals exit + new entry |
| H4 | High | `engine/execution.py:148-152` | Unconfirmed order booked at ref price; reconcile only on boot | Phantom position until next restart if order later rejects |
| M1 | Med | `core/client.py:157-161` + token mgr | Concurrent DH-901 handling races token generation & `.env` writes | Wasted token gen, transient auth instability |
| M2 | Med | `engine/bar_builder.py:66` | Bars stamped with server clock, not exchange LTT | Mis-bucketed bars corrupt Kronos input & backtest |
| M3 | Med | `engine/portfolio.py:191-192` | `float(None)` on partial broker avg fields | Exception aborts LIVE broker reconcile |
| M4 | Med | `engine/bar_builder.py:129` | `_pending`/`_last_cum_vol` unbounded | Memory growth on prolonged DB outage |
| M5 | Med | `tests/` | No tests for client retry, live_feed reconnect, journal lifecycle, API | Critical bugs invisible to CI |
| M6 | Med | `apps/api.py:775` | Postback is a no-op, unauthenticated, not reconciled | Authoritative fill data discarded; injectable |
| L1 | Low | `engine/execution.py:39` | Paper has no partial fills / liquidity model | Optimistic paper P&L on size |
| L2 | Low | `core/token_manager.py:44` | Token-file read/write has no file lock | Backfill may read partial JSON (caught → None) |
| L3 | Low | `engine/bar_builder.py:96` | Bars can be built off the feed outside RTH | Minor data hygiene (pre-open ticks) |
| L4 | Low | heartbeat | `portfolio.realized_pnl` (process-local) vs `risk.realised_pnl` (DB-today) differ | UI shows two different "realized" numbers |

---

## 6. Test Strategy & Recommendations

Coverage today is strong on pure logic (ORB 14, risk 16, calibration 9, portfolio 6, executors via mock) and **absent on IO paths**. Highest-value tests to write first, in order:

1. **`client._request` order idempotency/retry** (mock `aiohttp` session): assert a POST `orders` is *not* silently retried on an ambiguous failure, or that a `correlation_id` is always present. Directly targets C1.
2. **Journal trade lifecycle**: two entries then one exit → assert exactly one close and correct pnl; a flip → assert both an exit and a new entry are recorded. Targets C2/H3.
3. **`live_feed` reconnect**: assert exponential backoff schedule, 30s floor on 429, and that the prior socket is closed before reconnect (the fix shipped this week is untested).
4. **Risk daily-loss-from-DB + open-risk budget under concurrent entries** (integration with a test DB).
5. **API smoke tests** via `aiohttp` test client: `/api/snapshot` shape, mode POST → 409, kill-switch writes the flag file, `/api/kronos/signals` is today-only.

Testability is limited by direct `get_session()`/`get_engine()` calls inside functions and no DB/HTTP fixtures. Introducing a session-factory injection point and a SQLite-or-Testcontainers fixture would unlock the entire integration tier.

---

## 7. Actionable Improvements

- **Make live orders idempotent**: generate a `correlation_id` per intent in `LiveExecutor`, and treat the `orders` category as non-retriable on ambiguous failures (or dedupe via order-book lookup before re-placing).
- **Fix trade-exit matching**: close by `dhan_order_id` or enforce a one-open-row-per-security invariant; record the new entry on a flip.
- **Enforce the windowed rate limits** in `RateLimiter` (or explicitly document that backfill self-paces and remove the unused limit fields).
- **Atomic writes** for `.env` and `dhan_token.json` (`tmp` + `os.replace`); implement the documented refresh lock or delete the claim; serialize `handle_auth_error`.
- **Wire or remove the postback**: either reconcile fills from it (with signature verification) or drop the endpoint so it isn't mistaken for a safety net.
- **Stamp bars with exchange LTT** when present; bound `_pending`.
- **Unify the heartbeat P&L number** so the dashboard shows one consistent realized figure.

---

## 8. Remediation status

**ALL risk-register findings are now resolved (2026-06-16).** Every `xfail(strict=True)` marker
has been flipped to a passing test — the suite has **zero xfails**. (This happened across the
broader SDLC remediation sprint; C1/H1/M1 fixed mid-sprint, M2/M3/M4/M6 fixed last.)

| ID | Status | Test | Notes |
|----|--------|------|-------|
| C1 | ✅ Fixed | `test_client_idempotency.py` | `place_order` auto-generates a `correlation_id` and reconciles via `get_order_by_correlation_id` on transient failure; order POSTs are non-retriable → no double-fill |
| C2 | ✅ Fixed | `test_journal_lifecycle.py` | `log_trade_exit` closes exactly one (latest matching) open row — no blanket close, no P&L double-count (now also exact via `open_trade_id`, DATA-03) |
| H1 | ✅ Fixed | `test_client_idempotency.py` | windowed rate limits enforced — sec/min throttle, hour/day raise `RateLimitExceeded` |
| H2 | ✅ Fixed | `test_token_safety.py` | token cached atomically (os.replace); `.env` never rewritten by the app |
| H3 | ✅ Fixed | `test_journal_lifecycle.py` | flip now journals exit + a new entry for the opposite position |
| H4 | ✅ pinned | `test_execution_robustness.py` | unconfirmed-fill behavior covered by passing tests; broker reconcile-on-boot corrects drift (live-only) |
| M1 | ✅ Fixed | `test_client_idempotency.py` | token refresh serialized behind `MasterTokenManager._refresh_lock` (double-check) |
| M2 | ✅ Fixed | `test_bar_builder*` / `test_live_feed.py` | LiveFeed now forwards the exchange LTT (present in the Dhan Quote packet) to BarBuilder/CandleBuilder so bars bucket by exchange time, not server clock (falls back to `now()` if absent) |
| M3 | ✅ Fixed | `test_reconcile_partial_avg.py` | reconcile falls back primary avg → costPrice → other avg → 0; never `float(None)` |
| M4 | ✅ Fixed | `test_bar_builder_robustness.py` | `_pending` capped at 500 on sustained flush failure (drop oldest, warn) |
| M5 | ✅ Fixed | `tests/` | added LiveFeed / screener / kronos_gate / runner / risk._evaluate tests (+ many more); suite **71 → 195** |
| M6 | ✅ Fixed | `test_api_handlers.py` | `/postback` is HMAC-verified (SEC-09) and `_reconcile_postback` persists the authoritative fill to the `journal` table (no longer discarded). Full cross-process reconcile — the trader adopting the fill — remains the M8 follow-up |

**Residual trade-model limitations** (tracked, not yet addressed): a *partial* reduce still marks a trade row CLOSED even though quantity remains, and multi-entry positions are not P&L-apportioned across rows. Neither occurs with the current ORB strategy (one entry, one full exit per position), but a future adding/scaling strategy would need the trades table to model position lifecycle rather than individual fills.

Suite: **195 passed, 0 xfailed** (CI on Py3.11 × x86+ARM, ruff, coverage gate; CodeQL + Dependabot clean).

---

*Generated 2026-06-15. References are `file:line` against the commit current at generation time; verify line numbers before acting as they drift with edits.*

---

## Time Rendering & Timestamp Analysis

> ✅ **ALL findings resolved & deployed (2026-06-16).** T1 (IST trading-day queries),
> T2 (`fmtTime` IST), T3 (PnL chart x-axis + CalendarPnL IST; dead `utils.js` removed),
> T4 (UTC log timestamps via `time.gmtime`), T5 (tz-aware risk datetimes), T6 (out-of-order
> bar-tick guard + test). T7/T8 (Low) were subsumed by T2/T3/T4. The findings below are the
> original analysis, kept as the record of what was found.

**Scope:** All time-handling and rendering code.
**Date audited:** 2026-06-16.
**Evidence:** every claim grounded in `file:line` below.

### Overview

The system uses two clocks — UTC for storage (PostgreSQL/TimescaleDB, heartbeat JSON) and IST (Asia/Kolkata = UTC+5:30 fixed, no DST) for trading logic — which is architecturally sound. The split is mostly respected, but a handful of spots conflate the two or make assumptions that will bite at specific clock boundaries.

---

### 1. Timezone Handling (UTC vs IST)

#### Storage layer
All hypertable `time` columns are `TIMESTAMP WITH TIME ZONE` (`TIMESTAMP(timezone=True)` in SQLAlchemy — `alembic/versions/001_initial_schema.py:34,48,67,69`, `002_full_schema.py:27,56,133,145,157,179,201`). PostgreSQL stores these as UTC epoch internally; the box running PostgreSQL is UTC, so there is no calendar offset between wall-clock and storage. `session_date` on `engine_positions` is a plain `DATE` (`004_engine_positions.py:23`) — see risk discussion below.

#### Trading logic
IST is used consistently and correctly via `ZoneInfo("Asia/Kolkata")` in:
- `core/live_feed.py:29` — tick timestamps
- `engine/bar_builder.py:22` — bar bucketing (`datetime.now(IST)`)
- `engine/runner.py:28` — `_in_window` market-hours gate
- `engine/portfolio.py:22` — `session_date = datetime.now(IST).date()`
- `apps/trader.py` — `seed_opening_ranges` via `from engine.runner import IST`

All are `ZoneInfo`-aware objects; no `pytz` or naive datetimes in trading paths.

#### UTC-box + IST-market P&L boundary — **High risk**

`engine/risk.py:117-122` uses `CURRENT_DATE` and `date_trunc('week', CURRENT_DATE)` inside a PostgreSQL query to bucket realized P&L:

```sql
COALESCE(SUM(pnl) FILTER (WHERE exit_ts >= CURRENT_DATE), 0),
COALESCE(SUM(pnl) FILTER (WHERE exit_ts >= date_trunc('week', CURRENT_DATE)), 0)
```

`CURRENT_DATE` on a UTC box resolves to the UTC calendar date. The NSE session ends at 15:30 IST = 10:00 UTC. Any trade closed between **10:01 UTC and 23:59 UTC** (which is 15:31–05:29 IST the next IST calendar day) will be assigned to the *wrong* `CURRENT_DATE` bucket when the query runs after midnight UTC but before midnight IST. In practice the trader runs only during 09:15–15:30 IST, so no live closes happen in that window — but this assumption breaks if:
- A trade is reconciled or closed post-15:30 IST programmatically (e.g. broker-forced close).
- The `equity_curve` snapshot query (`apps/routes/db.py:33`) uses `WHERE time >= CURRENT_DATE` with the same UTC date, so the intraday P&L curve will drop the first 5.5 hours of each IST day (midnight IST = 18:30 UTC previous day) when populated with timestamps from the end of a session.

The same `CURRENT_DATE` issue affects the `signals_handler` and `trades_handler` `CURRENT_DATE` filters (`apps/routes/db.py:65-66, 108, 220, 286`): a trade entered on 09:15 IST (= 03:45 UTC) belongs to the IST "today" but the query filter `entry_ts::date = CURRENT_DATE` also uses the UTC date, so they align. This is coincidentally correct for all Indian market hours (03:45–10:00 UTC is always the same UTC date as IST date). The risk query is the one exception because realized P&L can land at any UTC time when the `refresh_pnl` loop runs (every 10 s even overnight).

**Recommendation:** Replace `WHERE exit_ts >= CURRENT_DATE` in `engine/risk.py:119` with `WHERE exit_ts AT TIME ZONE 'Asia/Kolkata' >= CURRENT_DATE AT TIME ZONE 'Asia/Kolkata'` (or equivalently cast to IST before comparing to an IST date). Apply the same fix to `equity_handler` (`apps/routes/db.py:33`).

#### Hardcoded `+00:00` log offset — **Medium risk**

Both `apps/trader.py:33` and `apps/api.py:36` set:
```python
datefmt="%Y-%m-%dT%H:%M:%S+00:00"
```
This writes `2026-06-16T04:30:01+00:00` literally regardless of what the wall-clock offset actually is. The datefmt only controls the UTC time portion (Python's `%(asctime)s` uses `localtime()` by default), so on an EC2 instance set to UTC the offset literal is correct — but it is a hardcoded assumption. If the box's local timezone were ever changed to IST, the time string would show IST time with a `+00:00` label (i.e. wrong by −5:30). The fix is either `datefmt="%Y-%m-%dT%H:%M:%SZ"` with `logging.Formatter.converter = time.gmtime` or use `%(asctime)s` with a formatter that explicitly formats UTC. Currently `apps/routes/system.py:27` already uses `datetime.now(timezone.utc).date()` to match against log-line prefixes — this is consistent with the UTC assumption and would break the same way if the OS timezone changed.

#### Naive `datetime.now()` calls — **Medium risk**

Two calls in `engine/risk.py` produce naive (no tzinfo) datetimes:
- `risk.py:176`: `"ts": datetime.now().isoformat()` — halt state persisted to `run/halt_state.json`
- `risk.py:321`: `self.state.last_checked = datetime.now()` — heartbeat payload serialized as `.isoformat()` via `get_summary()`

Both use `datetime.now()` without a tz argument, yielding a naive local-time datetime. On a UTC box these are effectively UTC, but they are not comparable with the tz-aware `datetime.now(timezone.utc)` used everywhere else (including in `read_heartbeat()` which does `datetime.fromisoformat(hb["ts"])` and subtracts from `datetime.now(timezone.utc)`). The `last_checked` field does not flow to the staleness comparison, so there is no immediate bug — but it is inconsistent. The halt-state `ts` is display-only. Fix: use `datetime.now(timezone.utc).isoformat()` in both.

---

### 2. Timestamp Precision

#### Bar 1-minute bucket alignment

`engine/bar_builder.py:67`:
```python
minute_start = now.replace(second=0, microsecond=0)
```
This truncates to the start of the current IST minute — correct for 1m bucketing. The same logic is applied in `bar_builder.py:107` in `flush()`. No sub-minute drift possible from this path.

`core/live_feed.py:52`:
```python
minute = now.hour * 60 + now.minute
```
`CandleBuilder` uses an integer minute-of-day rather than a truncated datetime. Comparing integers is cheaper but slightly different: `BarBuilder` compares `bar.minute_start != minute_start` (datetime equality) while `CandleBuilder` compares the integer minute number. Both are correct for minute boundaries. However, `CandleBuilder` is only used when no `BarBuilder` is wired (unit tests / lightweight callers); in the normal live path `BarBuilder` is authoritative.

#### LTT second-only precision — **Medium risk**

`core/live_feed.py:269-300`, the `_parse_ltt` docstring and code:
```python
t = datetime.strptime(ltt_str, "%H:%M:%S")
```
The dhanhq library decodes the exchange LTT epoch via `datetime.utcfromtimestamp(epoch).strftime('%H:%M:%S')` — losing all sub-second information. The reconstructed datetime therefore has `microsecond=0`. Since `BarBuilder` immediately truncates to `second=0, microsecond=0` anyway (`bar_builder.py:67`), the sub-second loss has no effect on bar bucketing. It does mean the tick timestamps are only second-precise, but that is the resolution dhanhq exposes.

#### Heartbeat / uptime timestamp

`apps/trader.py:121`: `"ts": datetime.now(timezone.utc).isoformat()` — correct, UTC-aware ISO 8601.
`apps/trader.py:175`: `start_time = time.time()` — monotonic epoch seconds; uptime is computed as `int(time.time() - start_time)` (`trader.py:122`) — correct, no timezone dependency.

`apps/api.py:59-60`:
```python
age = (datetime.now(timezone.utc)
       - datetime.fromisoformat(hb["ts"])).total_seconds()
```
`datetime.fromisoformat` on a Z-suffixed string requires Python ≥ 3.11 (in 3.10 it silently fails on the `Z` suffix). The heartbeat writes `isoformat()` which produces `+00:00`, not `Z` — safe for 3.10+.

#### Epoch conversions in OR seeding

`apps/trader.py:89`:
```python
bar = datetime.fromtimestamp(ts, IST)
```
REST intraday `charts/intraday` returns Unix epoch seconds. `datetime.fromtimestamp(ts, IST)` correctly applies the IST timezone to the epoch — no DST issue since IST is fixed UTC+5:30. The subsequent comparisons `bar.date() != now.date()` and `bar.time()` are all IST-aware and correct.

---

### 3. Rendering in Charts, Logs, and UI

#### `fmtTime` — **High risk (US-browser issue)**

`dashboard/src/tokens.js:23-25`:
```js
export function fmtTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString('en-IN', {hour12:false})
}
```
`toLocaleTimeString('en-IN', {hour12:false})` with **no `timeZone` option** uses the **browser's local timezone**. If the dashboard is accessed from a machine in a timezone other than IST (e.g. US Eastern = UTC−5), all times in the executions feed (`App.jsx:747`), gate panel (`App.jsx:660`), trade table TIME column (`App.jsx:966`), and system log panel (`App.jsx:1482`) will display in local time, not IST. A trade entered at 09:30 IST (04:00 UTC) would appear as "04:00:00" on a UTC browser or "23:00:00" on a US-Eastern browser.

The backend emits timestamps as ISO 8601 UTC strings (e.g. `"2026-06-16T03:45:00+00:00"` from `journal.py`) or as raw SQLAlchemy `str(ts)` which yields `"2026-06-16 03:45:00.123456+00:00"`. Both parse correctly in `new Date()`, but the display without `timeZone: 'Asia/Kolkata'` is wrong outside IST.

The clock in the header (`App.jsx:44-47`, `ClockIST`) and the session bar (`App.jsx:326-327`) correctly pass `{ timeZone: 'Asia/Kolkata' }` — but `fmtTime` does not.

**Fix:** Add `timeZone: 'Asia/Kolkata'` to the `toLocaleTimeString` call in `fmtTime`.

#### Equity curve x-axis label

`apps/routes/db.py:37`: The x-axis key `"t"` is built as:
```python
"t": str(r[0])[11:16]
```
`r[0]` is `time_bucket('1 minute', time)` — a PostgreSQL `TIMESTAMPTZ` returned to Python as a timezone-aware `datetime`. `str(...)` on a Python `datetime` from SQLAlchemy gives the UTC ISO representation, so `[11:16]` extracts `HH:MM` in UTC. For a 09:30 IST bar, the UTC time is 04:00, so the chart x-axis displays `"04:00"` instead of `"09:30"`. Since the equity curve chart (`App.jsx:814`) uses this `t` string directly as `dataKey="t"`, the x-axis labels are 5h30m behind IST. **This is a known daily offset (constant, since no DST), so the curve shape is correct, but axis labels are wrong.**

**Fix (two options):** Either (a) convert to IST in the SQL query: `time_bucket('1 minute', time AT TIME ZONE 'Asia/Kolkata')::text` and slice `[11:16]`; or (b) pass the full ISO timestamp to the frontend and let `fmtTime` display it (once `fmtTime` is fixed to use IST).

#### Calendar P&L day grouping — **Low risk, cosmetic**

`App.jsx:1037`:
```js
const d = t.exit_ts?.slice(0, 10)
```
`exit_ts` is returned from the API as `str(r[5])` (`apps/routes/db.py:126`), which SQLAlchemy formats as `"2026-06-16 10:00:00+00:00"`. Slicing `[0:10]` gives the UTC date `"2026-06-16"`. NSE closes at 15:30 IST = 10:00 UTC, so all trade closes occur between 03:45 and 10:00 UTC — always the same UTC calendar date as the IST trading day. The calendar grid (`App.jsx:1044-1045`) uses `new Date().toISOString().slice(0, 10)` for `todayKey`, which is also UTC date. Both match, so calendar P&L grouping is consistent (both use UTC dates) and there is no cross-day mismatch within normal market hours. However if a trade were closed outside market hours (programmatic reconcile), it would appear on the wrong IST day.

#### Log timestamp patching

`apps/routes/system.py:27-39`: The log tailer detects old-format `HH:MM:SS`-only timestamps and lifts them to `{today}T{ts_raw}+00:00`, where `today = datetime.now(timezone.utc).date()`. New-format lines (from the current `datefmt`) are already `"YYYY-MM-DDTHH:MM:SS+00:00"`. These are then passed to the frontend where `fmtTime` (without IST) will display them in browser-local time. The timezone label issue is the same as above.

#### Uptime display

`dashboard/src/tokens.js:18-21` (`fmtUptime`) uses raw integer seconds from `uptime_seconds`, computed as `int(time.time() - start_time)` — no timezone involvement. Correct.

---

### 4. K-line / Candle Alignment

#### Exchange LTT vs server-clock alignment (post-M2 fix)

`core/live_feed.py:324`:
```python
tick_ts: Optional[datetime] = self._parse_ltt(data.get("LTT"))
```
When LTT is present, `_parse_ltt` builds a UTC datetime from `datetime.now(timezone.utc).date()` + the parsed HH:MM:SS, then converts to IST. When LTT is absent (missing/zero), `tick_ts` is `None` and the caller (`_on_data:327`) passes it to both `CandleBuilder.on_tick` and the `on_tick_cb` (i.e. `BarBuilder.on_tick`). Both fall back to `datetime.now(IST)` on `None` input (`live_feed.py:51`, `bar_builder.py:66`). This is correct: the fallback is server-receive time in IST.

The UTC-midnight edge case is documented in the `_parse_ltt` docstring (`live_feed.py:284-285`): market hours 09:15–15:30 IST = 03:45–10:00 UTC, so the UTC day never changes during a session. The reconstruction using `datetime.now(timezone.utc).date()` is therefore safe.

#### BarBuilder vs CandleBuilder consistency

The live path wires `BarBuilder.on_tick` as the `LiveFeed` callback and auto-detects it as the `bar_builder` reference (`live_feed.py:122-126`). `get_ohlc_tick` then returns from `BarBuilder.get_current()` rather than `CandleBuilder.current` (`live_feed.py:171-175`). So ORB sees the same intrabar O/H/L/C that goes to the DB — single source of truth, post-M2 fix. `CandleBuilder` is now only used in tests that don't pass a `bar_builder`, which is expected.

#### Backtest 1-min alignment

`research/backtest/engine.py:78`:
```python
df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(IST)
```
Bars are loaded with UTC=True (correct for `TIMESTAMPTZ` columns), then converted to IST. `trading_days()` (`backtest/engine.py:193`) uses `time::date` which PostgreSQL evaluates as the UTC date; for 1m bars in NSE hours (03:45–10:00 UTC) this is always the correct IST trading date. One subtle asymmetry: live bars are stamped at `minute_start` in IST (e.g. `09:15 IST` = `03:45 UTC`), stored in the UTC column, then loaded and converted back to IST. The round-trip is lossless.

`replay_security_day` fills at `i+1` bar's open with `ts = bar["time"].to_pydatetime()` — IST-aware, consistent with the ORB `on_tick(now, ...)` which expects IST (`strategies/orb.py:78`).

#### Minute boundary: no fence-post issue

`BarBuilder.flush()` closes any bar whose `minute_start < cutoff` where `cutoff = datetime.now(IST).replace(second=0, microsecond=0)` (`bar_builder.py:107-109`). A bar for minute 09:30 has `minute_start = 09:30:00`, and `cutoff` at 09:31:00 is `> 09:30:00`, so the 09:30 bar is closed at the next flush (up to 5s late). That 5-second flush lag means a bar's DB record can appear up to 5s after its minute closes — Kronos reads from DB, so its input lags by up to 5s. This is acceptable and documented.

---

### 5. DST & Out-of-Order Timestamps

#### DST — no issue

IST is UTC+5:30 with no DST transitions. All IST-aware code uses `ZoneInfo("Asia/Kolkata")`. No `pytz`, no `US/Eastern`, no `Europe/London`. The `_parse_ltt` reconstruction is safe exactly because the UTC offset is constant. No risk here.

#### Naive datetimes in trading paths — none found

All trading-path datetimes that cross function boundaries are tz-aware. The two `datetime.now()` calls in `engine/risk.py:176,321` are display/persistence only — they do not feed any comparison or bucketing logic that could mis-bucket.

#### Out-of-order ticks and reconnects

`CandleBuilder.on_tick` and `BarBuilder.on_tick` both use `minute_start` comparison to detect minute rollover. If the WebSocket reconnects and the first tick after reconnect carries an LTT earlier than the previous tick (e.g. a buffered packet from before disconnect), `minute_start` will be less than the current in-progress bar's `minute_start`. In `BarBuilder`:
- If `bar.minute_start != minute_start` (the reconnect tick's LTT points to a past minute), `_close_bar` is called for the current in-progress bar and a new `_Bar` is created anchored to the stale past-minute timestamp. This would incorrectly roll over the current bar and write a bar with a past timestamp to the DB. The `ON CONFLICT DO UPDATE` in `_BARS_SQL` (`bar_builder.py:24-30`) would overwrite any existing row for that past minute. This is a low-frequency edge case (only on reconnect with a stale buffered packet), but the data integrity effect is a silent overwrite of an already-closed historical minute bar.

No explicit out-of-order protection exists. The fallback to `datetime.now(IST)` when LTT is absent avoids this for the common reconnect case where LTT is missing, but a stale LTT (non-null, but pointing to a past minute) will trigger the mis-bucket.

#### `ORDER BY time` in DB queries

`apps/routes/db.py:83` (`sigs.sort(key=lambda x: x["timestamp"], reverse=True)`) sorts in Python after the DB fetch, using the raw string ISO timestamp — correct, as ISO 8601 strings sort lexicographically by UTC time.

The `trading_days` query (`backtest/engine.py:193`) uses `ORDER BY 1` (the date column) — safe.

`core/journal.py:200` fallback exit uses `ORDER BY entry_ts DESC, id DESC` — safe, tz-aware column.

---

### Summary Table

| # | Finding | File(s):Line | Severity |
|---|---------|-------------|---------|
| T1 | `CURRENT_DATE` in P&L queries is UTC date; misclassifies post-10:00 UTC trades & equity curve is 5.5h offset on IST day | `engine/risk.py:119-121`, `apps/routes/db.py:33` | **High** |
| T2 | `fmtTime()` has no `timeZone` option — all event timestamps display in browser-local time | `dashboard/src/tokens.js:25` | **High** |
| T3 | Equity curve x-axis `t` key is UTC `HH:MM` — labels are 5h30m behind IST | `apps/routes/db.py:37` | **Med** |
| T4 | Hardcoded `+00:00` in `datefmt` — correct only while box is UTC | `apps/trader.py:33`, `apps/api.py:36` | **Med** |
| T5 | Two `datetime.now()` (naive) calls in risk module; inconsistent with rest of codebase | `engine/risk.py:176, 321` | **Med** |
| T6 | Out-of-order LTT on WS reconnect can mis-bucket a bar and silently overwrite a past minute in DB | `engine/bar_builder.py:78-84` | **Med** |
| T7 | Calendar P&L and trade table TIME column use UTC date/time — acceptable within market hours, breaks on off-hours reconcile | `App.jsx:1037`, `apps/routes/db.py:126` | **Low** |
| T8 | Log-tail timestamp lift uses UTC `today` — consistent with the rest but would silently produce wrong dates if box TZ changed | `apps/routes/system.py:27-38` | **Low** |

---

### Time-handling Recommendations (Prioritized)

1. **(High — T2)** Fix `fmtTime` in `dashboard/src/tokens.js:25`: add `timeZone: 'Asia/Kolkata'` to the `toLocaleTimeString` options. Single-line change; eliminates all incorrect timestamp display for non-IST browsers.

2. **(High — T1)** Fix the daily P&L query in `engine/risk.py:119`: replace `exit_ts >= CURRENT_DATE` with `exit_ts AT TIME ZONE 'Asia/Kolkata' >= (CURRENT_DATE AT TIME ZONE 'Asia/Kolkata')`. Apply same fix to `equity_handler` (`apps/routes/db.py:33`).

3. **(Med — T3)** Fix equity curve x-axis: emit full IST time string from the backend or format with IST in the frontend so the chart x-axis labels show IST HH:MM.

4. **(Med — T5)** Replace `datetime.now()` with `datetime.now(timezone.utc)` at `engine/risk.py:176` and `321` for consistency and to eliminate timezone ambiguity.

5. **(Med — T6)** Add a stale-LTT guard in `BarBuilder.on_tick`: if `minute_start < bar.minute_start` (LTT is in the past relative to the current bar), discard the tick rather than rolling the bar back. Log a debug warning so reconnect-induced stale packets are visible.

6. **(Med — T4)** Confirm the EC2 box is configured TZ=UTC (expected) and document it. If so, the `+00:00` literal in `datefmt` is correct and the risk is documentation-only. If the box ever changes TZ, switch to a proper UTC formatter.

7. **(Low — T7/T8)** No code change required if the current convention (all times UTC, display offsets incidental) is accepted and the box stays UTC. Document the decision.

*Audited 2026-06-16. All `file:line` references verified against the commit at audit time.*

---

## 9. Re-Review — Tessera Dashboard Redesign Audit (2026-06-16 EOD)

**Trigger:** the entire React dashboard was rebuilt today (shadcn/ui on Tailwind v4, react-day-picker, Geist) and the product was rebranded **DhanAI → Tessera**.
**Method:** **16 parallel review agents** (one per narrow slice: each dashboard tab, UI primitives, styles, hooks, deps, a11y, and every engine/core/apps/infra/security/test domain) + **Opus-led rendered UI/UX testing** against the live paper session (headless, dark+light, desktop+mobile).
**Result:** ~160 findings. The engine/core remain solid (the heavy lifting was the UI); most new issues are in the dashboard and, critically, in the **contract between the dashboard and the engine heartbeat**. None block paper trading today; several are **live-readiness blockers** and several are **plainly visible UX defects**.

### Severity tally (Opus-normalised across the 16 agents)
| | Critical | High | Medium | Low |
|---|---|---|---|---|
| Trading-safety / engine | 3 | 4 | 7 | 4 |
| API ↔ dashboard contract | — | 5 | 1 | — |
| Financial-metric correctness | — | 3 | 2 | 2 |
| Accessibility (WCAG/UX) | — | 6 | 6 | 6 |
| Frontend code / styles / hooks | — | 5 | 14 | 18 |
| Infra / CI / deps / security | — | 6 | 9 | 9 |

### 🔴 Critical (verify/fix before any live exposure)
- **QR-C1 — Kill-switch flatten path.** `engine/risk.py` `activate_kill_switch()` (sync) bypasses `_halt()`, so the `on_halt` *flatten-positions* callback never fires on that path. **Action: confirm the live path** — the dashboard's `POST /api/killswitch` writes the `run/killswitch` file and the risk loop's file-watch should call `_halt()` (which *does* flatten); the direct `activate_kill_switch()` method is the gap. Make both paths converge on `_halt()`. *(agent 10)*
- **QR-C2 — API-usage double count on shutdown.** `core/api_usage.py` shutdown flusher constructs a fresh accumulator with `_last={}` and re-emits the whole `calls_today` as a delta → the day's API-call count is **doubled** in the DB (corrupts the rate-limit spend panel + accounting). *(agent 13)*
- **QR-C3 — Stale tick after WS reconnect.** `core/live_feed.py`/`engine/runner.py`: `_ticks[sid]` survives a feed reconnect and `_FEED_FRESH_S` is a dead constant, so the runner can hand ORB a **pre-disconnect price** with no staleness guard. *(agent 11)*
- **QR-C4 — No audit row for failed live orders.** `engine/execution.py`: REJECTED / network-failed LIVE orders are never written to the `orders` table — no DB trail of a failed/ambiguous fill. *(agent 10)*

### 🟠 High — API ↔ dashboard contract mismatches (root cause: no contract test)
The dashboard reads fields the engine heartbeat/endpoints never emit. Each renders a silently-wrong value:
| Field read by dashboard | Reality | Visible effect | Fix |
|---|---|---|---|
| `trader.kronos_gate === 'SHADOW'` | engine emits lowercase `"shadow"` | **Gate KPI shows "live" in shadow mode** (inverse of truth) | compare lowercase |
| `t.kill_switch_active` (HeartbeatPanel) | never emitted; state is `risk.halted` | **kill-switch row stuck on "armed"** even after firing | use `!!t.risk.halted` (TopBar already does) |
| `t.heartbeat_age_s` | never emitted | Uptime sub always "heartbeat stale" | compute in `snapshot_handler` |
| `limits.max_positions` | key is `max_open_positions` | positions cap shows `—` | rename read |
| `summary.profit_factor` (Signals) | `/api/trades` never emits it | Signals Win-Rate KPI always "n/a" (Portfolio computes its own → 1.08) | emit it, or compute client-side |

*(agents 1, 3, 10, 14, 16 — independently cross-validated)*

### 🟠 High — Financial-metric correctness (Portfolio)
- **QR-H6** Max-drawdown loops `exits` in **DB DESC (newest-first) order without sorting** → wrong peak-to-trough; also yields absurd `1123.0%` from a tiny early peak. Sort ascending by `exit_ts`; cap/relabel the %.
- **QR-H7** Intraday Sharpe shown with **n ≥ 2 days**, population variance, ×√252 → unstable/meaningless (**renders 11.61**), yet labelled "annualised". Require n ≥ ~20, sample variance, add `(n=Xd)`, or hide until enough data.
- **QR-H8** `todayCt` / round-trip counts derive from **sliced arrays** (cap 30), not the API summary → understated on busy days.
- `INR0(negative)` → `₹-3,013` (minus inside the symbol) at two sites; use the `-₹` form (PerformancePanel already does).

### 🟠 High — Accessibility (verified against tokens + DOM)
- **Dialog (Kill-switch confirm):** no focus trap, no focus restoration on close, no `aria-labelledby` → keyboard/SR users can't safely operate the single most important control.
- **Tabs:** no Arrow-key roving tabindex, no `role="tabpanel"`/`aria-controls`.
- **Contrast:** the `--faint` token = **2.97:1 (light) / 3.06:1 (dark)** on cards — **fails WCAG AA** for the ubiquitous 9–10.5px mono labels (axis ticks, range-ladder lo/hi, calendar day numbers, exec timestamps, schema IDs). Amber `PAPER` badge also fails in light.
- No `prefers-reduced-motion` guard on the kill-switch pulse; touch targets < 44px (theme toggle 34, calendar nav 28); tabular data rendered as div-grids (no table semantics). *(agents 5, 9)*

### 🟠 High — Frontend correctness / styles / hooks / infra / security (selected)
- **QR-H-styles** `hsl(var(--muted-foreground))` is used in **plain CSS** (`index.css:132` `.dhan-cal` nav) and a recharts inline style (`PnlAreaChart.jsx:56` tooltip) — the token is `--muted-fg` (only `--color-muted-foreground` exists). Invalid → falls back to an inherited (wrong) colour. *(agent 6)*
- **QR-H-kill** `KillSwitch.jsx`: a non-ok HTTP response **closes the dialog silently** (no error surfaced), and it sends `X-Dashboard-Token` from a `localStorage('dashboard_token')` key **nothing ever writes** → if `DASHBOARD_TOKEN` is set server-side, kill POSTs go unauthenticated → 401 → silent failure of the safety control. *(agents 4, 16)*
- **QR-H-poller** `usePoller` has **no `AbortController`** → 16 in-flight fetches `setState` after unmount on every navigation/HMR; the error branch keeps stale data with no "stale" signal. *(agent 7)*
- **QR-H-ci** The **dashboard is never built or linted in CI** → Vite/React regressions are silent until the agent deploy. `scripts/build_clean_db.py` uses a **`LATERAL … ORDER BY time LIMIT 1` on `bars`** (the forbidden pattern) → can hang/OOM the live DB if `--identify` runs. *(agents 8, 15)*
- **QR-H-sec** `apps/api.py` CORS is wildcard `*` on **all** routes incl. `/api/killswitch`, and `DASHBOARD_TOKEN` **fails open** by default → a cross-origin page can POST the kill-switch when the token is unset. HMAC `/postback` path is **untested** (`_FakeRequest` has no `read()`). `/postback` is also in the **dev proxy** (`vite.config.js`), relaying to prod when `DHAN_API_TARGET` points at the agent. *(agents 8, 16)*

### 🟡 Medium / Low (themes — see per-agent detail)
- **Deps/perf:** `lucide-react` (^1.20, ~39 MB) is a **prod dep with zero imports** — remove. All 15 deps use `^` ranges (vs the "pinned" policy). Bundle is **~601 KB single chunk, no code-splitting** (split recharts + react-day-picker). Google-Fonts CDN call = egress/IP-leak for a private dashboard (self-host Geist).
- **Engine/core:** weekly-P&L DB-fallback understates the loss meter after a DB blip; paper executor can swallow a fill on a DB error; Kronos "confidence" measures temporal spread, not inter-sample uncertainty (samples are averaged before `pred_df`); `_evaluate()` does work after halting.
- **Infra:** `ssh_allowed_cidr` defaults `0.0.0.0/0` (no validation block); `dhan-alert@.service` runs `python` (no alias on 22.04) → silent alert death; `aws_eip` has no `prevent_destroy` (Dhan 7-day re-whitelist risk); `alembic.ini` ships a plaintext `trader123` default.
- **Dead/cosmetic:** `@keyframes spin` unused; `--radius`/`--color-muted` dead tokens; 14 `.dhan-cal` selectors + a `PortfolioTab` class still use the pre-rebrand `dhan` name; index-as-`key` in feeds; `/api/system/health` polled every 30 s but never rendered.

### Opus UI/UX rendered testing (my own pass, live data, dark+light, desktop+mobile)
- **Confirmed visually broken metrics:** Max Drawdown renders **`1123.0%`** and Sharpe **`11.61`** — both look like bugs to any user and undercut trust (QR-H6/H7).
- **Truncation:** the `SHARPE (INTRADAY)` KPI label truncates to `SHARPE (INTRA…` at narrow widths (StatCard `truncate` + tight column).
- **Rebrand:** "Tessera" wordmark, title, and theme all render correctly; calendar nav arrows **do render** (mis-coloured via the `--muted-fg` bug, not invisible — agent claim of "invisible" downgraded).
- **Responsive:** mobile (390px) correctly drops to 2-col KPIs + single-column cockpit/grids; **touch targets are visibly small** (confirms the a11y finding).
- **Consistency:** Signals shows gate "shadow/live" and Win-Rate "n/a" while Portfolio shows real profit-factor — the contract bugs produce **inconsistent numbers across tabs**, which a user *will* notice.

### Test gaps (highest-value additions)
1. **API contract test** (`aiohttp.TestClient` asserting the `/api/snapshot`, `/api/trades`, `/api/rate-limits` response shapes) — would have caught **all five** contract mismatches above.
2. **Any** dashboard tests (currently zero) — start with `KillSwitch` (sends token, surfaces failure) and the Portfolio metric maths.
3. HMAC `/postback` valid/invalid/missing-signature tests (give `_FakeRequest` a `read()`).
4. Coverage for the new handlers (`system_health`, `backfill_status`, `watchlist_refresh` auth, `rate_limits`, `kronos_screener`).

### Recommended remediation order
1. **Contract mismatches** (5 one-line fixes in the dashboard or heartbeat) — biggest visible-correctness win.
2. **Financial metrics** (drawdown sort + cap, Sharpe guard) — visible credibility.
3. **Kill-switch safety** (QR-C1 path convergence + KillSwitch error surfacing + token) — before live.
4. **QR-C2 / QR-C3 / QR-C4** engine fixes — before live.
5. **CI: build + lint the dashboard**; add the contract test.
6. **A11y**: focus trap on the dialog, `--faint` contrast bump, reduced-motion, touch targets.
7. Cleanup: remove `lucide-react`, code-split, fix `--muted-fg`, dead code.

*Re-review by 16 parallel agents + Opus synthesis/UX testing, 2026-06-16. Full per-domain detail captured above; `file:line` refs verified at audit time. **Status: documented, not yet remediated.***
