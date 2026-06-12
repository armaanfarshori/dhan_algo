# Backtesting

`research/backtest/` is an event-driven backtester built for one purpose: producing evidence trustworthy enough to justify (or veto) live trading. Its design choices are all anti-self-deception measures.

## Design principles

| Principle | Implementation |
|---|---|
| **Same strategy code** | The backtester instantiates the identical `strategies/orb.py` class the live engine runs — there is no "backtest version" to drift |
| **No lookahead** | Decisions made on bar *t* fill at bar *t+1*'s **open**. A test with a spy asserts the engine never reads a future bar |
| **Real costs** | Full Indian intraday stack per round trip (see below) — an earlier backtester with zero costs and same-bar fills produced beautiful, useless equity curves |
| **Point-in-time universe** | The screener is replayed *as of each historical date* (`time < as_of`), using daily bars — today's watchlist is never projected into the past |
| **Survivorship-safe** | Delisted/suspended instruments stay in the universe; the raw DB keeps everything |
| **Honest Sharpe** | Computed from **daily** returns × √252 — never from per-minute returns, which inflates it absurdly |

## Cost model (`research/backtest/costs.py`)

Per executed order / round trip on NSE intraday equity:

| Charge | Rate |
|---|---|
| Brokerage | min(₹20, 0.03%) per order |
| STT | 0.025% on the sell side |
| Exchange transaction | 0.00297% |
| SEBI turnover | 0.0001% |
| Stamp duty | 0.003% on the buy side |
| GST | 18% on brokerage + exchange |
| Slippage | Configurable bps, applied adversely |

On thin names, costs routinely consume ~20% of gross P&L — which is precisely the information a costless backtest hides. A known limitation queued for hardening: flat-bps slippage still understates low-priced names (tick-aware slippage is on the roadmap).

## Usage

```bash
# ORB alone, screener-picked top-5, one month
python -m research.backtest --from 2026-05-01 --to 2026-06-01 --n 5

# Fixed security ids
python -m research.backtest --from 2026-05-01 --to 2026-06-01 --ids 1333,11536

# With the Kronos zero-shot gate in the loop
python -m research.backtest --from 2026-05-01 --to 2026-06-01 --gate kronos

# Machine-readable output
python -m research.backtest --from 2026-05-01 --to 2026-06-01 --json
```

The report includes: total/annualized return, daily-return Sharpe, max drawdown, win rate, profit factor, gross-vs-net P&L (cost drag made explicit), and per-security breakdown.

## The decision study (M3)

The go/no-go experiment, run over the same 2-year window with identical costs:

| Run | Config | Question it answers |
|---|---|---|
| 1 | ORB standalone | Does the rule-based strategy have an edge at all? |
| 2 | ORB + Kronos zero-shot | Does a pre-trained model add value on NSE? |
| 3 | ORB + Kronos fine-tuned | Does NSE-specific training improve it further? |

Decision rules: no live trading unless Run 1 or 2 clears the bar; promote the fine-tuned model only if Run 3 shows a meaningfully better Sharpe than Run 2.

## Data prerequisites

The study needs the full historical backfill (5 years × ~9,470 NSE equities of 1-minute bars) and the clean replica (M2.5: liquid-only, corporate-action-adjusted — training or testing on split gaps and circuit-breaker days corrupts both models and conclusions).
