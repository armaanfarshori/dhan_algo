# QA Analysis Report — DhanAIBot (`dhan_algo`)

**Date:** 2026-06-15
**Scope:** Full codebase QA sweep (apps, engine, core, ml, strategies, research, tests)
**Method:** Evidence-based source review against the actual code (aiohttp backend, JSX frontend, raw SQLAlchemy, TimescaleDB)

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

DhanAIBot is a paper-mode-default intraday NSE trading platform with a genuinely clean post-rewrite architecture: pure strategy logic, a swappable executor abstraction, DB-persisted state, and disciplined separation between the trading process and the read-only dashboard. Engineering hygiene is above average for a solo trading project — 85 fast unit tests, pure-function stats, and strong safety defaults (paper-first, single kill-switch owner, fail-open gate).

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

Every finding now has a regression/characterization test under `tests/` (7 new files, 23 cases). Confirmed-but-unfixed bugs are `xfail(strict=True)` — they keep CI green and auto-flip to a hard failure the moment they're fixed.

| ID | Status | Test | Notes |
|----|--------|------|-------|
| C2 | ✅ Fixed | `test_journal_lifecycle.py` | `log_trade_exit` closes exactly one (latest matching) open row — no blanket close, no P&L double-count |
| H3 | ✅ Fixed | `test_journal_lifecycle.py` | flip now journals exit + a new entry for the opposite position |
| C1 | ⏳ xfail | `test_client_idempotency.py` | live-only; needs `correlation_id` + non-retriable orders |
| H1 | ⏳ xfail | `test_client_idempotency.py` | windowed rate limits unenforced |
| H2 | ✅ Fixed | `test_token_safety.py` | token cached atomically (os.replace); `.env` never rewritten by the app |
| H4 | ✅ pinned | `test_execution_robustness.py` | documented behavior covered by passing tests; design unchanged (live-only) |
| M1 | ⏳ xfail | `test_client_idempotency.py` | unserialized auth refresh |
| M2 | ✅ pinned | `test_bar_builder_robustness.py` | `on_tick` honors explicit ts; gap is the LiveFeed caller (forward LTT) |
| M3 | ⏳ xfail | `test_reconcile_partial_avg.py` | `float(None)` on partial broker avg (live-only) |
| M4 | ⏳ xfail | `test_bar_builder_robustness.py` | unbounded `_pending` |
| M6 | ⏳ xfail | `test_api_handlers.py` | postback no-op / unauthenticated |

**Residual trade-model limitations** (surfaced while fixing C2/H3, tracked, not yet addressed): a *partial* reduce still marks a trade row CLOSED even though quantity remains, and multi-entry positions are not P&L-apportioned across rows. Neither occurs with the current ORB strategy (one entry, one full exit per position), but a future adding/scaling strategy would need the trades table to model position lifecycle rather than individual fills.

Suite after remediation: **102 passed, 6 xfailed**.

---

*Generated 2026-06-15. References are `file:line` against the commit current at generation time; verify line numbers before acting as they drift with edits.*
