# Configuration Reference

All configuration flows through one typed object: `Config` in `config.py` (pydantic-settings). Values come from the environment / `.env`; **nothing else in the codebase calls `os.getenv()`**. A mistyped numeric env var fails loudly at startup instead of deep inside a trading session.

Environment variable names are the field names upper-cased (e.g. `paper_trading` → `PAPER_TRADING`).

> **All risk limits are FRACTIONS of equity (reworked 2026-06-13), not absolute rupees.**

## Dhan credentials

| Field | Default | Notes |
|---|---|---|
| `dhan_client_id` | `"mock"` | Dhan client ID |
| `dhan_access_token` | `"mock"` | ~24 h lifetime; auto-refreshed when PIN + TOTP set |
| `dhan_pin` | `""` | For TOTP token refresh |
| `dhan_totp_secret` | `""` | For TOTP token refresh (`cfg.totp_secret` is a back-compat alias) |

## Trading mode

| Field | Default | Notes |
|---|---|---|
| `paper_trading` | `true` | **The safety default.** Live requires editing `.env` + restart |
| `allow_live_toggle` | `false` | Second factor for going live — without it, live mode cannot be enabled at all. There is no auth layer yet (M6), so live must never be one request away |
| `strategy` | `"orb"` | Active strategy |

## Risk

All values are **fractions of equity**, not absolute rupees. Paper mode and live mode share the same geometry; in live mode every fraction is additionally halved by `live_risk_scale`.

| Field | Default | Notes |
|---|---|---|
| `capital` | `100000.0` | ₹ — live starting capital (equity reference) |
| `paper_balance` | `500000.0` | ₹ — simulated equity in paper mode |
| `risk_per_trade` | `0.005` | 0.5 % of equity at risk per trade; position size = equity × this / stop distance |
| `max_daily_loss_pct` | `0.02` | 2 % — halt and flatten for the day when breached |
| `weekly_loss_pct` | `0.05` | 5 % — halt until next week |
| `max_notional_per_trade_pct` | `0.20` | 20 % of equity — hard notional cap per trade regardless of stop-based size |
| `max_gross_exposure_pct` | `1.00` | Σ\|position notional\| / equity — no implicit leverage |
| `adv_participation_pct` | `0.01` | Quantity ≤ 1 % of 20-day average daily volume |
| `min_stop_distance_pct` | `0.0035` | Stop floor — prevents tiny ORB ranges from producing absurdly large sizes |
| `live_risk_scale` | `0.5` | Live mode halves every risk fraction (M8 training wheels); paper ignores this |
| `max_orders_per_session` | `4` | Per security per day |
| `max_open_positions` | `10` | Portfolio-wide concurrent positions cap |
| `paper_slippage_bps` | `2.0` | Adverse slippage applied to simulated fills (bps) |

## ORB strategy

| Field | Default | Notes |
|---|---|---|
| `orb_range_minutes` | `15` | Opening range window from 09:15 IST |
| `poll_interval` | `20.0` | Seconds between quote polls per runner (staggered to avoid burst rate limits) |
| `trade_quantity` | `1` | Fallback fixed quantity; risk-sized quantity overrides this when the engine is live |

## Watchlist / screener

| Field | Default | Notes |
|---|---|---|
| `watchlist_exchange_segment` | `"NSE_EQ"` | Only this segment is traded |
| `watchlist_n` | `20` | Screener picks top-N candidates by ATR%. More candidates = more breakout shots + faster gate calibration, not more simultaneous risk (the daily budget + `max_open_positions` bound actual holdings) |
| `screener_min_price` | `50.0` | ₹ average-close floor. Without it the ATR% rank selects penny stocks where simulated slippage is fantasy (one tick on a ₹14 stock ≈ 7 bps) |
| `screener_min_avg_volume` | `50000` | Shares/day floor |

There is deliberately **no static watchlist variable**. The screener output is validated against the instrument master at boot (must be EQUITY in the trading segment); securities holding open positions are exempt so they can always be managed to exit.

## Kronos gate

| Field | Default | Notes |
|---|---|---|
| `kronos_tokenizer` | `"NeoQuasar/Kronos-Tokenizer-base"` | HuggingFace tokenizer ID |
| `kronos_model` | `"NeoQuasar/Kronos-small"` | 24.7 M params, lazy-loaded on first use (2 GB RAM host) |
| `kronos_checkpoint` | `""` | Empty = HuggingFace zero-shot; set to an S3 path after fine-tuning |
| `kronos_timeframe` | `"5min"` | Bar granularity fed to Kronos. NSE is in the pre-training corpus at 5-min+ only; `"1min"` is OOD |
| `kronos_lookback` | `480` | Bars of context at `kronos_timeframe` (follows the Kronos paper protocol) |
| `kronos_pred_len` | `6` | Forecast bars (6 × 5 min = 30-min gate horizon) |
| `kronos_samples` | `10` | Monte-Carlo rollouts averaged per verdict |
| `kronos_temperature` | `0.6` | Sampling temperature (Kronos paper: 0.6) |
| `kronos_top_p` | `0.9` | Nucleus sampling p (Kronos paper: 0.90) |
| `kronos_thresh` | `0.001` | Minimum forecasted-return magnitude to consider a signal meaningful |
| `kronos_offline` | `true` | Load from local HF cache only — no network call on every start. A model swap is a deliberate act (clear cache / bump revision), never automatic |
| `kronos_revision` | `""` | Pin a HF commit hash or tag; empty = whatever is cached locally |
| `kronos_min_confidence` | `0.4` | Gate threshold in enforcing mode (confidence < this → block) |
| `kronos_scanner_enabled` | `true` | Optional live scanner used by the dashboard watchlist panel |
| `kronos_shadow_mode` | `true` | **Shadow = score and persist every verdict, block nothing.** Re-arm manually only when calibration shows ≥ 30 fresh-data outcomes with ≥ 55 % accuracy |

## Web / dashboard

| Field | Default | Notes |
|---|---|---|
| `webhook_port` | `8765` | Port `dhan-api` binds on |
| `api_bind_host` | `"0.0.0.0"` | Interface `dhan-api` binds to. Default keeps Tailscale-direct access (`100.x.x.x:8765`); set to `"127.0.0.1"` to restrict to loopback/SSH-tunnel only |
| `dashboard_token` | `""` | Shared secret for mutating POST endpoints (`/api/killswitch`, `/api/watchlist/refresh`). Accepted as `X-Dashboard-Token: <token>` or `Authorization: Bearer <token>`. Empty = unprotected (fail-open so a misconfigured secret never locks the operator out of the kill-switch) |
| `dhan_webhook_secret` | `""` | HMAC-SHA256 secret for the `/postback` Dhan webhook (SEC-09). When set, the handler verifies `X-Dhan-Signature: <hex>` on every incoming postback request. Empty = no verification (back-compat) |

## Telegram alerts

| Field | Default | Notes |
|---|---|---|
| `telegram_bot_token` | `""` | Plain bot-API token; empty disables alerts silently |
| `telegram_chat_id` | `""` | Target chat / group ID |

## Backfill paths

| Field | Default | Notes |
|---|---|---|
| `backfill_checkpoint_path` | `"/opt/dhan-trading/backfill_ckpt_NSE_EQ.json"` | Absolute path to the JSON checkpoint file read by `/api/backfill/status` |
| `backfill_log_path` | `"/tmp/backfill.log"` | Absolute path to the backfill log tailed by `/api/backfill/status` |

Override these only when running backfill outside `/opt/dhan-trading` (e.g. local test runs).

## TimescaleDB

| Field | Default | Notes |
|---|---|---|
| `db_host` | `"localhost"` | |
| `db_port` | `5432` | |
| `db_name` | `"dhan_trading"` | |
| `db_user` | `"trader"` | |
| `db_password` | `"trader123"` | Change in production `.env` |

`cfg.db_url` assembles the SQLAlchemy connection URL automatically from these fields (percent-encoding applied).

## Mode changes in practice

```bash
# paper → live (deliberately laborious):
# 1. edit .env:  PAPER_TRADING=false  ALLOW_LIVE_TOGGLE=true
# 2. sudo systemctl restart dhan-trader
# POST /api/mode is read-only by design and returns 409 until M6 auth exists.
```
