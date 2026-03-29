-- =============================================================
-- trading_engine/db/schema.sql
-- Full PostgreSQL schema for the automated trading engine
-- Run once on a fresh database:
--   psql $DATABASE_URL -f db/schema.sql
-- =============================================================

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- =============================================================
-- 1. positions_snapshot
--    Daily snapshot of all open positions
-- =============================================================
CREATE TABLE IF NOT EXISTS positions_snapshot (
    id              BIGSERIAL PRIMARY KEY,
    snapshot_date   DATE        NOT NULL,
    ticker          VARCHAR(16) NOT NULL,
    engine          VARCHAR(32) NOT NULL,   -- aggressive | moderate | safe | capture
    shares          NUMERIC(18,6) NOT NULL DEFAULT 0,
    avg_cost        NUMERIC(18,4) NOT NULL DEFAULT 0,
    current_price   NUMERIC(18,4),
    market_value    NUMERIC(18,4),
    unrealized_pnl  NUMERIC(18,4),
    unrealized_pct  NUMERIC(10,6),
    pct_of_portfolio NUMERIC(10,6),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (snapshot_date, ticker, engine)
);

CREATE INDEX IF NOT EXISTS idx_positions_snapshot_date    ON positions_snapshot (snapshot_date);
CREATE INDEX IF NOT EXISTS idx_positions_snapshot_ticker  ON positions_snapshot (ticker);


-- =============================================================
-- 2. engine_targets
--    Tracks target vs actual allocation per engine
-- =============================================================
CREATE TABLE IF NOT EXISTS engine_targets (
    id              BIGSERIAL PRIMARY KEY,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    engine          VARCHAR(32) NOT NULL,
    target_pct      NUMERIC(10,6) NOT NULL,
    actual_pct      NUMERIC(10,6),
    delta_pct       NUMERIC(10,6),
    portfolio_value NUMERIC(18,4)
);

CREATE INDEX IF NOT EXISTS idx_engine_targets_engine ON engine_targets (engine);
CREATE INDEX IF NOT EXISTS idx_engine_targets_ts     ON engine_targets (recorded_at);


-- =============================================================
-- 3. ticker_targets
--    Per-ticker target vs actual weight
-- =============================================================
CREATE TABLE IF NOT EXISTS ticker_targets (
    id              BIGSERIAL PRIMARY KEY,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ticker          VARCHAR(16) NOT NULL,
    engine          VARCHAR(32) NOT NULL,
    target_pct      NUMERIC(10,6) NOT NULL,
    actual_pct      NUMERIC(10,6),
    delta_pct       NUMERIC(10,6),
    portfolio_value NUMERIC(18,4)
);

CREATE INDEX IF NOT EXISTS idx_ticker_targets_ticker ON ticker_targets (ticker);
CREATE INDEX IF NOT EXISTS idx_ticker_targets_ts     ON ticker_targets (recorded_at);


-- =============================================================
-- 4. cash_bucket
--    Running state of the cash bucket
-- =============================================================
CREATE TABLE IF NOT EXISTS cash_bucket (
    id                  BIGSERIAL PRIMARY KEY,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cash_balance        NUMERIC(18,4) NOT NULL,
    portfolio_value     NUMERIC(18,4) NOT NULL,
    cash_pct            NUMERIC(10,6) NOT NULL,
    deployed_today      NUMERIC(18,4) DEFAULT 0,
    deployed_this_week  NUMERIC(18,4) DEFAULT 0,
    source_dividends    NUMERIC(18,4) DEFAULT 0,
    source_premiums     NUMERIC(18,4) DEFAULT 0,
    source_trims        NUMERIC(18,4) DEFAULT 0,
    source_deposits     NUMERIC(18,4) DEFAULT 0,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_cash_bucket_ts ON cash_bucket (recorded_at);


-- =============================================================
-- 5. trades_log
--    Every trade placed (equity only)
-- =============================================================
CREATE TABLE IF NOT EXISTS trades_log (
    id              BIGSERIAL PRIMARY KEY,
    trade_id        UUID NOT NULL DEFAULT gen_random_uuid(),
    placed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    filled_at       TIMESTAMPTZ,
    ticker          VARCHAR(16) NOT NULL,
    engine          VARCHAR(32) NOT NULL,
    action          VARCHAR(8)  NOT NULL,  -- BUY | SELL
    reason          VARCHAR(128),          -- dip_buy | trim | rebalance | deposit | capture_entry | capture_exit
    shares          NUMERIC(18,6),
    limit_price     NUMERIC(18,4),
    fill_price      NUMERIC(18,4),
    fill_value      NUMERIC(18,4),
    status          VARCHAR(32) NOT NULL DEFAULT 'pending',  -- pending | filled | cancelled | failed
    webull_order_id VARCHAR(64),
    dry_run         BOOLEAN NOT NULL DEFAULT FALSE,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_log_ticker   ON trades_log (ticker);
CREATE INDEX IF NOT EXISTS idx_trades_log_ts       ON trades_log (placed_at);
CREATE INDEX IF NOT EXISTS idx_trades_log_status   ON trades_log (status);
CREATE INDEX IF NOT EXISTS idx_trades_log_engine   ON trades_log (engine);


-- =============================================================
-- 6. options_positions
--    Open covered call positions
-- =============================================================
CREATE TABLE IF NOT EXISTS options_positions (
    id              BIGSERIAL PRIMARY KEY,
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at       TIMESTAMPTZ,
    ticker          VARCHAR(16) NOT NULL,
    option_type     VARCHAR(4)  NOT NULL DEFAULT 'call',  -- call only (no naked puts)
    strike          NUMERIC(18,4) NOT NULL,
    expiry_date     DATE NOT NULL,
    contracts       INTEGER NOT NULL DEFAULT 1,
    premium_received NUMERIC(18,4),
    close_price     NUMERIC(18,4),
    pnl             NUMERIC(18,4),
    status          VARCHAR(32) NOT NULL DEFAULT 'open',  -- open | expired | closed | assigned
    webull_order_id VARCHAR(64),
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_options_ticker  ON options_positions (ticker);
CREATE INDEX IF NOT EXISTS idx_options_expiry  ON options_positions (expiry_date);
CREATE INDEX IF NOT EXISTS idx_options_status  ON options_positions (status);


-- =============================================================
-- 7. dividend_events
--    Known upcoming and historical dividend events
-- =============================================================
CREATE TABLE IF NOT EXISTS dividend_events (
    id              BIGSERIAL PRIMARY KEY,
    ticker          VARCHAR(16) NOT NULL,
    ex_date         DATE NOT NULL,
    pay_date        DATE,
    record_date     DATE,
    amount          NUMERIC(10,6),          -- per-share dividend
    total_received  NUMERIC(18,4),          -- actual cash received (filled after pay date)
    status          VARCHAR(32) NOT NULL DEFAULT 'pending',  -- pending | received | missed
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ticker, ex_date)
);

CREATE INDEX IF NOT EXISTS idx_dividend_events_ticker  ON dividend_events (ticker);
CREATE INDEX IF NOT EXISTS idx_dividend_events_ex_date ON dividend_events (ex_date);


-- =============================================================
-- 8. capture_positions
--    Dividend capture module open/closed positions
-- =============================================================
CREATE TABLE IF NOT EXISTS capture_positions (
    id              BIGSERIAL PRIMARY KEY,
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at       TIMESTAMPTZ,
    ticker          VARCHAR(16) NOT NULL,
    ex_date         DATE NOT NULL,
    shares          NUMERIC(18,6) NOT NULL,
    entry_price     NUMERIC(18,4) NOT NULL,
    exit_price      NUMERIC(18,4),
    cost_basis      NUMERIC(18,4) NOT NULL,
    exit_value      NUMERIC(18,4),
    dividend_received NUMERIC(18,4),
    total_pnl       NUMERIC(18,4),
    exit_reason     VARCHAR(64),   -- price_target | rsi | days_held | pre_ex_return
    status          VARCHAR(32) NOT NULL DEFAULT 'open',  -- open | closed
    hold_days       INTEGER DEFAULT 0,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_capture_ticker  ON capture_positions (ticker);
CREATE INDEX IF NOT EXISTS idx_capture_status  ON capture_positions (status);
CREATE INDEX IF NOT EXISTS idx_capture_ex_date ON capture_positions (ex_date);


-- =============================================================
-- 9. deposits_log
--    Detected new deposits into the account
-- =============================================================
CREATE TABLE IF NOT EXISTS deposits_log (
    id              BIGSERIAL PRIMARY KEY,
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    amount          NUMERIC(18,4) NOT NULL,
    deploy_start    DATE,
    deploy_end      DATE,
    deployed_pct    NUMERIC(5,2) DEFAULT 0,  -- 0–100
    status          VARCHAR(32) NOT NULL DEFAULT 'pending',  -- pending | deploying | deployed
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_deposits_log_ts ON deposits_log (detected_at);


-- =============================================================
-- 10. risk_state
--    Current risk mode flags
-- =============================================================
CREATE TABLE IF NOT EXISTS risk_state (
    id                  BIGSERIAL PRIMARY KEY,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    portfolio_value     NUMERIC(18,4),
    peak_value          NUMERIC(18,4),
    drawdown_pct        NUMERIC(10,6),
    risk_mode           VARCHAR(32) NOT NULL DEFAULT 'normal',
                        -- normal | caution | pause_capture | pause_aggressive | safe_only
    capture_paused      BOOLEAN NOT NULL DEFAULT FALSE,
    aggressive_paused   BOOLEAN NOT NULL DEFAULT FALSE,
    safe_mode_only      BOOLEAN NOT NULL DEFAULT FALSE,
    trades_today        INTEGER NOT NULL DEFAULT 0,
    trades_this_week    INTEGER NOT NULL DEFAULT 0,
    buy_paused          BOOLEAN NOT NULL DEFAULT FALSE,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_risk_state_ts ON risk_state (recorded_at);


-- =============================================================
-- 11. scheduler_log
--    Audit log for every scheduler task run
-- =============================================================
CREATE TABLE IF NOT EXISTS scheduler_log (
    id          BIGSERIAL PRIMARY KEY,
    run_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    task_name   VARCHAR(128) NOT NULL,
    day_of_week VARCHAR(16),    -- monday | tuesday | ... | daily
    status      VARCHAR(32) NOT NULL DEFAULT 'success',  -- success | error | skipped
    duration_ms INTEGER,
    error_msg   TEXT,
    details     JSONB
);

CREATE INDEX IF NOT EXISTS idx_scheduler_log_task ON scheduler_log (task_name);
CREATE INDEX IF NOT EXISTS idx_scheduler_log_ts   ON scheduler_log (run_at);

-- =============================================================
-- SEED engine targets
-- =============================================================
INSERT INTO engine_targets (engine, target_pct, portfolio_value)
VALUES
    ('aggressive', 0.35, 0),
    ('moderate',   0.40, 0),
    ('safe',       0.25, 0)
ON CONFLICT DO NOTHING;
