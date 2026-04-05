"""
dashboard/app.py
Flask web dashboard for the trading bot.

Reads live data from PostgreSQL and exposes:
  GET /            — dashboard HTML
  GET /api/summary — cash + portfolio metrics (JSON)
  GET /api/positions — open positions (JSON)
  GET /api/trades  — last 50 trades (JSON)
  GET /api/risk    — latest risk state (JSON)

Environment:
  DATABASE_URL  — PostgreSQL connection string (required)
  DASHBOARD_PORT — port to listen on (default 5000)
  DASHBOARD_HOST — host to bind (default 0.0.0.0)
"""

import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, render_template

app = Flask(__name__)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://trading_user:changeme@localhost:5432/trading_engine"
)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_conn():
    """Open a new psycopg2 connection."""
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def query(sql: str, params=None) -> list:
    """Execute a SELECT and return all rows as a list of dicts."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return [dict(row) for row in cur.fetchall()]


def query_one(sql: str, params=None) -> dict | None:
    """Execute a SELECT and return the first row as a dict, or None."""
    rows = query(sql, params)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Routes — HTML
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------

@app.route("/api/summary")
def api_summary():
    """Latest cash bucket row — cash balance, portfolio value, cash %."""
    row = query_one(
        """
        SELECT
            cash_balance,
            portfolio_value,
            cash_pct,
            deployed_today,
            deployed_this_week,
            source_dividends,
            source_premiums,
            source_trims,
            source_deposits,
            recorded_at
        FROM cash_bucket
        ORDER BY recorded_at DESC
        LIMIT 1
        """
    )
    if row is None:
        return jsonify({"error": "No cash_bucket data found"}), 404

    # Serialize datetime
    if row.get("recorded_at"):
        row["recorded_at"] = row["recorded_at"].isoformat()

    return jsonify(row)


@app.route("/api/positions")
def api_positions():
    """Most recent snapshot of every open position."""
    rows = query(
        """
        SELECT
            p.ticker,
            p.engine,
            p.shares,
            p.avg_cost,
            p.current_price,
            p.market_value,
            p.unrealized_pnl,
            p.unrealized_pct,
            p.pct_of_portfolio,
            p.snapshot_date
        FROM positions_snapshot p
        INNER JOIN (
            SELECT ticker, engine, MAX(snapshot_date) AS latest
            FROM positions_snapshot
            GROUP BY ticker, engine
        ) latest_dates
            ON p.ticker = latest_dates.ticker
           AND p.engine = latest_dates.engine
           AND p.snapshot_date = latest_dates.latest
        WHERE p.shares > 0
        ORDER BY p.engine, p.market_value DESC NULLS LAST
        """
    )

    for row in rows:
        if row.get("snapshot_date"):
            row["snapshot_date"] = row["snapshot_date"].isoformat()
        # Cast Decimal → float for JSON serialisation
        for key in ("shares", "avg_cost", "current_price", "market_value",
                    "unrealized_pnl", "unrealized_pct", "pct_of_portfolio"):
            if row.get(key) is not None:
                row[key] = float(row[key])

    return jsonify(rows)


@app.route("/api/trades")
def api_trades():
    """Last 50 trades from trades_log."""
    rows = query(
        """
        SELECT
            trade_id,
            placed_at,
            filled_at,
            ticker,
            engine,
            action,
            reason,
            shares,
            limit_price,
            fill_price,
            fill_value,
            status,
            dry_run,
            notes
        FROM trades_log
        ORDER BY placed_at DESC
        LIMIT 50
        """
    )

    for row in rows:
        for ts_col in ("placed_at", "filled_at"):
            if row.get(ts_col):
                row[ts_col] = row[ts_col].isoformat()
        if row.get("trade_id"):
            row["trade_id"] = str(row["trade_id"])
        for key in ("shares", "limit_price", "fill_price", "fill_value"):
            if row.get(key) is not None:
                row[key] = float(row[key])

    return jsonify(rows)


@app.route("/api/risk")
def api_risk():
    """Latest risk state row."""
    row = query_one(
        """
        SELECT
            portfolio_value,
            peak_value,
            drawdown_pct,
            risk_mode,
            capture_paused,
            aggressive_paused,
            safe_mode_only,
            trades_today,
            trades_this_week,
            buy_paused,
            notes,
            recorded_at
        FROM risk_state
        ORDER BY recorded_at DESC
        LIMIT 1
        """
    )
    if row is None:
        return jsonify({"error": "No risk_state data found"}), 404

    if row.get("recorded_at"):
        row["recorded_at"] = row["recorded_at"].isoformat()
    for key in ("portfolio_value", "peak_value", "drawdown_pct"):
        if row.get(key) is not None:
            row[key] = float(row[key])

    return jsonify(row)


@app.route("/api/health")
def api_health():
    """Simple liveness check."""
    try:
        query_one("SELECT 1 AS ok")
        return jsonify({"status": "ok", "db": "connected",
                        "ts": datetime.now(timezone.utc).isoformat()})
    except Exception as exc:
        return jsonify({"status": "error", "db": "unreachable",
                        "detail": str(exc)}), 503


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.environ.get("DASHBOARD_PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host=host, port=port, debug=debug)
