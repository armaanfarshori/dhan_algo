# Backtesting

*Last updated: 2026-06-16*

`research/backtest/` is an event-driven backtester built for one purpose: producing evidence trustworthy enough to justify (or veto) live trading. Its design choices are all anti-self-deception measures.

## Design principles

| Principle | Implementation |
|---|---|
| **Same strategy code** | The backtester instantiates the identical `strategies/orb.py` class the live engine runs — there is no "backtest version" to drift |
| **No lookahead** | A decision made on bar *i* (using its close/high/low) executes at bar *i+1*'s **open**, plus adverse slippage. A decision on the session's last bar fills at that bar's close (square-off approximation). |
| **Real costs** | Full Indian intraday cost stack per round trip (`research/backtest/costs.py`) — an earlier backtester with zero costs and same-bar fills produced beautiful, useless equity curves |
| **Point-in-time universe** | `research/backtest/universe.py` reruns the ATR% screener with a hard `time < as_of` cutoff — today's watchlist is never projected into the past |
| **Survivorship CEILING (NOT safe)** | ⚠️ The universe derives from the **current** scrip master (`dhan_clean.clean_universe`) — delisted/suspended names are **absent**. So results are an **optimistic upper bound**, not survivorship-safe. A point-in-time master needs historical scrip masters + delisted OHLCV we don't have from the feed; every result is labelled a ceiling instead (report output + `M3-RESULTS-TEMPLATE.md` §1.3). |
| **Honest Sharpe** | Computed from **daily** net P&L / starting equity × √252 — never from per-minute equity points, which inflated the old backtester's number ~20× |
| **Identical risk math** | `replay_security_day` reuses `engine.risk.RiskEngine.size_position` — the same stop-distance sizing as the live trader, so backtest P&L is denominated in the same units |

## Cost model (`research/backtest/costs.py`)

Full NSE intraday cost stack per round trip (Dhan rates, 2025-26):

| Charge | Rate | Side |
|---|---|---|
| Brokerage | min(₹20, 0.03% of turnover) | per executed order |
| STT | 0.025% | sell side only |
| NSE transaction fee | 0.00297% | both sides |
| SEBI turnover fee | 0.0001% (₹10/crore) | both sides |
| Stamp duty | 0.003% | buy side only |
| GST | 18% | on brokerage + exchange + SEBI fees |
| Slippage | configurable bps (default 2 bps) | applied adversely on fill |

On thin names, costs routinely consume a large fraction of gross P&L — which is precisely the information a costless backtest hides. Known limitation: flat-bps slippage understates low-priced names (tick-aware slippage is on the roadmap).

## Usage

```bash
# ORB standalone, point-in-time screener universe (top 5), one month
python -m research.backtest --from 2026-05-01 --to 2026-06-01 --n 5

# Fixed security IDs (skips the screener)
python -m research.backtest --from 2026-05-01 --to 2026-06-01 --ids 1333,11536

# Run 2 of the three-way comparison: ORB + Kronos zero-shot gate
python -m research.backtest --from 2026-05-01 --to 2026-06-01 --gate kronos

# JSON output for comparing runs
python -m research.backtest --from 2026-05-01 --to 2026-06-01 --json out.json

# Adjust starting equity or slippage
python -m research.backtest --from 2026-05-01 --to 2026-06-01 --equity 500000 --slippage-bps 3.0
```

The report (`research/backtest/report.py`) includes: total/annualized return, daily-return Sharpe (√252), max drawdown (over the daily equity curve), win rate, profit factor, gross-vs-net P&L (cost drag made explicit), and a per-security breakdown. The JSON output additionally includes the full trade list and gate decisions.

## Universe construction (`research/backtest/universe.py`)

Each backtest day its own `point_in_time_universe(as_of=day)` call: ATR%-ranked NSE equities using only bars with `time < as_of`. Since M3 runs on `dhan_clean` (which holds only 1m bars + the `clean_universe` table — no `instruments`, no `1d` rollup), the query derives daily high/low/close/volume **from the 1m bars on the fly** and uses **`clean_universe` membership** in place of the old `instruments` EQUITY join (it is already the validated NSE_EQ liquid set). The `min_avg_volume` floor **defaults to the live 50,000** shares/day (was 10,000) so the backtest never trades names you'd never trade live; `dhan_clean` is itself built at ≥50k, so the pool is already pre-filtered.

> The backtester reads `dhan_clean` via `config.backtest_db_url`, and the **portfolio-level** runner (`research/backtest/portfolio_engine.py`) replays all names against one shared `RiskEngine`+book — finite capital, concurrent-position cap, and the daily-loss kill-switch — see `docs/Backtesting-Framework.md`.

Note: the universe query runs on `1d` bars, not 1-minute — rolling up 1-minute bars for every candidate × 60 days blew the statement timeout while the backfill was writing. Semantics are identical.

## The decision study (M3)

The go/no-go experiment, run over the same 2-year window with identical costs:

| Run | CLI flag | Question it answers |
|---|---|---|
| 1 | `--gate none` (default) | Does the rule-based strategy have an edge at all? |
| 2 | `--gate kronos` | Does the Kronos zero-shot model add value on NSE? |
| 3 | `--gate kronos` + fine-tuned checkpoint | Does NSE-specific fine-tuning improve it further? |

Decision rules: no live trading unless Run 1 or 2 clears a credible Sharpe/drawdown bar; promote the fine-tuned model only if Run 3 shows a meaningfully better Sharpe than Run 2.

The Kronos gate in the backtester (`research/backtest/kronos_gate.py`) is a thin adapter wrapping the same `KronosSignalEngine.score_from_db()` the live gate uses, so the scoring logic is identical across paper, backtest, and live.

## Data prerequisites

The study needs:

1. **Full historical backfill** — `~9,470 NSE equities × 5 years of 1-minute bars` (in progress as of 2026-06-16, ~23% complete, ETA ~2026-06-17)
2. **Clean replica (M2.5)** — `scripts/build_clean_db.py`: liquid names only, corporate-action-adjusted, circuit-breaker days excluded. Training or testing on split gaps and circuit-breaker days corrupts both model accuracy measurements and strategy conclusions.
