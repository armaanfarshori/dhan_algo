# F&O Strategy Orchestration Engine — planning context (read before planning)

PIVOT (2026-06-20): project goes F&O-focused. Equity intraday rules are DEAD (10 strategies all lose,
none beats ORB; Kronos gate doesn't rescue them). The EDGE is on the options side: defined-risk
premium-selling, vol-gated.

## Backtest evidence (weekly NIFTY, ROM = return-on-SPAN-margin; vol = vol-gated)
GO candidates (the "winning strategies" to orchestrate):
- iron_condor:        gated 3.91% GO (ungated 2.22%)
- bull_put_spread:    gated 7.19% GO (ungated NO-GO)
- credit_put_spread:  gated 2.70% GO
- broken_wing_condor: gated 2.67% GO
The vol-gate (ml/fno_vol_gate.py, k≈0.9) ADDS edge on options (opposite of equity).
NO-GO: undefined-risk (short straddle/strangle, jade_lizard, ratio — big net but tail-blind/low ROM),
directional + long-premium (bear spreads, long_straddle).

## What we're building
A STRATEGY ORCHESTRATION ENGINE: a regime-aware router that, per cycle (and per index), PICKS which
GO strategy to deploy (or stand aside) based on the regime — vol-gate state, VRP (VIX vs realized),
IV rank/level, trend, DTE. The novelty is the SELECTION layer, not the legs. Sits ON TOP of fno's
existing multi-leg engine (research/backtest/fno_strategies.py) + vol-gate. Defined-risk only.

## HARD REALITIES (plan around these — do not assume away)
1. DATA = NIFTY ONLY. index_bars/option chains exist for NIFTY (id 13) + India VIX (id 21) only.
   BANKNIFTY/FINNIFTY/MIDCPNIFTY/SENSEX/BANKEX have NO data. Multi-index = (a) make the engine
   index-AGNOSTIC now (per-index lot/step/expiry-calendar/data-source registry), (b) a DATA INGESTION
   plan (Dhan API per index; historical option chains likely UNAVAILABLE → those indices forward-only).
   The "quick backtest" runs on NIFTY now; other indices only once ingested.
2. EDGE IS PRELIMINARY: rests on VIX-as-weekly-IV proxy (regime-dependent), close-not-FSP settlement,
   EXPIRY-ONLY (tail-blind for undefined risk). Real-IV forward paper-log is the truth test. PAPER only.
3. ROM (return-on-SPAN-margin) is the headline metric, not return-on-capital. Defined-risk only for live.

## Lanes: fno owns fno_strategies.py + the data foundation + real-IV validation. We build the
## orchestration layer + index-agnostic generalization on top. Coordinate via the bus; don't edit
## fno_strategies.py internals (call it).

## Deliverable per planning agent: write ONE focused spec .md in this dir (orchestrator_specs/).
