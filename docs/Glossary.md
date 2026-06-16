# Glossary — DhanAIBot (`dhan_algo`)

Every abbreviation, acronym, and domain term used across the codebase and documentation, grouped by topic. Definitions are grounded in how *this project* uses each term. Entries are alphabetical within each section.

---

## 1. Trading and Strategy

| Term | Expansion / Meaning |
|---|---|
| **ADV** | Average Daily Volume. The mean share count traded per day over a rolling window (~20 trading days / 30 calendar days). `RiskEngine` caps position size to 1% of ADV (`adv_participation_pct`) to ensure the live market can absorb the order. |
| **ATR** | Average True Range. A volatility measure (difference between daily high and low, adjusted for gaps). Not computed directly in code, but `ATR%` (ATR as a percentage of price) is the ranking metric the NSE screener uses to select high-volatility candidates. |
| **ATR%** | ATR expressed as a fraction of the stock's price. The screener ranks all NSE_EQ instruments by ATR% so the watchlist gravitates toward volatile, tradeable names. |
| **Breakout** | Price closes above the Opening Range high — triggers a long (BUY) entry in ORB. |
| **Breakdown** | Price closes below the Opening Range low — triggers a short (SELL) entry in ORB. |
| **bps** | Basis points. 1 bps = 0.01%. Used for paper slippage (`paper_slippage_bps = 2.0`), ATR% floors, and transaction cost analysis. |
| **Drawdown** | The peak-to-trough decline of the equity curve. Reported as `max_drawdown_pct` in backtest reports; computed over the cumulative daily P&L series. |
| **EOD** | End of Day. Used specifically for the unconditional square-off that flattens all open positions before market close (15:15 IST, 15 minutes before the 15:30 IST close). This fires regardless of strategy state, gate mode, or whether the OR is known. |
| **Entry** | The opening side of a trade (BUY for long, SELL for short). Recorded in the `trades` table via `log_trade_entry`. |
| **Equity** | Current capital = starting capital + all-time realized P&L (from the `trades` table). All risk limits are fractions of this, so sizing compounds gains and shrinks in drawdown. |
| **Exit** | The closing side of a trade. Recorded via `log_trade_exit`. Exits are never blocked by the risk engine's exposure limits (`check_intent` returns True for exits unconditionally). |
| **Fill** | A confirmed executed order. In paper mode, a simulated fill at the reference price ± slippage; in live mode, the broker's actual average fill price returned by `get_order_by_id`. Represented by the `Fill` dataclass in `engine/types.py`. |
| **Flat** | A position with zero net quantity (neither long nor short). `Position.is_flat` in `engine/types.py`. |
| **Gross P&L** | P&L before costs (brokerage, STT, fees, slippage). `total_gross` in the backtest report. |
| **Intraday** | Within a single trading session (09:15–15:30 IST). All positions are opened and closed on the same day; no overnight exposure. The `product_type` field in orders is `"INTRADAY"`. |
| **Kill switch** | An emergency halt triggered by `POST /api/killswitch`. Writes `run/killswitch`; `RiskEngine` detects the file within ~10 seconds, halts trading, and flattens all positions. The `RiskEngine` is the only component permitted to halt. |
| **Live mode** | `PAPER_TRADING=false`. Orders go to the real Dhan production account. Requires `ALLOW_LIVE_TOGGLE=true` plus a restart — not activatable by a single POST request. |
| **Long** | A BUY position (profits if price rises). |
| **LTP** | Last Traded Price. The most recent trade price returned by `marketfeed/ltp` or the WebSocket feed. Used for unrealized P&L calculation and ORB tick evaluation. |
| **LTT** | Last Traded Time. The exchange timestamp embedded in WebSocket tick packets (decoded as `HH:MM:SS` by the `dhanhq` library). `BarBuilder` uses LTT to stamp bars with exchange time rather than server receive time. |
| **Net P&L / Net PnL** | P&L after all costs (brokerage, STT, exchange fees, SEBI fee, stamp duty, GST, slippage). The primary metric in backtest reporting. |
| **Notional** | The total value of a position at current price (qty × price). Capped at 20% of equity per trade (`max_notional_per_trade_pct`). |
| **OHLC** | Open, High, Low, Close. The four price points that summarize a bar (candlestick). |
| **OHLCV** | Open, High, Low, Close, Volume. OHLC plus volume — the standard candlestick representation. The `bars` hypertable stores 1-minute OHLCV rows; Kronos receives OHLCV(+amount) as input. |
| **OR** | Opening Range. The high/low band formed during the first `orb_range_minutes` (default 15) of the trading session (09:15–09:30 IST). `or_high` and `or_low` in `strategies/orb.py`. |
| **or_locked** | Boolean flag in `ORB` indicating the Opening Range window has closed and the high/low are fixed. Set to `True` at 09:30 IST; not unset until the next session reset. |
| **ORB** | Opening Range Breakout. The only active strategy. Price breaking above `or_high` → long entry; below `or_low` → short entry. Pure synchronous class in `strategies/orb.py`; used identically by the live runner and the backtester. |
| **or_range** | The width of the Opening Range (`or_high − or_low`). Used to set the target (`entry ± 1.5 × or_range`) and to filter out too-narrow ranges (`min_range_pct`). |
| **Paper trading** | Simulated trading with no real orders. The hard default (`PAPER_TRADING=true`). `PaperExecutor` applies adverse slippage and journals identically to live mode. |
| **P&L / PnL** | Profit and Loss. Used in realized (closed trade) and unrealized (open position marked-to-market) forms. |
| **Profit factor** | Total winning P&L divided by total losing P&L (absolute). A profit factor > 1 means the strategy makes more on wins than it loses. Reported in `research/backtest/report.py`. |
| **Realized P&L** | P&L locked in by closed trades (status = 'CLOSED' in `trades` table). The `RiskEngine` reads this from the DB every monitoring cycle. |
| **Round trip** | One complete trade: an entry fill plus the corresponding exit fill. `research/backtest/costs.py` models the full cost of one intraday round trip. |
| **Sharpe ratio** | Risk-adjusted return = mean(daily returns) / std(daily returns) × √252. Annualized from *daily* net P&L in the backtester — not from per-bar equity points. |
| **Short** | A SELL position (profits if price falls). Entered on ORB breakdown. |
| **SL / Stop-loss** | Stop-Loss. The price at which the strategy exits a losing position. For a long ORB trade: `or_low × (1 − sl_buffer_pct)`. |
| **Slippage** | The difference between the expected fill price and the actual fill price. `PaperExecutor` applies adverse slippage: BUY fills at `ref_price × (1 + bps/10000)`, SELL fills at `ref_price × (1 − bps/10000)`. |
| **Square-off** | Closing all open positions before end of day. The ORB EOD square-off fires at 15:15 IST unconditionally. |
| **Stop distance** | The absolute price difference between entry and stop-loss. Used by `RiskEngine.size_position` to compute position size: `qty = risk_budget / stop_distance`. |
| **Target / TP** | Take-profit price. For a long: `entry + 1.5 × or_range`. For a short: `entry − 1.5 × or_range`. |
| **Unrealized P&L** | Mark-to-market value of open positions at the current LTP. Included in the daily loss budget check (day_total = realized + unrealized). |
| **VWAP** | Volume-Weighted Average Price. Not directly computed or used as a signal, but referenced conceptually in market-regime analysis in `hermes_skills/`. |
| **Win rate** | Percentage of trades that close with positive net P&L. `Report.win_rate` in `research/backtest/report.py`. |

---

## 2. Indian Market Specifics

| Term | Expansion / Meaning |
|---|---|
| **Brokerage** | Commission charged by the broker per order. Dhan charges ₹20 or 0.03% of turnover, whichever is lower, per executed order. Modelled in `research/backtest/costs.py`. |
| **BSE** | Bombay Stock Exchange. India's other major exchange (after NSE). In the Kronos pre-training corpus at 5T–W granularity; not traded by this platform. |
| **Circuit / Freeze** | A price band limit set by NSE/SEBI. A security in circuit cannot trade outside its daily price band. Mentioned in Kronos training data notes; not directly handled in platform code. |
| **Expiry** | The date on which F&O contracts expire (typically last Thursday of the month for NSE). The `hermes_skills/options_expiry_watch/` script monitors upcoming expiry dates. |
| **F&O** | Futures and Options. Derivatives segment on NSE. The platform currently trades only NSE_EQ (equity); `core/charges.py` contains a separate F&O options cost calculator. |
| **GST** | Goods and Services Tax. 18% applied to (brokerage + exchange fee + SEBI fee) on every trade. Modelled in `research/backtest/costs.py`. |
| **GTT** | Good Till Triggered. A conditional order type on Dhan that persists until a price condition is met. Also called a "Forever Order." `DhanClient.create_forever_order` with `order_flag="SINGLE"`. |
| **Lot size** | The minimum tradeable unit for F&O contracts. Stored in the `instruments` table (`lot_size` column, default 1 for equities). |
| **NSE** | National Stock Exchange of India. The primary exchange this platform trades. Market hours: 09:15–15:30 IST. |
| **NSE_EQ** | NSE Equity segment. The `exchange_segment` value for ordinary listed equities. The platform's target universe: `watchlist_exchange_segment = "NSE_EQ"`. |
| **NSE_FNO** | NSE Futures and Options segment. The `exchange_segment` value for derivatives. Referenced in API documentation; not actively traded by this platform. |
| **NSE_IDX** | NSE Index segment. The `exchange_segment` for index instruments (e.g. NIFTY 50). Not valid for direct trading; the screener's instrument validation rejects IDX entries in the equity universe. |
| **OCO** | One-Cancels-Other. A paired order type (stop-loss + target) where filling one leg cancels the other. `DhanClient.create_forever_order` with `order_flag="OCO"`. |
| **RBI** | Reserve Bank of India. Central bank; policy announcements can cause sharp intraday moves. Mentioned as a scheduled-event calendar filter to implement post-backtest. |
| **SEBI** | Securities and Exchange Board of India. Market regulator. The SEBI fee is ₹10 per crore of turnover (0.0001%), applied to both sides. Modelled in `research/backtest/costs.py`. |
| **Scrip master** | The complete instrument reference list published by Dhan (all tradeable securities with SecurityId, ticker, segment, instrument type). Loaded via `backfill.py --instruments` and stored in the `instruments` table. The screener validates every candidate against it to reject index scrips and non-equity instruments. |
| **SecurityId** | Dhan's numeric identifier for a tradeable instrument (e.g. `"2885"` for RELIANCE). Must be passed as a **string** in WebSocket subscribe messages — an integer is silently accepted but never streams data. |
| **Stamp duty** | Tax on the buy side only: 0.003% of buy-side turnover for intraday equity. Modelled in `research/backtest/costs.py`. |
| **STT** | Securities Transaction Tax. 0.025% of turnover on the sell side for intraday equity. Levied by the Indian government; modelled in `research/backtest/costs.py`. |
| **Union Budget** | Annual Indian government budget announcement. A high-impact scheduled event that can cause extreme intraday volatility. Mentioned as a calendar filter to implement. |

---

## 3. Dhan API and Data

| Term | Expansion / Meaning |
|---|---|
| **charts/historical** | Dhan REST endpoint for end-of-day OHLCV bars. Accessed via `DhanClient.get_daily_historical`. |
| **charts/intraday** | Dhan REST endpoint for intraday OHLCV bars (up to 5 years of 1-minute data, accessed directly via REST rather than the SDK which only returns 5 days). Rate-limited to ~1 req/s; burst calls return DH-904. Used by `backfill.py` for historical backfill and by `apps/trader.py` for OR seeding on mid-session restart. |
| **correlation_id** | A client-supplied idempotency key (up to 20 hex chars) attached to each order placement. `DhanClient.place_order` auto-generates one via `uuid4().hex[:20]` if not supplied. Used to look up an order via `get_order_by_correlation_id` if the placement response is lost. |
| **DH-807** | Dhan API error code. Treated as an auth error in `AUTH_ERROR_CODES` — triggers token refresh. |
| **DH-901** | Dhan API error code: expired or invalid access token. Triggers `handle_auth_error` → token refresh in `MasterTokenManager`. The leading cause of multi-day backfill failures (stale in-memory token). |
| **DH-902** | Dhan API error code. Treated as an auth error — triggers token refresh. |
| **DH-904** | Dhan API error code: burst rate limit exceeded on `charts/intraday`. Returned when requests come faster than ~1/s. The backfill spaces requests at ~1.2 s to avoid this. |
| **DH-905** | Dhan API error code: invalid or non-retryable request (bad parameters, unsupported operation). Not a transient error — retrying will not help. |
| **DhanHQ v2** | The DhanHQ REST + WebSocket trading API version 2. Base URL: `https://api.dhan.co/v2`. No sandbox exists; all calls hit production. |
| **fundlimit** | Dhan REST endpoint (`GET /fundlimit`) that returns available cash and margin. Used by `DhanClient.get_funds` and surfaced in the dashboard's funds panel. |
| **marketfeed/ltp** | Dhan REST endpoint for Last Traded Price for up to 1000 instruments (rate category: `quote`). |
| **marketfeed/ohlc** | Dhan REST endpoint for OHLC + LTP for up to 1000 instruments. |
| **marketfeed/quote** | Dhan REST endpoint for full market depth (bid/ask ladder), OI, and OHLC. |
| **non_trading** | Dhan API rate category for non-order, non-market-data endpoints (order book, trade book, holdings, positions, funds). Rate limit: 20 req/s; no daily cap. |
| **orders** | Dhan API rate category for all order-placement/modification/cancellation endpoints. Limits: 10/s, 250/min, 1000/hour, 7000/day. |
| **Postback** | An inbound webhook from Dhan delivered to `POST /postback`. Carries order-status updates (fill, rejection, cancellation). Currently logged only; not wired into reconciliation (QA finding M6). HMAC-SHA256 verification is enabled when `DHAN_WEBHOOK_SECRET` is set. |
| **quote** | Dhan API rate category for `marketfeed/*` endpoints. Limit: 1 req/s. |
| **REST** | Representational State Transfer. The synchronous HTTP API used for order management, historical data, and market data polling. Complements the WebSocket feed. |
| **Token / Access token** | A short-lived authentication credential (JWT-style) issued by Dhan upon login. Generated via PIN + TOTP. Cached in `dhan_token.json` (atomically); refreshed ~30 minutes before expiry by `MasterTokenManager.run()`. |
| **TOTP** | Time-based One-Time Password. A 6-digit code derived from `dhan_totp_secret` via `pyotp.TOTP.now()`. Required alongside the PIN to generate a new Dhan access token. |
| **WS / WebSocket** | The Dhan market data streaming protocol. `LiveFeed` subscribes to `Quote` packets that carry LTP, LTT, and intrabar OHLCV. SecurityId must be a string in the subscribe payload — an integer is silently ignored and never streams. |
| **data** | Dhan API rate category for historical data endpoints (`charts/intraday`, `charts/historical`, `optionchain`). Limits: 5 req/s, 100,000/day. |

---

## 4. Time and Timezones

| Term | Expansion / Meaning |
|---|---|
| **DST** | Daylight Saving Time. Not applicable to IST. IST is a fixed UTC+5:30 offset with no seasonal change — all IST-aware code uses `ZoneInfo("Asia/Kolkata")` safely. |
| **Epoch** | Unix timestamp (seconds since 1970-01-01T00:00:00 UTC). Dhan's `charts/intraday` REST endpoint returns bar timestamps as Unix epoch seconds. Converted to IST via `datetime.fromtimestamp(ts, IST)` in `apps/trader.py`. |
| **IST** | Indian Standard Time. UTC+5:30, no DST. Used for all trading-logic timestamps (market open/close checks, ORB window, EOD square-off). `ZoneInfo("Asia/Kolkata")` throughout the codebase. |
| **TIMESTAMPTZ** | PostgreSQL timestamp with time zone (`TIMESTAMP WITH TIME ZONE` in SQL; `TIMESTAMP(timezone=True)` in SQLAlchemy). All hypertable time columns use this type; PostgreSQL stores them as UTC epoch internally. |
| **UTC** | Coordinated Universal Time. The storage timezone for all DB timestamps. The EC2 box running PostgreSQL is set to UTC; there is no calendar offset between wall-clock and storage. |

---

## 5. ML and Kronos

| Term | Expansion / Meaning |
|---|---|
| **AAAI** | Association for the Advancement of Artificial Intelligence. Kronos was accepted at AAAI 2026 (Shi et al., "Kronos: A Foundation Model for the Language of Financial Markets", arXiv:2508.02739). |
| **BSQ** | Binary Spherical Quantization. The tokenization method Kronos uses. Each OHLCV bar's continuous latent representation is projected onto 20 learnable hyperplanes; the sign of each projection becomes one bit, giving a 20-bit binary code split into two 10-bit subtokens (coarse + fine) with an implicit vocabulary of ~1M tokens without codebook collapse. |
| **Calibration** | The two-stage process in `ml/calibration.py` that measures whether the Kronos gate adds trading value. Stage 1 (`fill`) writes realized 30-minute forward returns back into `signals.features_snapshot`; Stage 2 (`report`) computes accuracy metrics on fresh-data rows and issues an ARM/DO NOT ARM recommendation. Re-arm criterion: ≥30 fresh rows, ≥55% directional accuracy. |
| **Confidence** | A [0, 1] score from `_directional_confidence()`: `1 − clamp(std(pred_close) / price × 10, 0, 1)`. High confidence = Monte-Carlo samples agree tightly. The gate threshold is `kronos_min_confidence = 0.4`; in enforcing mode, trades with confidence below this are blocked. |
| **data_age_min** | Minutes since the most recent bar in the DB at scoring time. Reported in every gate verdict and persisted in `features_snapshot`. Decisions scored on bars older than 15 minutes (`STALE_AFTER_MIN`) are flagged stale and excluded from calibration. |
| **Enforcing mode** | `KRONOS_SHADOW_MODE=false`. The gate actually blocks ORB entries when the model disagrees or has low confidence. Not active until calibration confirms the gate adds value. |
| **Fine-tune** | Domain-specific additional training of Kronos-base on clean NSE 1-minute bars, using a date-split (train ≤ 2024, val 2025, test 2026). Planned post-M2.5. Checkpoint goes to S3; `KRONOS_CHECKPOINT` env var activates it. |
| **IC** | Information Coefficient. A measure of the correlation between a model's forecasts and actual outcomes (−1 to +1). The Kronos paper reports zero-shot IC of ~0.03–0.06 on NSE equities — small but positive. |
| **Kronos** | "Kronos: A Foundation Model for the Language of Financial Markets" (arXiv:2508.02739). A decoder-only Transformer that tokenizes OHLCV candlesticks via BSQ and forecasts future bars via Monte-Carlo sampling. Kronos-small (24.7M params) is the version this platform runs. Vendored under `kronos/`. |
| **Monte-Carlo sampling** | Kronos generates N independent forecast trajectories (`kronos_samples = 10`) by sampling from the model's output distribution. The median of predicted close prices gives the directional forecast; the spread across samples measures confidence. |
| **OOD** | Out-of-Distribution. A model encounters OOD input when the test data differs from the training distribution. NSE equities at 1-minute granularity are OOD for Kronos (which only saw NSE at 5-minute+). Scorer v2 aggregates 1-min bars to 5-min before inference to stay in-distribution. |
| **scorer_version** | A string encoding the scoring configuration (`v2-5min-T0.6-N10-L480-P6`). Persisted in every `signals` row's `features_snapshot`. Calibration groups verdicts by `scorer_version` — outcomes from different configs (timeframe, temperature, N) must never be pooled. A formula or parameter change requires a version bump. |
| **Shadow mode** | `KRONOS_SHADOW_MODE=true` (the default). The gate scores every entry, logs `[SHADOW] ... would ALLOW/BLOCK`, and persists the verdict and features to `signals`, but always returns `True` (never blocks a trade). Builds the calibration dataset without affecting trading. |
| **Temperature (T)** | A softmax scaling parameter controlling how "peaked" the model's token-probability distribution is. Lower T → sharper directional estimates. The Kronos paper's recommended value for price/return forecasting: T=0.6 (vs the earlier platform default of T=1.0). |
| **top_p** | Nucleus sampling threshold. The model samples only from the smallest set of tokens whose cumulative probability exceeds `top_p` (0.90 per the paper). Cuts off the long tail of unlikely predictions. |
| **Zero-shot** | Using a pre-trained Kronos model without any domain-specific fine-tuning. "Zero-shot on NSE" is a slight misnomer here — Kronos has seen ~242M NSE bars in pre-training — but it has never seen NSE at 1-minute granularity and has no post-corpus NSE data. |

---

## 6. Architecture and Platform

| Term | Expansion / Meaning |
|---|---|
| **Alembic head** | The current database schema version identifier. The highest applied migration defines the head. Currently **007** (as of 2026-06-16, 20 tables total). Migration 006 converted `signals.features_snapshot` to `jsonb`; migration 007 added the `api_usage` table. |
| **BarBuilder** | `engine/bar_builder.py`. The single tick-to-candle aggregator in the live path. Receives ticks from `LiveFeed`, accumulates 1-minute OHLCV bars, and flushes them to the `bars` hypertable every 5 seconds. Both the strategy and the DB see identical intrabar OHLC via `LiveFeed.get_ohlc_tick()` reading `BarBuilder.get_current()`. |
| **Continuous aggregate** | A TimescaleDB materialized view that auto-refreshes as new data arrives. Referenced in architecture notes; the platform uses raw hypertable queries rather than continuous aggregates for the main trading path. |
| **dhan-api** | The systemd service running `apps/api.py` on port 8765. Serves the React dashboard and ~30 REST endpoints. Reads the DB and heartbeat file; never touches orders. |
| **dhan-trader** | The systemd service running `apps/trader.py`. Owns all order flow: WebSocket feed, strategy runners, risk engine, executors, portfolio. Writes `run/trader_heartbeat.json` every 5 seconds. |
| **Gate** | Short for Kronos gate (`ml/kronos_gate.py`). Intercepts ORB entry signals and scores them with Kronos. In shadow mode, always allows; in enforcing mode, blocks entries where the model disagrees or has low confidence. |
| **Heartbeat** | `run/trader_heartbeat.json`. Written atomically every 5 seconds by `dhan-trader`. Contains mode, positions, risk state, strategy state, and a UTC timestamp. `dhan-api` reads this file (never the DB) for fast polling endpoints like `/api/snapshot`. Staleness > 60 seconds triggers a Telegram alert. |
| **Hypertable** | A TimescaleDB table automatically partitioned by time into chunks. The platform has 4 hypertables: `ticks`, `bars`, `positions` (via engine_positions), and `equity_curve`. Never run `COUNT(*)` or `ORDER BY time LIMIT 1` on `bars` — it has 300M+ rows and forces chunk decompression. |
| **LiveExecutor** | `engine/execution.py`. The live-mode order executor. Calls `DhanClient.place_order`, then polls `get_order_by_id` until the order reaches a terminal state (TRADED, REJECTED, CANCELLED). Books the broker's actual average fill price. |
| **LiveFeed** | `core/live_feed.py`. Manages the Dhan WebSocket connection, subscribes to Quote packets for all watchlist securities, parses LTT, and dispatches ticks to `BarBuilder` and `StrategyRunner`. |
| **ORB cockpit** | The per-security range-ladder display in the Signals tab of the React dashboard. Shows `or_high`, `or_low`, current LTP, position, entry price, and whether each side has been tried. Named in `README.md`. |
| **PaperExecutor** | `engine/execution.py`. The paper-mode order executor. Fills immediately at `ref_price ± paper_slippage_bps` (adverse: BUY fills high, SELL fills low). Journals orders and fills identically to `LiveExecutor` so paper rehearses the same DB paths as live. |
| **Portfolio** | `engine/portfolio.py`. DB-persisted position store. `apply_fill()` updates `engine_positions` via upsert; `reconcile_on_boot()` restores today's open positions; `reconcile_with_broker()` (live mode only) adopts broker truth and logs any mismatch as CRITICAL. |
| **Reconcile** | The process of aligning the engine's in-memory/DB position state with an external source of truth. Two forms: `reconcile_on_boot()` (from the DB `engine_positions` table, all modes) and `reconcile_with_broker()` (from the Dhan positions API, live mode only). |
| **RiskEngine** | `engine/risk.py`. Pre-trade gate (checks halt, exposure limits, daily budget), position sizer (stop-distance-based with ADV cap), and monitoring loop (DB-sourced P&L, daily/weekly loss halts, kill-switch detection). The sole component permitted to halt trading. |
| **Runner / StrategyRunner** | `engine/runner.py`. Per-security asyncio polling loop. On each poll: fetches tick from feed/REST, calls `ORB.on_tick`, passes `Decision` through `KronosGate` → `RiskEngine` → executor → portfolio. |
| **Screener** | `core/nse_screener.py`. Selects the top-N NSE_EQ securities by ATR% at trader boot, filtered by price floor (₹50), volume floor (50K shares/day), and instrument-type validation against the `instruments` table. Open positions are always included regardless of filter results. |
| **Scorer** | Short for `KronosSignalEngine`. Scores a proposed trade direction and returns `{side, score, confidence, forecasted_return, data_age_min}`. |
| **seed_opening_range** | `ORB.seed_opening_range()`. Called at mid-session restart to reconstruct the opening range from REST intraday bars. Also marks sides that already broke out while the process was down as tried, so the engine never chases a stale breakout. |
| **StatusSpine** | The header/status strip in the React dashboard that shows mode (PAPER/LIVE), gate state, kill-switch button, and the live IST clock. Named in dashboard memory notes. |

---

## 7. Infra, DevOps, and AWS

| Term | Expansion / Meaning |
|---|---|
| **ARM64 / Graviton** | AWS Graviton processor architecture (64-bit ARM). Both EC2 instances (`t4g.small`) use Graviton3 — cheaper and more power-efficient than x86. CI tests on `ubuntu-24.04-arm` as well as x86 to catch ARM-specific issues. |
| **CI/CD** | Continuous Integration / Continuous Deployment. GitHub Actions runs on every push to `main` and every PR: `pytest` (coverage gate 41%), `ruff` lint, and CodeQL scanning. |
| **CodeQL** | GitHub's code scanning engine (static analysis). Runs on Python and JavaScript/TypeScript source in this repo, currently clean. |
| **cron** | The Unix time-based job scheduler. Agent crontab (weekdays): backfill watchdog every 15 min, calibration `fill` + `report` at 16:45 IST, EOD summary at 17:00 IST, `scripts/health_alert.py` every 5 min. |
| **Dependabot** | GitHub's automated dependency-update tool. Active and currently clean on this repo. |
| **DLM** | AWS Data Lifecycle Manager. Creates daily EBS snapshots of the TimescaleDB data volume with 7-day retention. Configured in `infra/storage.tf`. |
| **Docker** | Container runtime. Used locally for the development TimescaleDB instance (`docker compose up -d`). The production TimescaleDB is pinned to `timescale/timescaledb:2.17.2-pg16`. |
| **EBS** | Elastic Block Store. AWS persistent block storage. The TimescaleDB data volume is a separate 30 GB `gp3` EBS volume that survives instance replacement. |
| **EC2** | Elastic Compute Cloud. AWS virtual machine service. The platform uses two EC2 instances: the agent (`t4g.small`, public subnet, Elastic IP) and the DB (`t4g.small`, private subnet). |
| **EIP** | Elastic IP Address. A static public IP attached to the agent EC2. Whitelisted at Dhan's DevPortal for order placement. (Dhan's IP whitelist applies to orders only; data and WebSocket work from any IP.) |
| **IAM** | Identity and Access Management. AWS service for controlling permissions. The EC2 instances have a least-privilege IAM role granting SSM and S3 access only. CI uses a least-privilege `GITHUB_TOKEN` (contents: read only). |
| **IaC** | Infrastructure as Code. `infra/` contains Terraform configurations that provision all AWS resources. Remote state is stored in S3 with DynamoDB lock. |
| **logrotate** | Linux utility that automatically rotates, compresses, and deletes old log files. Used on the agent for `/var/log/dhan/trader.log` and `api.log`. |
| **pre-commit** | A framework for running code quality hooks before each git commit. Configured in `.pre-commit-config.yaml`: ruff lint, `detect-private-key`, and a large-file guard. |
| **ruff** | Fast Python linter and formatter. Used in CI (lint check) and pre-commit. |
| **S3** | Simple Storage Service. Used for: Terraform remote state, TimescaleDB backups, tick archives (transitioned to Glacier after 90 days), and will hold Kronos fine-tuned checkpoints. |
| **SG (Security Group)** | AWS virtual firewall controlling inbound/outbound traffic to EC2 instances. The DB SG allows PostgreSQL only from the agent SG; the agent SG allows SSH and port 8765 only from VPC-internal traffic. |
| **SSM** | AWS Systems Manager Parameter Store. Stores secrets (Dhan credentials, DB password, TOTP secret, PIN) as `SecureString` parameters — never committed to git. |
| **systemd** | Linux service manager. Both `dhan-trader` and `dhan-api` run as systemd services with `Restart=on-failure`, `StartLimitBurst=5`, and `OnFailure=dhan-alert@%n.service` (Telegram alert on crash). |
| **t4g** | AWS EC2 instance family using Graviton (ARM) processors. `t4g.small` = 2 vCPU, 2 GB RAM — the agent instance type. Memory constraint means Kronos is lazy-loaded and RSS is monitored to stay under 1.6 GB. |
| **Tailscale** | A VPN mesh network. The agent dashboard (port 8765) is accessed via Tailscale or SSH tunnel — never directly from the public internet. The bind address `0.0.0.0` is mitigated by Security Group rules; `API_BIND_HOST=127.0.0.1` + SSH tunnel is the recommended hardening. |
| **Terraform** | Infrastructure-as-code tool. All AWS resources (VPC, EC2, EBS, EIP, S3, SSM, IAM, DLM, security groups) are defined in `infra/*.tf`. Remote state uses an S3 backend with DynamoDB lock. |
| **TimescaleDB** | A PostgreSQL extension for time-series data. The platform runs `timescale/timescaledb:2.17.2-pg16` (pinned). Provides hypertables (auto-partitioned by time), chunk compression, and time-series aggregation functions. |
| **VPC** | Virtual Private Cloud. The isolated AWS network containing both EC2 instances. The DB instance has no public IP; the agent reaches it via the VPC's private subnet. |

---

## 8. Security

| Term | Expansion / Meaning |
|---|---|
| **CORS** | Cross-Origin Resource Sharing. HTTP mechanism allowing browsers to call APIs from different origins. `apps/api.py` applies CORS middleware with `Authorization` in `Access-Control-Allow-Headers`. |
| **DASHBOARD_TOKEN** | A shared-secret environment variable. When set, the `_check_auth` helper requires an `X-Dashboard-Token` or `Authorization: Bearer` header matching this value on mutating POST endpoints (`/api/killswitch`, `/api/watchlist/refresh`). Fail-open if unset (to prevent locking the operator out of the kill-switch). Superseded by M6 session auth when implemented. |
| **HMAC** | Hash-based Message Authentication Code. `apps/routes/system.py` verifies `POST /postback` requests by computing `HMAC-SHA256(DHAN_WEBHOOK_SECRET, raw_body)` and constant-time-comparing against the `X-Dhan-Signature` header. Configured by SEC-09. |
| **JWT** | JSON Web Token. Stateless auth token. Deliberately *not* used for M6 auth — a server-side session (SHA-256 hash in the `sessions` table) is preferred because it can be revoked immediately if a credential is compromised. |
| **MFA** | Multi-Factor Authentication. The `mfa_credentials` table (migration 003) supports WebAuthn and TOTP. Phase 1 of M6 skips MFA (Tailscale provides a network-layer second factor); TOTP is planned for Phase 2. |
| **Partial backend config** | The pattern in `/api/config` where the `client_id` is returned masked (`****<last 4 digits>`) so the dashboard can display useful identifiers without leaking credentials. |
| **SecureString** | An AWS SSM Parameter Store parameter type that encrypts the value at rest using KMS. All Dhan credentials, DB password, and TOTP secret are stored as `SecureString` in SSM. |
| **TOTP** | Time-based One-Time Password (see also section 3). In the auth context (`alembic/versions/003_auth_tables.py`): the `mfa_credentials` table can store an AES-256-GCM-encrypted TOTP secret for dashboard MFA. Planned for M6 Phase 2. |
| **WebAuthn** | Web Authentication standard for passkey/FIDO2 authentication. Supported by the `mfa_credentials` schema (migration 003); planned for M6 Phase 3 (internet-exposed scenario only). |

---

## 9. Project Shorthand

### Milestone IDs

| ID | Meaning |
|---|---|
| **M0** | AWS infrastructure — VPC, EC2 × 2 (agent + DB), EIP, S3, SSM, IAM, Terraform. Done. |
| **M1** | Database schema — 19–20 tables, 4–5 hypertables, alembic migrations 001–007. Done. |
| **M2** | Data pipeline — historical backfill (NSE_EQ, ~67% complete as of 2026-06-16) and live WebSocket feed → BarBuilder → bars. Half done; backfill still running. |
| **M2.5** | Clean data replica — `scripts/build_clean_db.py` builds a survivorship-bias-reviewed, corporate-action-adjusted training copy after full backfill completes. Not started. |
| **M3** | Two-year three-way backtest on clean data (ORB alone vs ORB+zero-shot Kronos vs ORB+fine-tuned Kronos), full Indian intraday cost stack. Requires M2.5. |
| **M4** | Execution engine DB writes — verified that all orders, fills, trades, and equity snapshots land in the DB during a live session. Done. |
| **M5** | Deployment and operations — systemd services, weekday crons, Telegram alerts, heartbeat monitoring, EBS snapshots, logrotate. Done. |
| **M6** | Auth layer for the dashboard API — server-side sessions, Argon2id passwords, audit logging (`auth_events`). Schema exists (migration 003); code not yet implemented. Mitigation: `DASHBOARD_TOKEN` shared secret + SSH tunnel. |
| **M7** | Readonly validation — a systematic verification pass on paper-mode behavior against backtest expectations. Requires M3. |
| **M8** | Tiny live — first real-capital session with minimum position sizes, halved risk limits (`live_risk_scale`), Elastic IP whitelisted, all M0–M7 gates cleared. |

### QA Risk IDs

These identifiers come from `docs/QA-Analysis-Report.md`. The full risk descriptions, locations, and remediation status are documented there.

| ID | Severity | Brief description |
|---|---|---|
| **C1** | Critical | POST `orders` retried on network error with no `correlation_id` — risk of duplicate live orders. |
| **C2** | Critical (fixed) | `log_trade_exit` closed every open row for a security rather than the specific matching row. |
| **H1** | High | Only per-second rate limits enforced; per-hour/day caps defined but not enforced. |
| **H2** | High (fixed) | Non-atomic `.env` rewrite on token refresh could corrupt `.env` mid-write. |
| **H3** | High (fixed) | Position flip not journalled (closing exit recorded but new opposite entry lost). |
| **H4** | High | Unconfirmed-after-backoff order booked at reference price (phantom fill until next restart). |
| **M1** | Medium | Concurrent DH-901 responses could race to generate multiple tokens simultaneously. |
| **M2** | Medium | Bars stamped with server clock instead of exchange LTT. |
| **M3** | Medium | `float(None)` hazard in broker reconciliation on partial-average fields (live only). |
| **M4** | Medium | `_pending` queue in `BarBuilder` unbounded under prolonged DB outage. |
| **M5** | Medium | No tests covering client retry, live-feed reconnect, journal lifecycle, or API endpoints. |
| **M6** | Medium | `/postback` is a no-op and unauthenticated; authoritative fill data discarded. |

### Remediation Checklist Prefixes

Used in `docs/Architecture.md`, `docs/Home.md`, and `docs/Operations-Runbook.md` to label specific improvements by category:

| Prefix | Category |
|---|---|
| **CODE-** | Code quality, structure, or simplification (e.g. CODE-09: API route decomposition). |
| **DATA-** | Data integrity (e.g. DATA-02: one engine per process; DATA-03: entry→exit linkage; DATA-04: single tick aggregator; DATA-05: time-bounded ADV query). |
| **FEAT-** | New platform features (e.g. FEAT-02: cross-process API spend accounting). |
| **OPS-** | Operational improvements (e.g. OPS-08: systemd OnFailure alert; OPS-09: EBS DLM snapshots). |
| **PLAN-** | Deferred planning items (e.g. PLAN-02: gap-scan and repair pass post-backfill). |
| **SEC-** | Security hardening (e.g. SEC-04: DASHBOARD_TOKEN shared-secret; SEC-09: HMAC postback verification; SEC-10: DB password strength warning in live mode). |

### Other Project Shorthand

| Term | Meaning |
|---|---|
| **"the agent"** | The AWS EC2 instance running the trading engine (`dhan-trader` and `dhan-api`), at `/opt/dhan-trading`. Distinct from the concept of an LLM agent. |
| **Hermes** | A retired LLM gateway (OpenRouter-backed) that was used for Telegram alert summarization. Burned its $10 budget in one day of cron sessions (18K-token system prompt per step). Retired 2026-06-11 and replaced by `core/notify.py` (plain Telegram bot API, ₹0/month). The `~/.hermes/` directory and `hermes_skills/` code are left intact but inactive. |
| **_in_window** | Boolean check in `engine/runner.py` that guards market hours (09:15–15:30 IST). The runner only polls strategies when `_in_window` is True. |
| **data_age_min** | See section 5 (ML / Kronos). Surfaced here as it also appears in heartbeat JSON and dashboard displays. |
