"""
trading_engine/core/state_manager.py
Builds and persists PortfolioState.

Responsibilities:
  1. Pull live positions + cash from Webull
  2. Calculate actual weights vs targets
  3. Compute engine allocations
  4. Calculate drawdown vs rolling peak
  5. Write positions_snapshot, engine_targets, ticker_targets, cash_bucket to DB
  6. Return a fully-populated PortfolioState
"""

from __future__ import annotations

import datetime
from typing import Optional

import sqlalchemy as sa

from api.client_factory import get_client
from api.webull_client  import Position, CashBalance
from core.portfolio     import (
    PortfolioState, TickerPosition, EngineAllocation,
    TICKER_ENGINE,
)
from config.settings    import (
    ENGINE_TARGETS, ALL_TICKER_WEIGHTS,
    AGGRESSIVE_TICKERS, MODERATE_TICKERS, SAFE_TICKERS,
)
from db.database        import get_db
from utils.logger       import get_logger

logger = get_logger(__name__)


class StateManager:
    """
    Single entry point for refreshing and persisting portfolio state.
    """

    def build_state(self) -> PortfolioState:
        """
        Fetch live data, compute state, write to DB, return PortfolioState.
        """
        client = get_client()

        # 1. Fetch live data
        wb_positions: list[Position] = client.fetch_positions()
        cash_bal:     CashBalance    = client.fetch_cash()

        total_value   = cash_bal.portfolio_value or self._sum_positions(wb_positions, cash_bal)
        cash_balance  = cash_bal.total_cash
        cash_pct      = cash_balance / total_value if total_value else 0.0
        today         = datetime.date.today()

        # 2. Build ticker positions
        positions: dict[str, TickerPosition] = {}
        for p in wb_positions:
            ticker = p.ticker.upper()
            engine = TICKER_ENGINE.get(ticker, self._infer_engine(ticker))
            target = ALL_TICKER_WEIGHTS.get(ticker, 0.0)
            actual = p.market_value / total_value if total_value else 0.0
            positions[ticker] = TickerPosition(
                ticker         = ticker,
                engine         = engine,
                shares         = p.shares,
                avg_cost       = p.avg_cost,
                current_price  = p.current_price,
                market_value   = p.market_value,
                unrealized_pnl = p.unrealized_pnl,
                unrealized_pct = p.unrealized_pct,
                actual_pct     = actual,
                target_pct     = target,
                delta_pct      = actual - target,
            )

        # 3. Engine allocations
        engine_allocs: dict[str, EngineAllocation] = {}
        for eng, target_pct in ENGINE_TARGETS.items():
            eng_value = sum(
                p.market_value for p in positions.values() if p.engine == eng
            )
            actual_pct = eng_value / total_value if total_value else 0.0
            engine_allocs[eng] = EngineAllocation(
                engine       = eng,
                target_pct   = target_pct,
                actual_pct   = actual_pct,
                actual_value = eng_value,
                delta_pct    = actual_pct - target_pct,
            )

        # 4. Drawdown vs peak
        peak_value, drawdown_pct = self._compute_drawdown(total_value)

        # 5. Trade counters from DB
        trades_today, trades_week = self._get_trade_counts()

        state = PortfolioState(
            snapshot_date   = today,
            portfolio_value = total_value,
            cash_balance    = cash_balance,
            cash_pct        = cash_pct,
            positions       = positions,
            engine_allocs   = engine_allocs,
            peak_value      = peak_value,
            drawdown_pct    = drawdown_pct,
            trades_today    = trades_today,
            trades_this_week= trades_week,
        )

        # 6. Persist to DB
        self._persist_state(state)

        logger.info(
            "State built | portfolio=%.2f | cash=%.1f%% | drawdown=%.1f%% | "
            "positions=%d | trades_today=%d",
            total_value,
            cash_pct * 100,
            drawdown_pct * 100,
            len(positions),
            trades_today,
        )
        return state

    # ------------------------------------------------------------------
    # Drawdown tracking
    # ------------------------------------------------------------------

    def _compute_drawdown(self, current_value: float) -> tuple[float, float]:
        """
        Compare current value against the rolling peak stored in risk_state.
        Returns (peak_value, drawdown_pct).
        """
        with get_db() as db:
            row = db.execute(sa.text(
                "SELECT peak_value FROM risk_state ORDER BY recorded_at DESC LIMIT 1"
            )).fetchone()

        peak = float(row[0]) if row and row[0] else current_value
        peak = max(peak, current_value)           # update peak if new high
        drawdown_pct = (peak - current_value) / peak if peak else 0.0
        return peak, drawdown_pct

    # ------------------------------------------------------------------
    # Trade counters
    # ------------------------------------------------------------------

    def _get_trade_counts(self) -> tuple[int, int]:
        """Return (trades_today, trades_this_week) from trades_log."""
        today = datetime.date.today()
        week_start = today - datetime.timedelta(days=today.weekday())  # Monday

        with get_db() as db:
            today_count = db.execute(sa.text(
                "SELECT COUNT(*) FROM trades_log "
                "WHERE status IN ('filled','pending') "
                "AND placed_at::date = :today AND dry_run = FALSE"
            ), {"today": today}).scalar() or 0

            week_count = db.execute(sa.text(
                "SELECT COUNT(*) FROM trades_log "
                "WHERE status IN ('filled','pending') "
                "AND placed_at::date >= :week_start AND dry_run = FALSE"
            ), {"week_start": week_start}).scalar() or 0

        return int(today_count), int(week_count)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_state(self, state: PortfolioState) -> None:
        try:
            self._upsert_positions_snapshot(state)
            self._upsert_engine_targets(state)
            self._upsert_ticker_targets(state)
            self._upsert_cash_bucket(state)
            self._upsert_risk_state(state)
        except Exception as exc:
            logger.error("Failed to persist state: %s", exc)

    def _upsert_positions_snapshot(self, state: PortfolioState) -> None:
        with get_db() as db:
            # Delete today's existing snapshot rows for clean upsert
            db.execute(sa.text(
                "DELETE FROM positions_snapshot WHERE snapshot_date = :d"
            ), {"d": state.snapshot_date})
            for pos in state.positions.values():
                db.execute(sa.text("""
                    INSERT INTO positions_snapshot
                        (snapshot_date, ticker, engine, shares, avg_cost,
                         current_price, market_value, unrealized_pnl,
                         unrealized_pct, pct_of_portfolio)
                    VALUES
                        (:d, :ticker, :engine, :shares, :avg_cost,
                         :current_price, :market_value, :unrealized_pnl,
                         :unrealized_pct, :pct_of_portfolio)
                """), {
                    "d":                  state.snapshot_date,
                    "ticker":             pos.ticker,
                    "engine":             pos.engine,
                    "shares":             pos.shares,
                    "avg_cost":           pos.avg_cost,
                    "current_price":      pos.current_price,
                    "market_value":       pos.market_value,
                    "unrealized_pnl":     pos.unrealized_pnl,
                    "unrealized_pct":     pos.unrealized_pct,
                    "pct_of_portfolio":   pos.actual_pct,
                })

    def _upsert_engine_targets(self, state: PortfolioState) -> None:
        with get_db() as db:
            for alloc in state.engine_allocs.values():
                db.execute(sa.text("""
                    INSERT INTO engine_targets
                        (engine, target_pct, actual_pct, delta_pct, portfolio_value)
                    VALUES (:engine, :target, :actual, :delta, :pv)
                """), {
                    "engine": alloc.engine,
                    "target": alloc.target_pct,
                    "actual": alloc.actual_pct,
                    "delta":  alloc.delta_pct,
                    "pv":     state.portfolio_value,
                })

    def _upsert_ticker_targets(self, state: PortfolioState) -> None:
        with get_db() as db:
            for pos in state.positions.values():
                db.execute(sa.text("""
                    INSERT INTO ticker_targets
                        (ticker, engine, target_pct, actual_pct, delta_pct, portfolio_value)
                    VALUES (:ticker, :engine, :target, :actual, :delta, :pv)
                """), {
                    "ticker": pos.ticker,
                    "engine": pos.engine,
                    "target": pos.target_pct,
                    "actual": pos.actual_pct,
                    "delta":  pos.delta_pct,
                    "pv":     state.portfolio_value,
                })

    def _upsert_cash_bucket(self, state: PortfolioState) -> None:
        with get_db() as db:
            db.execute(sa.text("""
                INSERT INTO cash_bucket
                    (cash_balance, portfolio_value, cash_pct)
                VALUES (:cash, :pv, :pct)
            """), {
                "cash": state.cash_balance,
                "pv":   state.portfolio_value,
                "pct":  state.cash_pct,
            })

    def _upsert_risk_state(self, state: PortfolioState) -> None:
        from core.risk_manager import RiskMode, classify_risk_mode
        mode = classify_risk_mode(state.drawdown_pct)
        with get_db() as db:
            db.execute(sa.text("""
                INSERT INTO risk_state
                    (portfolio_value, peak_value, drawdown_pct, risk_mode,
                     capture_paused, aggressive_paused, safe_mode_only,
                     trades_today, trades_this_week)
                VALUES
                    (:pv, :peak, :dd, :mode,
                     :capture_paused, :aggr_paused, :safe_only,
                     :t_today, :t_week)
            """), {
                "pv":             state.portfolio_value,
                "peak":           state.peak_value,
                "dd":             state.drawdown_pct,
                "mode":           mode.value,
                "capture_paused": mode.value in ("pause_capture", "pause_aggressive", "safe_only"),
                "aggr_paused":    mode.value in ("pause_aggressive", "safe_only"),
                "safe_only":      mode.value == "safe_only",
                "t_today":        state.trades_today,
                "t_week":         state.trades_this_week,
            })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sum_positions(positions: list[Position], cash: CashBalance) -> float:
        return sum(p.market_value for p in positions) + cash.total_cash

    @staticmethod
    def _infer_engine(ticker: str) -> str:
        """Fallback engine assignment for unrecognised tickers."""
        return "moderate"
