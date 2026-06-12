# Configuration Reference

All configuration flows through one typed object: `Config` in `config.py` (pydantic-settings). Values come from the environment / `.env`; **nothing else in the codebase calls `os.getenv()`**. A mistyped numeric env var fails loudly at startup instead of deep inside a trading session.

Environment variable names are the field names upper-cased (e.g. `paper_trading` → `PAPER_TRADING`).

## Credentials

| Field | Default | Notes |
|---|---|---|
| `dhan_client_id` | `"mock"` | Dhan client ID |
| `dhan_access_token` | `"mock"` | ~24 h lifetime; auto-refreshed when PIN+TOTP set |
| `dhan_pin` | `""` | For TOTP token refresh |
| `dhan_totp_secret` | `""` | For TOTP token refresh |

## Trading mode

| Field | Default | Notes |
|---|---|---|
| `paper_trading` | `true` | **The safety default.** Live requires editing `.env` + restart |
| `allow_live_toggle` | `false` | Second factor for going live — without it, live mode cannot be enabled at all. There is no auth layer yet (M6), so live must never be one request away |
| `strategy` | `"orb"` | Active strategy |

## Risk

| Field | Default | Notes |
|---|---|---|
| `max_daily_loss` | `5000` | ₹ — total (realized + unrealized) portfolio loss that halts and flattens |
| `capital` | `100000` | ₹ — live capital reference |
| `risk_per_trade` | `0.01` | Fraction of equity risked per trade; position size = equity × this / stop distance |
| `paper_balance` | `500000` | ₹ — simulated equity in paper mode |
| `max_orders_per_session` | `4` | Per security per day |
| `max_open_positions` | `10` | Portfolio-wide |
| `max_notional_per_trade` | `100000` | ₹ cap regardless of stop-based size |
| `paper_slippage_bps` | `2.0` | Adverse slippage applied to simulated fills |

## ORB strategy

| Field | Default | Notes |
|---|---|---|
| `orb_range_minutes` | `15` | Opening range window from 09:15 IST |
| `poll_interval` | `20.0` | Seconds between quote polls per runner (staggered to avoid burst rate limits) |

## Watchlist / screener

| Field | Default | Notes |
|---|---|---|
| `watchlist_exchange_segment` | `"NSE_EQ"` | |
| `watchlist_n` | `5` | Screener picks top-N by ATR% |
| `screener_min_price` | `50.0` | ₹ average-close floor. Without it the ATR% rank selects penny stocks where simulated slippage is fantasy (one tick on a ₹14 stock ≈ 7 bps) |
| `screener_min_avg_volume` | `50000` | Shares/day floor |

There is deliberately **no static watchlist variable**. The screener output — and the cached fallback — is validated against the instrument master at boot (must be EQUITY in the trading segment); securities holding open positions are exempt so they can always be managed to exit.

## Kronos gate

| Field | Default | Notes |
|---|---|---|
| `kronos_model` | `NeoQuasar/Kronos-small` | 24.7M params, lazy-loaded on first use (2 GB RAM host) |
| `kronos_checkpoint` | `""` | Empty = HuggingFace zero-shot; set to an S3 path after fine-tuning |
| `kronos_lookback` | `400` | 1-min bars of context |
| `kronos_pred_len` | `30` | Forecast horizon (bars) |
| `kronos_min_confidence` | `0.4` | Enforcing-mode gate threshold |
| `kronos_shadow_mode` | `true` | **Shadow = score + persist every verdict, block nothing.** Re-arm manually only when calibration shows ≥30 fresh-data outcomes with ≥55% accuracy |
| `kronos_scanner_enabled` | `true` | Optional live scanner for the dashboard watchlist panel |

## Infrastructure

| Field | Default | Notes |
|---|---|---|
| `webhook_port` | `8765` | dhan-api bind port |
| `telegram_bot_token` / `telegram_chat_id` | `""` | Plain bot-API alerts; empty disables silently |
| `db_host` / `db_port` / `db_name` / `db_user` / `db_password` | localhost / 5432 / dhan_trading / trader / … | `cfg.db_url` assembles the SQLAlchemy URL |

## Mode changes in practice

```bash
# paper → live (deliberately laborious):
# 1. edit .env:  PAPER_TRADING=false  ALLOW_LIVE_TOGGLE=true
# 2. sudo systemctl restart dhan-trader
# POST /api/mode is read-only by design and returns 409 until M6 auth exists.
```
