# M3 Decision Study — Results & Go/No-Go Record

**Strategy:** ORB (Opening Range Breakout) on screened NSE equities
**Study:** Three-way comparison — ORB alone vs ORB + Kronos zero-shot vs ORB + Kronos fine-tuned
**Template version:** 1.0
**Philosophy:** *Evidence before exposure.* Thresholds are committed in Section 2 **before** any result is viewed. After Section 3 is filled, Section 2 is frozen — moving a threshold to fit a number invalidates the study.

---

## How to use this file

1. Fill **Section 1 (Provenance)** and **Section 2 (Pre-committed thresholds)** *before* running anything. Get a second person to countersign Section 2.
2. Run the three backtests (commands in Section 1.5).
3. Paste raw outputs into **Section 3** and compute the KPI panel.
4. Evaluate each gate in **Section 4** mechanically — PASS/FAIL against the frozen Section 2 numbers. No reinterpretation.
5. Record the decision and sign off in **Section 5**.
6. Commit the completed copy as `research/backtest/results/M3-YYYY-MM-DD.md`. Never overwrite a prior result file.

> ⚠️ If any threshold in Section 2 is edited after Section 3 contains data, this run is **void**. Start a fresh dated file.

---

## 1. Provenance (fill before running)

| Field | Value |
|---|---|
| Run date | `____-__-__` |
| Operator | `__________` |
| Git commit (full SHA) | `__________________________________________` |
| Branch | `__________` |
| `research/backtest/` last modified (commit/date) | `__________` |
| Database | `dhan_clean` (M2.5 replica) — confirm: `config.backtest_db_url` → `__________` |
| M2.5 build manifest (`s3://…/kronos/training-data/manifest.json` `built_at`) | `__________` |
| Universe size (instruments in clean replica) | `______` |

### 1.1 Window & split

| Field | Value |
|---|---|
| Full window (from → to) | `____-__-__` → `____-__-__` |
| In-sample (IS) period | `____-__-__` → `____-__-__` |
| Out-of-sample (OOS) period | `____-__-__` → `____-__-__` |
| Split type | **date-split** (NOT random — required) |
| OOS as % of total trading days | `____%` |

> The OOS period must be a contiguous *later* block, never seen during any tuning or fine-tuning. Run 3's fine-tuned checkpoint must be trained only on data strictly before the OOS start.

### 1.2 Cost & fill parameters (must match across all three runs)

| Parameter | Value used | Note |
|---|---|---|
| Slippage (bps) | `____` | Audit recommends a stress pass at **4–5 bps**, not just the 2 bps default |
| Starting equity (₹) | `____` | |
| Risk per trade | `____` | default 0.01 |
| Max notional % per trade | `____` | default 0.20 |
| Backtest min avg volume floor | `____` | CLI `--min-volume` default is **50,000** (matches live screener); `dhan_clean` pre-filtered ≥50k |
| Cost stack version | `research/backtest/costs.py` @ `__________` | Dhan rates 2025–26 |

### 1.3 Fidelity acknowledgments (operator must check each)

These are the known biases from the fidelity audit. Confirm each is addressed or explicitly accepted before trusting results.

- [ ] **Survivorship:** universe derives from the current `clean_universe` table (dhan_clean replica) → delisted names absent → results are an **optimistic upper bound**. Ceiling accepted (point-in-time master not reconstructable from the feed). Confirm: `__________`
- [ ] **Slippage understatement:** flat-bps understates low-priced/thin names; `_slip` floors at half-tick (₹0.05). Stress pass at ≥4 bps completed (Section 3.5): `yes / no`
- [ ] **Volume floor:** CLI `--min-volume` default is **50,000** (matches live screener); `dhan_clean` is pre-filtered ≥50k. Confirm value used: `__________`
- [ ] **Partial fills:** MODELLED — fill qty capped at `partial_fill_pct` (default 10%) of the fill-bar volume. Confirm pct used: `__________`
- [ ] **No look-ahead:** confirmed by `test_gate_receives_only_past_bars` passing on this commit: `yes / no`

### 1.4 Commands run (paste exact invocations)

```
# Run 1 — ORB standalone (does the rule-based edge exist at all?)
python -m research.backtest --from <IS_start> --to <OOS_end> --split-date <OOS_start> --gate none --json run1.json --slippage-bps <X> --equity <E>

# Run 2 — ORB + Kronos zero-shot gate
python -m research.backtest --from <IS_start> --to <OOS_end> --split-date <OOS_start> --gate kronos --json run2.json --slippage-bps <X> --equity <E>

# Run 3 — ORB + Kronos fine-tuned checkpoint (KRONOS_CHECKPOINT=s3://…)
KRONOS_CHECKPOINT=<s3-uri> python -m research.backtest --from <IS_start> --to <OOS_end> --split-date <OOS_start> --gate kronos --json run3.json --slippage-bps <X> --equity <E>

# (--split-date produces IS/OOS panels in ONE run; no need to run each twice)
```

> Each run must be executed twice (IS-only and OOS-only windows) OR with a split-aware report, so IS and OOS KPIs can be reported separately in Section 3.

---

## 2. Pre-committed thresholds (FREEZE before viewing results)

> Proposed defaults below. **Review, adjust to your risk appetite, then LOCK.** Once Section 3 has data, these cannot change.

### 2.1 Primary go/no-go (evaluated on the OOS period, net of costs)

| KPI | Threshold to pass | Locked value |
|---|---|---|
| Net annualized Sharpe (daily √252) | ≥ **1.0** (floor); ≥ 1.5 = strong | `____` |
| Max drawdown (% of starting equity, daily curve) | ≤ **15%** *and* never implies repeated kill-switch trips | `____` |
| Net profit factor | ≥ **1.3** | `____` |
| Net total return (OOS, after costs) | **> 0** with margin | `____` |

### 2.2 Statistical validity (any failure ⇒ result is noise, not evidence)

| KPI | Threshold to pass | Locked value |
|---|---|---|
| Total trades (full window) | ≥ **150** | `____` |
| Total trades (OOS) | ≥ **50** | `____` |
| OOS Sharpe ÷ IS Sharpe (degradation) | ≥ **0.70** (≤30% decay) | `____` |
| OOS Sharpe absolute | ≥ **1.0** | `____` |

### 2.3 Robustness (fragility checks)

| KPI | Threshold to pass | Locked value |
|---|---|---|
| Net P&L ÷ Gross P&L (cost retention) | ≥ **0.50** | `____` |
| Top-5-day share of total net profit | ≤ **50%** | `____` |
| Months with positive net P&L | ≥ **60%** | `____` |
| Edge survives slippage stress (≥4 bps) | Net Sharpe stays ≥ 1.0 | `____` |

### 2.4 Kronos gate value — Run 2 vs Run 1 (only relevant if going the AI route)

| KPI | Threshold to pass | Locked value |
|---|---|---|
| OOS net Sharpe uplift (Run 2 − Run 1) | ≥ **+0.30** | `____` |
| `gate_value_summary.gate_adds_value` | **true** (ALLOW avg return > BLOCK avg return) | `____` |
| Calibration fresh accuracy (`ml.calibration report`) | ≥ **55%** | `____` |
| Min sample per ALLOW/BLOCK bucket | ≥ `RECOMMEND_MIN_N` | `____` |

### 2.5 Fine-tune promotion — Run 3 vs Run 2

| KPI | Threshold to pass | Locked value |
|---|---|---|
| OOS net Sharpe uplift (Run 3 − Run 2) | ≥ **+0.30** (meaningful, not rounding) | `____` |
| No worse on max drawdown than Run 2 | within tolerance `____` | `____` |

**Section 2 frozen by (sign + date):** `__________`  /  **Countersigned:** `__________`

---

## 3. Results (fill after running — do not touch Section 2)

### 3.1 Headline panel (NET, after full cost stack)

| KPI | Run 1 (ORB) IS | Run 1 OOS | Run 2 (+ZS) IS | Run 2 OOS | Run 3 (+FT) IS | Run 3 OOS |
|---|---|---|---|---|---|---|
| Annualized Sharpe | | | | | | |
| Total return % | | | | | | |
| Max drawdown % | | | | | | |
| Profit factor | | | | | | |
| Win rate % | | | | | | |
| Avg win ÷ avg loss | | | | | | |
| Trades (count) | | | | | | |
| Gross P&L (₹) | | | | | | |
| Costs (₹) | | | | | | |
| Net P&L (₹) | | | | | | |
| Net ÷ Gross | | | | | | |

### 3.2 OOS degradation

| Run | IS Sharpe | OOS Sharpe | OOS ÷ IS |
|---|---|---|---|
| Run 1 | | | |
| Run 2 | | | |
| Run 3 | | | |

### 3.3 Concentration / consistency (per run, OOS)

| Metric | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| Top-5-day share of net profit % | | | |
| Months positive (n / total) | | | |
| Longest losing streak (days) | | | |

### 3.4 Gate diagnostics (Run 2 / Run 3)

| Metric | Run 2 (ZS) | Run 3 (FT) |
|---|---|---|
| ALLOW: n / hit-rate / avg return (bps) | | |
| BLOCK: n / hit-rate / avg return (bps) | | |
| `gate_adds_value` | | |
| Calibration fresh accuracy % | | |

### 3.5 Slippage stress (re-run best variant at ≥4 bps)

| Variant | Slippage bps | OOS net Sharpe | Net P&L (₹) | Survives? |
|---|---|---|---|---|
| | | | | |

### 3.6 Per-security notes (optional, qualitative)

> Which names drove or dragged results? Any single name dominating P&L = fragility flag. Free text:

`__________`

---

## 4. Gate evaluation (mechanical — PASS/FAIL vs frozen Section 2)

Evaluate the **best-performing standalone-or-gated variant** that you intend to take forward.

| Gate | Threshold (from §2) | Actual | PASS / FAIL |
|---|---|---|---|
| OOS net Sharpe ≥ floor | | | |
| Max drawdown ≤ cap | | | |
| Profit factor ≥ 1.3 | | | |
| OOS net return > 0 | | | |
| Trades ≥ 150 / OOS ≥ 50 | | | |
| OOS÷IS Sharpe ≥ 0.70 | | | |
| Cost retention ≥ 0.50 | | | |
| Top-5-day share ≤ 50% | | | |
| Months positive ≥ 60% | | | |
| Slippage-stress survives | | | |
| *(if AI route)* gate uplift ≥ +0.30 | | | |
| *(if AI route)* gate_adds_value true | | | |
| *(if AI route)* calibration ≥ 55% | | | |
| *(if fine-tune)* Run3 uplift ≥ +0.30 | | | |

**Rule:** every applicable gate must PASS. One FAIL on a primary or statistical-validity gate = **NO-GO for live**. A FAIL only on a gate-value gate = ship **white-box ORB alone**, Kronos stays in shadow.

---

## 5. Decision & sign-off

**Backtest outcome:** ☐ GO (proceed to M7/M8 tiny-live)  ☐ GO, ORB-only (Kronos stays shadow)  ☐ NO-GO (iterate / shelve)

**Variant selected for next stage:** ☐ ORB alone  ☐ ORB + Kronos zero-shot  ☐ ORB + Kronos fine-tuned

**Rationale (2–4 sentences, grounded in the KPI panel):**

`__________`

**Residual risks carried forward (e.g. survivorship ceiling, thin-name slippage):**

`__________`

**This result earns the right to risk small capital under M8 — it is not a forecast of profit.**

| Role | Name | Signature | Date |
|---|---|---|---|
| Operator | | | |
| Reviewer | | | |

> Links: PR `__________` · raw JSON artifacts (`run1.json`/`run2.json`/`run3.json`) committed under `research/backtest/results/` · `Live-Readiness-Checklist.md` updated: ☐
