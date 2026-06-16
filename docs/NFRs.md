# Non-Functional Requirements — Tessera

**Status:** Current (2026-06-16)
**Scope:** Platform-wide; applies to both `dhan-trader` and `dhan-api` unless a domain column says otherwise.

These requirements convert the implicit constraints scattered across `CLAUDE.md` "Key constraints", code comments, and the QA report into explicit, measurable targets. Each entry includes the mechanism that enforces or measures the requirement and the test(s) that assert it.

---

## Format

| Field | Meaning |
|---|---|
| **ID** | Stable identifier (NFR-NN) |
| **Statement** | Measurable target with a number or observable outcome |
| **Rationale** | Why it exists (usually a hard constraint or a past failure) |
| **Verification** | Mechanism in code + test file / manual check |

---

## Safety & Control

### NFR-01 — Kill-switch response latency

**Statement:** After `POST /api/killswitch` writes `run/killswitch`, `RiskEngine._evaluate` detects the file and halts trading within **≤ 10 seconds** under normal load.

**Rationale:** `RiskEngine.run()` loops on `check_interval_seconds = 10` (`engine/risk.py:297`). The ~10 s upper bound is documented in `CLAUDE.md` and in the `killswitch_handler` response message (`apps/api.py:354`). The kill-switch is the operator's last resort for a stuck position; latency longer than one check interval would be a regression.

**Verification:**
- `engine/risk.py:48` — `RiskParams.check_interval_seconds = 10` is the ceiling.
- `apps/api.py:354` — response message states "within ~10s".
- `tests/test_risk.py:test_evaluate_killswitch_file_triggers_halt` — writes the flag file, runs `_evaluate()`, asserts `kill_switch=True` and `halted=True` and that `on_halt` callback fires.

---

### NFR-02 — Persistent halt across restarts (loss meter survives restart)

**Statement:** A daily-loss halt or weekly-loss halt triggered in process A must be re-entered automatically when a replacement process B starts, without operator intervention, for the remainder of the scope (calendar day or ISO week).

**Rationale:** On 2026-06-12, a mid-session restart erased ₹3,533 of tracked losses from the in-memory risk view. The fix writes `run/halt_state.json` atomically on every `_halt()` call (`engine/risk.py:165-178`) and restores it on `load_persisted_halt()` (`engine/risk.py:137-163`).

**Verification:**
- `tests/test_risk.py:test_daily_halt_survives_restart` — creates halt, simulates restart, asserts halt is re-entered.
- `tests/test_risk.py:test_stale_halt_file_is_cleared` — previous-day halt file is deleted on boot (not re-entered on a new day).
- `tests/test_risk.py:test_resume_clears_halt_file` — `resume()` deletes the file.

---

### NFR-03 — Realized P&L source of truth is the database, not process memory

**Statement:** `RiskEngine.refresh_pnl()` reads realized P&L from the `trades` table (filter `status='CLOSED'`), not from any in-process counter. A restart must not reset the daily-loss meter.

**Rationale:** Same 2026-06-12 root cause as NFR-02. `engine/risk.py` specification comment (lines 12-16) and `refresh_pnl()` implementation (lines 110-133).

**Verification:**
- `engine/risk.py:110-133` — DB query on `CURRENT_DATE` and ISO week window.
- `tests/test_risk.py:test_evaluate_daily_loss_triggers_halt` — monkeypatches `_realized_today` to simulate a reloaded DB value, confirms halt triggers.
- Confirmed in first paper session 2026-06-12 (ops note in `CLAUDE.md`).

---

### NFR-04 — Paper trading is the default; live mode requires two explicit actions

**Statement:** `PAPER_TRADING=true` in `config.py` default. Switching to live additionally requires `ALLOW_LIVE_TOGGLE=true` in `.env` and a `dhan-trader` restart. A POST request alone must never flip the mode.

**Rationale:** Prevents accidental live activation. `apps/api.py:trading_mode_handler` returns 409 on POST with the explanation, and `config.py` comments (`allow_live_toggle`, line 35-37) encode the intent. Safety rule #1 in `CLAUDE.md`.

**Verification:**
- `config.py:33` — `paper_trading: bool = True`.
- `apps/api.py:334-344` — POST `/api/mode` always returns 409.
- `tests/test_config.py` — default is paper.

---

### NFR-05 — Kronos gate is fail-open; model errors never block trades

**Statement:** Any exception inside `KronosGate` (model load failure, OOM, timeout) must cause the gate to return `True` (allow), log a WARNING, and not propagate.

**Rationale:** Kronos is an advisory layer; trading must continue if the gate is broken. `ml/kronos_gate.py` wraps scoring in try/except and always allows on error.

**Verification:**
- `ml/kronos_gate.py` — error paths return `True`.
- `tests/test_kronos_gate.py` — tests exception paths.
- Safety rule #4 in `CLAUDE.md`.

---

### NFR-06 — EOD square-off is unconditional

**Statement:** The EOD square-off routine evaluates above all strategy-state gates. It must flatten open positions regardless of Kronos gate mode, kill-switch state (exits are never blocked by check_intent — see `engine/risk.py:253-255`), or strategy context.

**Rationale:** A mid-session restart that loses strategy context must not leave positions open overnight. Documented in `CLAUDE.md` "Mid-session restart" and `docs/Architecture.md` "Mid-session restart safety".

**Verification:**
- `engine/risk.py:253-255` — `is_exit=True` bypasses all exposure limits.
- `apps/trader.py` — EOD square-off runs before any OR-gate check.
- Safety rule #6 in `CLAUDE.md`.

---

## API Rate Limits

### NFR-07 — Per-second Dhan API rate limits are enforced with sleep throttling

**Statement:** The `data` category is limited to **≤ 5 requests/second**, the `orders` category to **≤ 10 requests/second**, `quote` to **≤ 1/second**, enforced by an async sliding-window throttler that sleeps (never drops or silently overruns).

**Rationale:** Dhan has no sandbox; bursts hit production. Exceeding per-second limits returns DH-904 (burst) or 429. Backfill uses `charts/intraday` which tolerates ~1 req/s (`CLAUDE.md`).

**Verification:**
- `core/client.py:55-107` — `_second_window` and `_min_window` use sleep throttle.
- `tests/test_rate_limit_usage.py` — confirms `usage()` shape including `per_sec`.

---

### NFR-08 — Per-day Dhan API caps are tracked and must raise on breach

**Statement:** The `orders` category must not exceed **7,000 calls/day**; the `data` category must not exceed **100,000 calls/day**. On breach, `RateLimitExceeded` is raised immediately (no sleep for a 24-hour window).

**Rationale:** `core/client.py:41-42` documents the design intent: "A trading process must never block for hours; exhausting the daily quota raises immediately." QA report (M1, Risk Register) noted that per-day and per-hour enforcement was defined but not fully wired to the deque-based windows.

**Verification:**
- `core/client.py:47-58` — `per_day` values defined; `_day_window` deque wired as `raise_on_exceed=True`.
- `core/client.py:76,120-121` — `calls_today` counter tracks all calls across categories.
- `tests/test_rate_limit_usage.py:test_usage_counts_calls_today` — counter increments correctly.
- Note: per-hour and per-day window enforcement (FEAT-02) was identified in the QA report as requiring test coverage; confirm deque enforcement paths are exercised before M8.

---

## Database Conventions

### NFR-09 — No full-scan queries on the `bars` hypertable

**Statement:** No query against `bars` may use `COUNT(*)`, `ORDER BY time LIMIT 1`, or any pattern that causes TimescaleDB to decompress compressed chunks. Row counts use `approximate_row_count()`; date spans use `timescaledb_information.chunks` catalog; sizes use `hypertable_size()`.

**Rationale:** `bars` contains 300M+ rows under active backfill. A full scan or chunk decompression takes minutes and blocks DB connections used by the live trading path. Observed to freeze the dashboard panel during backfill (fixed in `apps/api.py:db_stats_handler`, lines 501-561).

**Verification:**
- `apps/api.py:501-561` — `db_stats_handler` uses `approximate_row_count()` and chunk catalog. Inline comment documents the exact prohibited patterns.
- `CLAUDE.md` "Key constraints" — "NEVER `COUNT(*)` or `ORDER BY time LIMIT 1` on `bars`".
- Manual code review / grep: `grep -rn "COUNT(\*)\|ORDER BY time" --include="*.py" .` must return no hits against `bars` outside of `timescaledb_information`.

---

### NFR-10 — One trade row per fill; no implicit multi-close

**Statement:** Each call to `log_trade_exit` must close exactly the rows that match the exit's `dhan_order_id`. In LIVE mode (order_id present), the `UPDATE` must scope to `dhan_order_id = :order_id`; it must never close sibling open rows from a multi-entry position.

**Rationale:** QA finding C2 (partially fixed 2026-06-15): the paper path (no order_id) closes all open rows for a security, which was intentional for paper's single-entry model. The live path (order_id present) must be scoped. The risk engine's halt reads from `trades`; a double-close corrupts the daily-loss meter.

**Verification:**
- `core/journal.py:log_trade_exit` — scoped UPDATE with `AND (:order_id IS NULL OR dhan_order_id = :order_id)`.
- `tests/test_journal_lifecycle.py` — exit closes one row, not siblings.

---

## Memory & Resources

### NFR-11 — Agent EC2 (t4g.small, 2 GB RAM) must not OOM under normal trading

**Statement:** Total RSS of `dhan-trader` + `dhan-api` must remain **below 1.6 GB** under normal intraday operation. Kronos model must not be loaded at startup; it is lazy-loaded on first gate invocation.

**Rationale:** t4g.small has 2 GB. Eager Kronos loading on startup plus DB connection pool plus TimescaleDB backend routinely exhausts the 2 GB. `CLAUDE.md`: "Kronos lazy-loads on first use; never eager-load at startup."

**Verification:**
- `config.py:96` — `kronos_offline: bool = True` prevents HuggingFace network calls.
- `ml/kronos_gate.py` — model instantiated on first `score()` call, not at module import.
- `core/kronos_signal.py` — `KronosSignalEngine` is not instantiated at process start.
- Operational check: `ps aux` on agent after boot; RSS of trader process should be under 500 MB before first Kronos invocation.

---

### NFR-12 — Dashboard API executor pool does not starve file-serving threads

**Statement:** `dhan-api` must configure a dedicated `ThreadPoolExecutor` with **≥ 16 threads** so that slow DB queries cannot block aiohttp's default executor used for file serving.

**Rationale:** During backfill write contention the default ~6-thread executor saturated, freezing static file responses while JSON endpoints kept answering. Fixed in `apps/api.py:main()` (lines 928-930).

**Verification:**
- `apps/api.py:928-930` — `ThreadPoolExecutor(max_workers=16)` set as the loop's default executor.

---

## Data Integrity & Auditability

### NFR-13 — All orders, fills, trades, and gate verdicts are persisted before acknowledgement

**Statement:** Every `OrderIntent` routed through `RiskEngine.check_intent` is logged to `orders` before placement. Every `Fill` upserts `engine_positions` and appends to `fills`. Every trade (entry + exit) has a row in `trades`. Every Kronos gate verdict (allow or block, shadow or enforcing) is written to `signals` with `strategy='orb_gate'`.

**Rationale:** Auditability for regulatory and post-hoc analysis. No order should be recoverable only from broker records. Gate verdicts persist even in shadow mode to allow calibration (`ml/calibration.py`).

**Verification:**
- `engine/portfolio.py:apply_fill()` — upserts `engine_positions`, appends `fills`, calls `core/journal.log_trade_entry/exit`.
- `ml/kronos_gate.py` — verdict + features written to `signals` on every call.
- `tests/test_execution.py`, `tests/test_portfolio.py`, `tests/test_journal_lifecycle.py`.

---

### NFR-14 — Bar timestamps use exchange time, not arrival time

**Statement:** Every row inserted into `bars` must carry the exchange-aligned 1-minute boundary as its `time` column, derived from the WebSocket packet's exchange timestamp (or bar-close inference), not the wall-clock time of receipt.

**Rationale:** Kronos `score_from_db()` reads bars and reports `data_age_min` relative to bar close. Bars stamped with arrival time would report false freshness and corrupt gate calibration.

**Verification:**
- `engine/bar_builder.py` — bar close time is computed from the bar's minute boundary, not `datetime.now()`.
- `tests/test_bar_builder.py`, `tests/test_bar_builder_robustness.py`.

---

## Alerting

### NFR-15 — Critical events trigger a Telegram alert within 60 seconds

**Statement:** On any halt (daily/weekly loss, kill-switch), order rejection, or CRITICAL-level log event, a Telegram message must be sent via `core/notify.py` within 60 seconds. `notify.send()` must never raise (alerting must not break trading).

**Rationale:** Solo operator; no human watching logs continuously. The Hermes gateway was retired 2026-06-11 for cost; `core/notify.py` is the replacement at zero cost.

**Verification:**
- `core/notify.py:28-50` — wrapped in try/except; never re-raises.
- `engine/risk.py:_halt()` — fires `on_halt` callbacks, which include the Telegram alert.
- `config.py:telegram_bot_token` and `telegram_chat_id` — set in agent `.env`.
- Manual check: trigger a paper kill-switch and confirm Telegram delivery within 60 s.

---

## Token Safety

### NFR-16 — `.env` token rewrite is atomic; concurrent writes are prevented

**Statement:** `core/token_manager` must use a temp-file-and-rename pattern when rewriting `.env` so a process kill mid-write does not corrupt it. Concurrent token refreshes across processes must be serialized by a file lock.

**Rationale:** QA finding H2 (fixed, commit `af3441b`): a non-atomic regex substitution with no lock meant a mid-write kill would leave `.env` truncated, losing DB credentials.

**Verification:**
- `core/token_manager.py` — after fix: atomic write via `tempfile` + `os.replace`, file lock guards concurrent callers.
- `tests/test_token_manager.py`, `tests/test_token_safety.py`.

---

## Screener Safety

### NFR-17 — Screener candidates must pass price and volume floors and instrument validation

**Statement:** Any security entering the watchlist must satisfy: **close price ≥ ₹50**, **20-day average volume ≥ 50,000 shares/day**, and must be classified as `EQUITY` in the `instruments` table for segment `NSE_EQ`. Open positions are exempt from re-validation (exits must always be reachable).

**Rationale:** Day-1 paper session (2026-06-12): the unfloored screener admitted ₹13 penny stocks (tick = 7bps vs paper slippage floor of 2bps — dishonest simulation) and a non-tradeable index scrip smuggled in via a cached watchlist.

**Verification:**
- `config.py:70-74` — `screener_min_price = 50.0`, `screener_min_avg_volume = 50_000`.
- `core/nse_screener.py` — floors and scrip-master validation applied.
- `tests/test_screener.py`.
