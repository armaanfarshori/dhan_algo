# Live Readiness Checklist — M8 Go/No-Go Gate

This document is the explicit gate that must be cleared before enabling live trading (M8 tiny-live). Every item must be checked and evidenced. No item may be skipped. The philosophy: **evidence before exposure**.

Current status of each item is left blank — fill in with evidence (PR link, test run, date) when resolved.

---

## 1. Backtest Evidence

The three-way comparison (ORB alone vs ORB + zero-shot Kronos vs ORB + fine-tuned Kronos) must be completed on the clean data replica with realistic Indian intraday costs before any live order is placed.

- [ ] M2 historical backfill complete (all NSE_EQ, no >1% chunk-skip rate)
- [ ] M2.5 clean data replica built (`scripts/build_clean_db.py`) — survivorship bias reviewed and documented
- [ ] 2-year three-way backtest run on clean data (`research/backtest/`)
- [ ] ORB-alone Sharpe ≥ **[fill in threshold]** on out-of-sample period (date-split, not random)
- [ ] Realistic cost stack applied: STT, brokerage, SEBI charges, slippage model (see `research/backtest/costs.py`)
- [ ] Backtest report reviewed and committed under `research/backtest/results/`
- [ ] Decision documented: which variant (ORB alone / +zero-shot / +fine-tuned) will go live

**Kronos calibration gate (required before disabling shadow mode):**

- [ ] Calibration fill complete: `python -m ml.calibration fill` has processed realized 30-min returns into `features_snapshot` for at least **30 fresh rows** (rows not yet filled at time of scoring)
- [ ] `python -m ml.calibration report` shows fresh accuracy ≥ **55%** (re-arm criterion from `ml/calibration.py`)
- [ ] Decision logged: set `KRONOS_SHADOW_MODE=false` + restart dhan-trader (deliberately manual)

---

## 2. Correctness — Engine Behaviour

These must be verified on paper before trusting the engine with real capital.

- [ ] **C1 — Order idempotency:** duplicate order IDs are detected and not re-sent (TEST-01 green; see `tests/`)
- [ ] **C2 — Exit closes one row:** partial-exit / double-exit bug fixed; single-row close verified end-to-end in paper session
- [ ] **Rate limits enforced:** per-day (100K Dhan API calls) and per-minute limits active in `core/client.py` (TEST-02 green)
- [ ] **Broker reconcile verified:** `reconcile_with_broker()` tested in LIVE mode path — positions at boot match broker truth
- [ ] **Mid-session restart safe:** `seed_opening_ranges()` rebuilds OR from REST intraday bars; already-broken sides marked as tried; verified manually or in integration test
- [ ] **EOD square-off unconditional:** square-off fires regardless of strategy state or gate verdict; no dependency reintroduced
- [ ] **Kill-switch end-to-end:** `POST /api/killswitch` → flag written → risk loop detects within ~10 s → all positions flattened → confirmed in paper session
- [ ] **REJECTED order handling:** `LiveExecutor` receives REJECTED → logs CRITICAL → returns `None` (no phantom fill); tested
- [ ] **Screener floors hold:** ₹50 price floor + 50K volume floor + scrip-master EQUITY validation active; index constituents and penny stocks excluded

---

## 3. Security

- [ ] **SEC-01 — Dhan credentials rotated** before going live (access token + any API keys); old token invalidated
- [ ] **SEC-02 — `DASHBOARD_TOKEN` set** in `.env`; kill-switch endpoint protected by shared-secret check (`_check_auth`)
- [ ] **SEC-03 — Security Group locked** to operator IP only (no `0.0.0.0/0` on port 8765 or SSH)
- [ ] **SEC-04 — API not internet-exposed:** dashboard tunnel accessed only via SSH tunnel or VPN, never directly from public internet
- [ ] **SEC-05 — M6 auth decision made:** either M6 `/api/mode` POST auth is implemented before going live, or a documented decision records why the current read-only 409 response is sufficient
- [ ] **SEC-06 — Postback HMAC enabled:** `DHAN_WEBHOOK_SECRET` set; `X-Dhan-Signature` verification active for the `/postback` endpoint
- [ ] **SEC-07 — No secrets in repo:** final audit of committed files — no tokens, IPs, account IDs, or Telegram credentials

---

## 4. Ops Readiness

- [ ] **OPS-01 — Bootstrap reprovisions cleanly:** Terraform apply from scratch on a fresh instance works end-to-end (or the equivalent manual runbook is documented and tested)
- [ ] **Log rotation live:** `/var/log/dhan/trader.log` and `api.log` are rotated (logrotate config in place); disk usage monitored
- [ ] **Alerts cover all critical paths:**
  - [ ] REJECTED orders → Telegram CRITICAL alert
  - [ ] Feed disconnect → Telegram alert within 30 s
  - [ ] Process crash → Telegram alert (systemd `OnFailure=` or watchdog cron)
  - [ ] Disk > 80% → Telegram alert
  - [ ] Calibration run failure → Telegram alert
- [ ] **Backups tested:** TimescaleDB backup procedure run and restore to a clean instance verified
- [ ] **Heartbeat staleness monitored:** external check (cron or uptime monitor) fires Telegram if heartbeat age exceeds 60 s during market hours
- [ ] **Old platform_watchdog.sh cron absent:** confirmed removed (caused the June crash loop by kill -9ing a slow-booting process)

---

## 5. Configuration — Live Flags

These must be set deliberately, not by accident.

- [ ] `PAPER_TRADING=false` explicitly set in agent `.env` (default is `true`)
- [ ] `ALLOW_LIVE_TOGGLE=true` set (required for live mode activation)
- [ ] `live_risk_scale` set to intended fraction (e.g. `0.1` for 10% of capital on first tiny-live session)
- [ ] `CAPITAL` set to actual funded capital in the Dhan account
- [ ] `MAX_DAILY_LOSS_PCT` reviewed and confirmed appropriate for live capital
- [ ] **EIP whitelisted at Dhan** for order placement (done 2026-06-12; 7-day change lock — confirm still active)
- [ ] `KRONOS_CHECKPOINT` set if running fine-tuned model (empty = zero-shot HuggingFace; set to S3 path after fine-tune)
- [ ] `KRONOS_SHADOW_MODE` status confirmed (keep `true` until calibration criterion above is met)

---

## 6. Sign-off

Before the first live order is placed, the following must be confirmed in writing (in this document or linked issue/PR):

| Item | Status | Evidence / Notes | Date |
|---|---|---|---|
| Backtest evidence reviewed | | | |
| Correctness items all green | | | |
| Security items all cleared | | | |
| Ops items all cleared | | | |
| Live config deliberately set | | | |
| **Go / No-Go decision** | | | |

---

*This checklist operationalises the safety rules in `CLAUDE.md` and the critical path described in the project milestone table. It is the gate between paper trading and M8 tiny-live.*
