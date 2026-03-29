# Trading Engine — Deployment Guide

## Project Structure

```
trading_engine/
├── main.py                        # Entry point
├── requirements.txt
├── .env.template                  # Copy to .env and fill in
├── config/
│   └── settings.py                # All constants and allocations
├── api/
│   ├── webull_auth.py             # Auth, token management
│   ├── webull_client.py           # All Webull API calls
│   ├── client_factory.py          # Singleton client
│   └── market_data.py             # RSI, earnings, option helpers
├── core/
│   ├── portfolio.py               # PortfolioState dataclasses
│   ├── state_manager.py           # Build + persist state
│   ├── risk_manager.py            # Safety rail gate
│   ├── trade_executor.py          # Order placement + logging
│   ├── cash_bucket.py             # Cash deploy + deposit detection
│   ├── decision_loop.py           # 13-step orchestrator
│   └── scheduler_log.py           # Task audit logging
├── engines/
│   ├── aggressive_engine.py       # Aggressive (35%)
│   ├── moderate_engine.py         # Moderate (40%)
│   └── safe_engine.py             # Safe (25%)
├── modules/
│   ├── dividend_capture.py        # Dividend capture module
│   └── dividend_sync.py           # Ex-date sync from Webull
├── scheduler/
│   └── scheduler.py               # APScheduler weekly rhythm
└── db/
    ├── schema.sql                 # PostgreSQL schema (11 tables)
    └── database.py                # SQLAlchemy session factory
```

---

## Quick Start (Local)

```bash
# 1. Clone / download the project
cd trading_engine

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.template .env
# Edit .env with your Webull credentials and DATABASE_URL

# 5. Set up PostgreSQL
createdb trading_engine
python main.py --init-db

# 6. Verify DB
python main.py --check-db

# 7. Dry-run one cycle
python main.py --dry-run daily

# 8. Start the scheduler
python main.py
```

---

## Cloud Deployment

### Option A — Railway.app (Recommended, cheapest)

1. Push repo to GitHub (make sure `.env` is in `.gitignore`)
2. Create new Railway project → Deploy from GitHub
3. Add a PostgreSQL plugin (Railway provides one free)
4. Set environment variables in Railway dashboard:
   - `DATABASE_URL` (auto-set by Railway Postgres plugin)
   - `WEBULL_USERNAME`
   - `WEBULL_PASSWORD`
   - `WEBULL_DEVICE_ID`
   - `WEBULL_TRADE_PIN`
   - `TZ=America/New_York`
   - `DRY_RUN=false`
5. Set start command: `python main.py`
6. Deploy — Railway keeps the process alive 24/7

### Option B — Render.com

1. Create a Background Worker (not a Web Service)
2. Connect GitHub repo
3. Set build command: `pip install -r requirements.txt`
4. Set start command: `python main.py`
5. Add environment variables (same as above)
6. Add a Render PostgreSQL database, copy the connection string to `DATABASE_URL`

### Option C — AWS EC2 / Lightsail

```bash
# On the server:
sudo apt update && sudo apt install -y python3 python3-pip python3-venv postgresql

# Set up Postgres
sudo -u postgres psql -c "CREATE USER trading_user WITH PASSWORD 'yourpassword';"
sudo -u postgres psql -c "CREATE DATABASE trading_engine OWNER trading_user;"

# Clone repo and set up
git clone <your-repo> /opt/trading_engine
cd /opt/trading_engine
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.template .env
# fill in .env

python main.py --init-db

# Run as a systemd service
sudo nano /etc/systemd/system/trading-engine.service
```

**systemd service file:**
```ini
[Unit]
Description=Automated Trading Engine
After=network.target postgresql.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/trading_engine
EnvironmentFile=/opt/trading_engine/.env
ExecStart=/opt/trading_engine/venv/bin/python main.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable trading-engine
sudo systemctl start trading-engine
sudo journalctl -u trading-engine -f    # tail logs
```

### Option D — Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV TZ=America/New_York
CMD ["python", "main.py"]
```

```bash
docker build -t trading-engine .
docker run -d \
  --env-file .env \
  --name trading-engine \
  --restart unless-stopped \
  trading-engine
```

---

## Webull Device ID

Webull requires a device ID for authentication. Generate one:

```python
import uuid, hashlib
device_id = hashlib.md5(str(uuid.uuid4()).encode()).hexdigest()
print(device_id)
```

Use the same device ID every time. Store it in `.env`.

---

## Manual One-Shot Runs

```bash
# Test a specific day's logic without waiting for the scheduler
python main.py --run-now monday
python main.py --run-now wednesday
python main.py --run-now friday
python main.py --run-now daily

# Full dry-run (logs everything, places no real orders)
python main.py --dry-run monday
```

---

## Scheduler Times (Eastern Time)

| Time     | Days          | Job                              |
|----------|---------------|----------------------------------|
| 09:35 ET | Mon           | Cash deploy, underweight buys    |
| 09:35 ET | Wed           | Covered calls, capture entries   |
| 09:35 ET | Fri           | Rebalance, capture exits         |
| 09:35 ET | Tue, Thu      | Daily dip/spike/safety           |
| 12:00 ET | Mon–Fri       | Midday dip check                 |
| 15:45 ET | Mon–Fri       | EOD snapshot + stale order flush |

---

## Safety Checklist Before Going Live

- [ ] Run `--dry-run daily` and verify logs look correct
- [ ] Verify DB tables exist: `psql $DATABASE_URL -c "\dt"`
- [ ] Confirm `DRY_RUN=false` only when ready for real orders
- [ ] Confirm Webull account has fractional shares enabled (if using < $100 buys)
- [ ] Confirm Webull account has options trading enabled for covered calls
- [ ] Review `config/settings.py` — all allocations and thresholds match your intent
- [ ] Set a calendar reminder to update `MARKET_HOLIDAYS` in `scheduler.py` annually

---

## Monitoring

All activity is logged to:
- **Console** (stdout) — for cloud provider log dashboards
- **`logs/trading_engine.log`** — rotating file, 10 MB × 5 backups
- **`scheduler_log` table** — every task run with duration and status
- **`trades_log` table** — every order placed or failed
- **`risk_state` table** — drawdown and mode at every run

Query today's trades:
```sql
SELECT placed_at, action, ticker, engine, reason, fill_value, status
FROM trades_log
WHERE placed_at::date = CURRENT_DATE
ORDER BY placed_at DESC;
```

Query risk history:
```sql
SELECT recorded_at, drawdown_pct, risk_mode, trades_today
FROM risk_state
ORDER BY recorded_at DESC
LIMIT 20;
```
