"""
trading_engine/config/settings.py
Central configuration for all engine parameters, allocations, and safety rails.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://trading_user:changeme@localhost:5432/trading_engine"
)

# ---------------------------------------------------------------------------
# WEBULL CREDENTIALS
# ---------------------------------------------------------------------------
WEBULL_USERNAME     = os.environ.get("WEBULL_USERNAME", "")
WEBULL_PASSWORD     = os.environ.get("WEBULL_PASSWORD", "")
WEBULL_DEVICE_ID    = os.environ.get("WEBULL_DEVICE_ID", "")
WEBULL_TRADE_PIN    = os.environ.get("WEBULL_TRADE_PIN", "")
WEBULL_MFA_CODE     = os.environ.get("WEBULL_MFA_CODE", "")   # optional TOTP secret

# ---------------------------------------------------------------------------
# ENGINE TARGET ALLOCATIONS  (must sum to 1.0)
# ---------------------------------------------------------------------------
ENGINE_TARGETS: Dict[str, float] = {
    "aggressive": 0.35,
    "moderate":   0.40,
    "safe":       0.25,
}

# ---------------------------------------------------------------------------
# TICKER WEIGHTS  (within-engine weights; must sum to engine target)
# ---------------------------------------------------------------------------
AGGRESSIVE_TICKERS: Dict[str, float] = {
    "MARA":  0.07,
    "CLSK":  0.07,
    "SOXL":  0.07,
    "BITX":  0.06,
    "TNA":   0.04,
    "NVDA":  0.02,
    "SMCI":  0.02,
}

MODERATE_TICKERS: Dict[str, float] = {
    "JEPI":  0.08,
    "JEPQ":  0.06,
    "QYLD":  0.06,
    "RYLD":  0.04,
    "MPLX":  0.06,
    "DIVO":  0.04,
    "VYM":   0.03,
    "DGRO":  0.03,
}

SAFE_TICKERS: Dict[str, float] = {
    "SCHD":  0.07,
    "VOO":   0.06,
    "VTI":   0.05,
    "QQQ":   0.04,
    "HDV":   0.03,
}

# All tickers flat map
ALL_TICKER_WEIGHTS: Dict[str, float] = {
    **AGGRESSIVE_TICKERS,
    **MODERATE_TICKERS,
    **SAFE_TICKERS,
}

# ---------------------------------------------------------------------------
# CASH BUCKET
# ---------------------------------------------------------------------------
CASH_MIN_PCT          = 0.03   # pause buys if below
CASH_TARGET_LOW_PCT   = 0.03
CASH_TARGET_HIGH_PCT  = 0.05
CASH_DEPLOY_PCT_MONDAY = 0.30  # deploy 25–33%; we use 30% as default
CASH_DEPLOY_EXTRA_THRESHOLD = 0.05  # deploy extra if cash > this

# ---------------------------------------------------------------------------
# NEW DEPOSIT DETECTION
# ---------------------------------------------------------------------------
DEPOSIT_DETECTION_THRESHOLD = 50.00   # USD
DEPOSIT_DEPLOY_DAYS_MIN     = 2
DEPOSIT_DEPLOY_DAYS_MAX     = 4

# ---------------------------------------------------------------------------
# AGGRESSIVE ENGINE RULES
# ---------------------------------------------------------------------------
AGGRESSIVE_TRIM_RULES: List[Dict] = [
    {"gain_pct": 0.15, "trim_pct": 0.05},
    {"gain_pct": 0.25, "trim_pct": 0.10},
    {"gain_pct": 0.40, "trim_pct": 0.15},
]

AGGRESSIVE_DIP_RULES: List[Dict] = [
    {"loss_pct": 0.10, "buy_pct": 0.01},
    {"loss_pct": 0.20, "buy_pct": 0.02},
    {"loss_pct": 0.30, "buy_pct": 0.03},
]

COVERED_CALL_TICKERS: List[str] = ["MARA", "CLSK", "SOXL", "BITX"]
COVERED_CALL_OTM_MIN  = 0.20   # 20% OTM
COVERED_CALL_OTM_MAX  = 0.30   # 30% OTM
COVERED_CALL_EXP_MIN  = 7      # days to expiry
COVERED_CALL_EXP_MAX  = 14

# ---------------------------------------------------------------------------
# MODERATE ENGINE RULES
# ---------------------------------------------------------------------------
MODERATE_OVERWEIGHT_THRESHOLD  = 0.02   # trim if >2% over
MODERATE_UNDERWEIGHT_THRESHOLD = 0.02   # buy if >2% under

# ---------------------------------------------------------------------------
# SAFE ENGINE RULES
# ---------------------------------------------------------------------------
SAFE_DIP_THRESHOLD     = 0.05   # buy on –5% or more
SAFE_OVERWEIGHT_TRIM   = 0.02
SAFE_UNDERWEIGHT_BUY   = 0.02

# ---------------------------------------------------------------------------
# DIVIDEND CAPTURE MODULE
# ---------------------------------------------------------------------------
DIVIDEND_CAPTURE_TICKERS: List[str] = [
    "JEPI", "JEPQ", "QYLD", "RYLD", "MPLX", "DIVO", "VYM", "DGRO", "SCHD"
]
CAPTURE_EX_DATE_WINDOW    = 3    # days before ex-date
CAPTURE_RSI_ENTRY_MAX     = 65
CAPTURE_RSI_EXIT_MIN      = 70
CAPTURE_NO_LOSS_DAY_PCT   = 0.03  # skip if –3% day
CAPTURE_EARNINGS_WINDOW   = 7     # days
CAPTURE_SIZE_MIN_PCT      = 0.005
CAPTURE_SIZE_MAX_PCT      = 0.01
CAPTURE_MAX_TOTAL_PCT     = 0.02
CAPTURE_MAX_HOLD_DAYS     = 5
CAPTURE_PROFIT_TARGET_PCT = 0.005  # +0.5% exit

# ---------------------------------------------------------------------------
# SAFETY RAILS
# ---------------------------------------------------------------------------
MAX_TRADES_PER_WEEK    = 7
MAX_TRADES_PER_DAY     = 3
MIN_POSITION_PCT       = 0.005   # never reduce below 0.5%

DRAWDOWN_PAUSE_CAPTURE = 0.15   # pause capture trades
DRAWDOWN_PAUSE_AGGR    = 0.20   # pause aggressive buys
DRAWDOWN_SAFE_ONLY     = 0.25   # route all cash to safe engine

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
LOG_LEVEL     = os.environ.get("LOG_LEVEL", "INFO")
LOG_DIR       = os.environ.get("LOG_DIR", "logs")
LOG_FILE      = os.path.join(LOG_DIR, "trading_engine.log")
LOG_MAX_BYTES = 10 * 1024 * 1024   # 10 MB
LOG_BACKUP_COUNT = 5

# ---------------------------------------------------------------------------
# SCHEDULER
# ---------------------------------------------------------------------------
TIMEZONE = os.environ.get("TZ", "America/New_York")
MARKET_OPEN_HOUR   = 9
MARKET_OPEN_MINUTE = 35   # 5 min after open
MARKET_CLOSE_HOUR  = 15
MARKET_CLOSE_MINUTE = 45  # 15 min before close

# ---------------------------------------------------------------------------
# MISC
# ---------------------------------------------------------------------------
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
ENVIRONMENT = os.environ.get("ENVIRONMENT", "production")
