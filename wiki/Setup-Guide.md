# Setup Guide

Full step-by-step instructions for running the DhanHQ Algo Platform on Ubuntu 22.04 / 24.04. The process takes about 10 minutes on a clean machine.

---

## Prerequisites

| Requirement | Minimum Version | Check |
|---|---|---|
| Ubuntu | 22.04 LTS | `lsb_release -a` |
| Python | 3.14 | `python3.14 --version` |
| pip | 24+ | `pip --version` |
| Node.js | 18+ (dashboard only) | `node --version` |
| npm | 9+ (dashboard only) | `npm --version` |
| Internet access | — | Needed for Dhan API + scrip CSV |

You need a DhanHQ account with API access enabled. Obtain your `client_id` and `access_token` from the [DhanHQ developer portal](https://developer.dhan.co/).

---

## Step 1 — Install Python 3.14

Ubuntu ships Python 3.10 or 3.12 depending on version. Install 3.14 from the deadsnakes PPA:

```bash
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
sudo apt install python3.14 python3.14-venv python3.14-dev -y
```

Verify:

```bash
python3.14 --version
# Python 3.14.x
```

---

## Step 2 — Clone the Repository

```bash
git clone <your-repo-url> dhan_algo
cd dhan_algo
```

---

## Step 3 — Create the Virtual Environment

Always use the project-local venv. Do not install into the system Python.

```bash
python3.14 -m venv venv
source venv/bin/activate
```

Your prompt should now show `(venv)`. All subsequent commands assume the venv is active.

Verify you are using the right Python:

```bash
which python
# /path/to/dhan_algo/venv/bin/python
python --version
# Python 3.14.x
```

---

## Step 4 — Install Python Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

This installs:

- `aiohttp` — async HTTP client and web server
- `dhanhq` — official DhanHQ SDK (used for reference; the platform's own `DhanClient` is standalone)
- `pandas`, `numpy`, `scipy` — data handling and backtesting
- `python-dotenv` — `.env` file loader
- `rich` — formatted terminal output
- `pytest`, `pytest-asyncio`, `aioresponses` — test suite

Installation takes 1–2 minutes depending on your connection.

---

## Step 5 — Configure the Environment

Copy the example config and fill in your credentials:

```bash
cp .env .env.backup   # keep the original as reference
```

Open `.env` in your editor:

```bash
nano .env
```

Set these values at minimum:

```env
# Required — from https://developer.dhan.co/
DHAN_CLIENT_ID=your_client_id_here
DHAN_ACCESS_TOKEN=your_jwt_token_here

# Safety defaults — change only when ready
PAPER_TRADING=true
MAX_DAILY_LOSS=5000
WEBHOOK_PORT=8765

# Strategy selection
STRATEGY=scalper

# Options Scalper — leave blank for automatic nearest weekly expiry
EXPIRY_DATE=

# Number of NIFTY lots (1 lot = 75 units)
NUM_LOTS=1
```

Do not commit `.env` to version control. It is listed in `.gitignore`.

Full description of every variable: [Configuration wiki](Configuration.md).

---

## Step 6 — Run the Platform

```bash
source venv/bin/activate
python main.py
```

Expected startup output:

```
12:00:00  INFO      dhan.main — ============================================================
12:00:00  INFO      dhan.main —   DhanHQ Algo Trading Platform  v1.0
12:00:00  INFO      dhan.main —   Mode:     PAPER TRADING
12:00:00  INFO      dhan.main —   Strategy: SCALPER
12:00:00  INFO      dhan.main —   Client:   1234567890
12:00:00  INFO      dhan.main — ============================================================
12:00:00  INFO      dhan.main — Dashboard: http://localhost:8765
12:00:00  INFO      dhan.risk — Risk manager started
12:00:00  INFO      dhan.scalper — Options Scalper started (paper=True)
12:00:00  INFO      dhan.instruments — Downloading scrip master from Dhan...
12:00:01  INFO      dhan.instruments — Instrument master loaded: 4120 NIFTY option contracts
12:00:01  INFO      dhan.scalper — Auto-selected expiry: 2026-05-08
```

Open `http://localhost:8765` in your browser to see the dashboard.

---

## Step 7 — Switch Strategies

Run the SMA crossover strategy instead:

```bash
STRATEGY=sma python main.py
```

Specify a particular option expiry for the scalper:

```bash
STRATEGY=scalper EXPIRY_DATE=2026-05-15 python main.py
```

Run with 2 lots:

```bash
NUM_LOTS=2 python main.py
```

All environment variables can be passed inline or set in `.env`.

---

## Step 8 — Build the React Dashboard (Optional)

The platform serves a fallback HTML dashboard from `static/index.html` by default. For the full React dashboard:

```bash
# Install Node dependencies
cd dashboard
npm install

# Build the production bundle
npm run build

# Return to project root and restart
cd ..
python main.py
```

The compiled output lands in `dashboard/dist/`. The aiohttp server detects it automatically and serves React instead of the fallback.

For active frontend development with hot reload:

```bash
cd dashboard
npm run dev   # starts Vite dev server on port 5173
# In a second terminal:
python main.py   # API backend on port 8765
```

The Vite config proxies `/api/*` to port 8765.

---

## Stopping the Platform

Press `Ctrl+C`. The platform handles `SIGINT` and `SIGTERM` gracefully:

```
12:35:00  INFO  dhan.main — Signal SIGINT received — shutting down...
12:35:00  INFO  dhan.main — Platform shut down cleanly
```

All pending tasks are cancelled cleanly. No orders are left dangling.

---

## Verifying Live API Connectivity

While still in paper mode, check that credentials are valid:

```bash
# In a running instance, curl the funds endpoint
curl -s http://localhost:8765/api/funds | python3 -m json.tool
```

Expected response when credentials are correct:

```json
{
  "ok": true,
  "data": {
    "availabelBalance": 50000.0,
    "sodLimit": 50000.0,
    ...
  }
}
```

If `"ok": false`, check `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` in `.env`.

---

## Enabling Live Trading

Read every line of [Strategies](Strategies.md) and [Configuration](Configuration.md) before doing this.

1. Confirm paper trading is profitable and the strategy behaves as expected
2. Set `PAPER_TRADING=false` in `.env`
3. Set a conservative `MAX_DAILY_LOSS`
4. Start with `NUM_LOTS=1`

```bash
PAPER_TRADING=false MAX_DAILY_LOSS=2000 python main.py
```

The startup banner will show `LIVE TRADING` in red and log a warning. Real orders will be sent to DhanHQ immediately when signals fire.

---

## Running as a Systemd Service

For unattended operation, create a systemd unit:

```ini
# /etc/systemd/system/dhan-algo.service
[Unit]
Description=DhanHQ Algo Platform
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=yourusername
WorkingDirectory=/path/to/dhan_algo
ExecStart=/path/to/dhan_algo/venv/bin/python main.py
EnvironmentFile=/path/to/dhan_algo/.env
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable dhan-algo
sudo systemctl start dhan-algo
sudo journalctl -u dhan-algo -f   # follow logs
```

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `ModuleNotFoundError` | venv not active | `source venv/bin/activate` |
| `401 Unauthorized` from Dhan API | Expired or wrong token | Regenerate token at developer.dhan.co |
| Dashboard shows blank / 404 | React build not present | Run `npm run build` in `dashboard/` or use fallback |
| `Instrument master load failed` | No internet or Dhan CDN down | Check connectivity; stale cache is used if present |
| Risk halt immediately on start | Existing positions / previous loss | Check positions on Dhan app; use `risk.resume()` in code |
| Port 8765 already in use | Another process bound to it | Set `WEBHOOK_PORT=8766` or kill the other process |
