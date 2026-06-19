# F&O — revalidation & ORB-vs-condor head-to-head plan (2026-06-19)

**Context.** The first NIFTY iron-condor backtest gave a **preliminary GO** (1.5×: 90.9% win,
PF 1.74, +₹36.4k after costs over 2022–2026), with a clearly-present variance risk premium
(India VIX > NIFTY realized on 100% of SELL days, +3.8 vol pts). But it used three *deliberately
conservative* approximations. This plan turns that into a **decision-quality** result and a fair
comparison against the equity ORB/Kronos track — **before** any Phase-2 strategy or live build.

**Hard rules unchanged:** PAPER stays true; read-only Dhan via the cached token (never mint a
session); off-hours only; branch+PR; coordinate shared infra (DB downgrade) with the fine-tune
session on the bus.

---

## Why the current GO is only "preliminary" (the gaps to close)

| # | Approximation used | Bias | What it hides |
|---|---|---|---|
| 1 | India VIX (~30d) as the weekly straddle IV | conservative (understates weekly IV → tight shorts, low credit) | true per-expiry pricing & strike placement |
| 2 | Settlement = index daily **close** (not NSE **FSP** 15:00–15:30 avg) | ambiguous | correct win/loss on near-the-money expiries |
| 3 | Entry at **prior-expiry close** (not next-morning), synthetic ISO-week cycles | slightly optimistic | real entry fills + exact expiry alignment |
| 4 | P&L reported on notional ₹2L (ROC ~3.8% CAGR) | wrong denominator | **return on deployed SPAN margin** — the real profitability |

Net of 1–3 is *conservative*, so the real edge is plausibly ≥ measured — but unconfirmed. #4 is
the one that actually answers "is this profitable?" — and it's not yet computed.

---

## Workstreams

### A. Real per-expiry ATM IV (replaces the VIX proxy) — **STARTED**
- **Forward (now):** the EOD collector (`core/fno_collector.py`, cron) snapshots the NIFTY chain
  post-close into `option_chain_snapshot` + the ATM projection into `option_atm_iv`. Real weekly
  IV accrues from day 1. Usable for a forward/paper test within weeks.
- **Backward (faster history) — FEASIBILITY TO VERIFY FIRST:** the *intent* is, for
  **currently-listed** option contracts, to pull each contract's historical OHLCV via
  `charts/historical` (NSE_FNO/OPTIDX, security_id from `fno_instruments`) and **derive IV via
  Black-76** from option LTP + spot + rate + dte. **But Dhan may not serve per-contract option
  OHLCV historically** (handoff §8 Open Q#1 is still open; the option-chain endpoint is
  snapshot-only). So step 0 is a one-call probe: `get_daily_historical(<an OPTIDX security_id>,
  "NSE_FNO", "OPTIDX", …)` and confirm it returns bars. If yes → a few months of *real* weekly IV
  immediately (expired contracts are gone, so not 2yr). If no → rely solely on the forward
  collector accruing IV over time. Do not scope work against A-backward until the probe passes.
- **Deliverable:** re-run the backtest with `samples_from_db(source="atm")` + cycles priced from
  real ATM IV; quantify the VIX-proxy bias (expected: real edge ≥ VIX-based).

### B. NSE Final Settlement Price (FSP) for expiry resolution
- Source the weekly NIFTY **FSP** series (NSE F&O bhavcopy / settlement files; the 15:00–15:30
  weighted average). Add an `expiry_settlement` table (or column) and use it as `expiry_spot` in
  `cycles_from_db`/`run_backtest` instead of the daily close.
  - **Sourcing format TBD — verify first:** NSE's FSP lives in the daily F&O bhavcopy (the
    settlement-price column); the file format changed across 2022–2026 and 2yr means ~200+ daily
    files. Pin the exact URL pattern + the settlement-price column name before implementing, and
    handle gaps/holidays. Treat this as a manual verification step, not a given.
- **Deliverable:** re-resolve all cycles on FSP; report how many near-the-money win/loss flips occur
  and the P&L delta.

### C. Capital efficiency / return-on-margin — **the profitability answer**
- Compute **SPAN+exposure margin** per condor (or, as a defined-risk lower bound, use max-loss =
  (wing_width − credit)×lot as the capital-at-risk). Report **return on deployed margin** and a
  **sizing-to-target-risk** view (e.g. risk 1–2% of capital per cycle), not return on notional ₹2L.
- Quantify the **hedged-vs-naked** SPAN advantage (the original thesis: condor margin ≪ naked
  strangle) — this is the structural edge that makes the strategy scalable.
- **Caveat:** `max_loss` is a *conservative* (high) proxy for capital-at-risk — the exchange's actual
  SPAN on a hedged condor is typically ~40–60% of max_loss (it recognises the long wings), so a
  return-on-margin computed off max_loss **understates** true capital efficiency. Compute the real
  SPAN where possible; treat the max_loss-based ROM as a floor.
- **Deliverable:** a return-on-margin + max-DD-on-margin table; the number that decides allocation.

### D. ORB/Kronos vs condor — head-to-head (after the fine-tune session's M3)
- The fine-tune session runs **M3** (ORB vs +zero-shot Kronos vs +fine-tuned) on `dhan_clean`.
- Compare both tracks on **common, risk-adjusted, capital-efficiency** metrics over a common period:
  Sharpe/Sortino, max-DD, **return on deployed margin**, win-rate, turnover, tail risk.
- **Key question — complementarity, not just competition:** ORB is *directional/intraday*; the condor
  is *short-vol/weekly*. Different risk factors → running **both** may diversify (lower combined DD)
  rather than one replacing the other. Decide allocation accordingly.
  - **Tail caveat:** the diversification holds in the *average* regime, but in a market-wide
    crash/gap the two can lose **together** (short strikes blown through *and* no clean ORB breakout +
    wide slippage). Measure the **joint left-tail** (worst combined weeks), not just average
    correlation, before assuming the combo lowers risk.
- **Deliverable:** a one-page comparison + an allocation recommendation.

### E. NIFTY-vol Kronos (replace the persistence proxy) — later
- The gate currently uses `realized_vol_20d` (persistence) as the predicted-vol proxy. Once the
  equity-Kronos pipeline is free, train a **NIFTY-vol model** (reuse `prepare_kronos_dataset →
  finetune.py`, export `index_bars`; checkpoint `kronos/checkpoints/nifty-vol-v1/`, model-only).
  Kronos forecasts a price *path* → derive predicted vol from forecast dispersion. Only worthwhile
  if A–D clear; it sharpens the gate, it isn't the edge itself.

---

## Sequencing & dependencies

```
NOW ───────────────► collector cron live (A-forward accrues; idempotent, off-hours)
A-backward (Black-76 on listed contracts) ──┐
B (FSP series)                              ├──► REVALIDATE backtest (real IV + FSP)
C (return-on-margin)  ──────────────────────┘         │
                                                      ▼
fine-tune M3 (their track) ───────────────► D head-to-head + allocation decision
                                                      │
                                                      ▼ (only if it clears)
                                            E NIFTY-vol Kronos → Phase-2 strategy build
```
- A-forward needs no one; A-backward + B + C can proceed in parallel off-hours.
- D waits on M3 (fine-tune session — coordinate via bus; also their DB-downgrade window).
- No live order path until D clears **and** a paper-forward period confirms the backtest.

## Decision gate for LIVE consideration (stricter than the preliminary GO)
ALL of: positive expectancy after costs on **real ATM IV + FSP**; max-DD < 15% of allocated capital
**and** acceptable on a return-on-margin basis; edge robust across sub-periods (not one regime);
a **paper-forward** stretch (≥ 1–2 months on the live collector data) confirms the backtest; and the
hedged-margin efficiency holds. Negative on real data → widen wings / cut frequency / raise k, or
shelve. (Handoff §7 fallbacks apply.)

## Risks / unknowns to keep honest about
- Expired option contracts aren't in Dhan's master → true multi-year *real-IV* history is not
  retrievable; A-backward is months, not years (forward accrual fills the rest over time).
- FSP file availability/format (verify the NSE source).
- Liquidity/slippage on the OTM wings in size (the ≥0.5% assumption needs a fills check before live).
- Single-underlying (NIFTY) only; BANKNIFTY/FINNIFTY are a later add, not now.
