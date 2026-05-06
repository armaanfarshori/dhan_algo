# DhanHQ Algo Platform — Quick Start

Get from zero to a running algo in under 5 minutes. Assumes Ubuntu 22.04+ and a DhanHQ account with API access enabled.

---

## 1. Get credentials

Log in at https://developer.dhan.co/ and generate an access token. You need two values:

- **Client ID** — shown on the developer portal home
- **Access Token** — a long JWT string

---

## 2. Clone and set up

```bash
git clone <your-repo-url> dhan_algo && cd dhan_algo
python3.14 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## 3. Configure

Open `.env` and fill in your credentials:

```env
DHAN_CLIENT_ID=your_client_id
DHAN_ACCESS_TOKEN=your_jwt_token

PAPER_TRADING=true        # safe default — no real money
MAX_DAILY_LOSS=5000       # INR daily stop-loss
STRATEGY=scalper          # scalper (NIFTY options) or sma (equities)
EXPIRY_DATE=              # leave blank for auto nearest-weekly
NUM_LOTS=1                # 1 lot = 75 NIFTY units
WEBHOOK_PORT=8765
```

---

## 4. Run

```bash
source venv/bin/activate && python main.py
```

Open **http://localhost:8765** for the live dashboard.

Expected output within the first ~10 seconds:

```
  DhanHQ Algo Trading Platform  v1.0
  Mode:     PAPER TRADING
  Strategy: SCALPER
Dashboard: http://localhost:8765
Instrument master loaded: 4120 NIFTY option contracts
Auto-selected expiry: 2026-05-08
```

---

## 5. Verify it's working

```bash
# Check strategy state
curl -s http://localhost:8765/api/status | python3 -m json.tool

# Check available funds (requires valid credentials)
curl -s http://localhost:8765/api/funds | python3 -m json.tool

# Watch the signal feed
curl -s http://localhost:8765/api/signals | python3 -m json.tool
```

Check RSI and scalper state:

```bash
curl -s http://localhost:8765/api/scalper | python3 -m json.tool
```

---

## 6. Switch strategies

```bash
# SMA 9/21 crossover on Reliance (NSE equities)
STRATEGY=sma python main.py

# Scalper on a specific weekly expiry
STRATEGY=scalper EXPIRY_DATE=2026-05-15 python main.py

# Two lots
NUM_LOTS=2 python main.py
```

---

## 7. Stop

`Ctrl+C` — graceful shutdown, all tasks cancelled cleanly.

---

## When you're ready for live trading

1. Read the full [Strategies wiki](../wiki/Strategies.md) — understand every signal condition and the OCO mechanics
2. Run paper mode for at least a few sessions and confirm the signal feed looks correct
3. Set `PAPER_TRADING=false` and start with `NUM_LOTS=1` and a tight `MAX_DAILY_LOSS`

```bash
PAPER_TRADING=false MAX_DAILY_LOSS=2000 NUM_LOTS=1 python main.py
```

The startup banner will show `LIVE TRADING` and log a warning. Real orders go to NSE immediately on signal.

---

## How the scalper works (30-second summary)

- Polls NIFTY 50 index price every 10 seconds
- Computes RSI-14 on the index
- **RSI crosses below 30** → buys the ATM call option at market
- **RSI crosses above 70** → buys the ATM put option at market
- ATM security IDs are auto-discovered from Dhan's scrip master CSV (downloaded at startup, cached 6h)
- Immediately after fill: places a Forever OCO — target at `breakeven + ₹5`, stop at `entry − ₹5`
- Breakeven includes all charges: brokerage ₹20/leg, STT, exchange fee, SEBI fee, GST, stamp duty
- Hard squareoff at 15:15 IST regardless of OCO status

---

## API endpoints at a glance

| Endpoint | Returns |
|---|---|
| `/api/status` | Mode, uptime, position, order count |
| `/api/risk` | P&L, halt state, violations |
| `/api/signals` | Last 50 signals |
| `/api/funds` | Available balance from Dhan |
| `/api/positions` | Open positions from Dhan |
| `/api/scalper` | RSI, OCO state, ATM strike |
| `/api/instruments` | NIFTY expiry list |
| `POST /postback` | DhanHQ order webhook |
