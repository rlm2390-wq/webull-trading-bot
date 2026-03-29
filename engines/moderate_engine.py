"""
trading_engine/engines/moderate_engine.py
Moderate Engine (40% target allocation).

Tickers: JEPI, JEPQ, QYLD, RYLD, MPLX, DIVO, VYM, DGRO

Logic:
  - Hold long-term; dividends → cash bucket
  - Trim if >2% overweight vs target
  - Buy if >2% underweight vs target
  - Dividend capture module runs through this engine
"""

from __future__ import annotations

from typing import List

from config.settings     import (
    MODERATE_TICKERS,
    MODERATE_OVERWEIGHT_THRESHOLD,
    MODERATE_UNDERWEIGHT_THRESHOLD,
)
from core.portfolio      import PortfolioState
from core.risk_manager   import RiskManager, TradeProposal, RiskMode
from core.trade_executor import TradeExecutor
from utils.logger        import get_logger

logger = get_logger(__name__)


class ModerateEngine:

    ENGINE = "moderate"

    def __init__(self, state: PortfolioState, risk: RiskManager, executor: TradeExecutor):
        self.state    = state
        self.risk     = risk
        self.executor = executor

    # ------------------------------------------------------------------
    # Main entry (Friday rebalance + daily drift checks)
    # ------------------------------------------------------------------

    def run(self) -> List[TradeProposal]:
        proposals: List[TradeProposal] = []
        proposals += self._scan_overweights()
        proposals += self._scan_underweights()
        executed = self.executor.execute_many(proposals)
        logger.info("[Moderate] %d proposals → %d executed", len(proposals), len(executed))
        return proposals

    # ------------------------------------------------------------------
    # Overweight trim
    # ------------------------------------------------------------------

    def _scan_overweights(self) -> List[TradeProposal]:
        proposals = []
        for ticker in MODERATE_TICKERS:
            pos = self.state.get_position(ticker)
            if not pos:
                continue
            if pos.delta_pct < MODERATE_OVERWEIGHT_THRESHOLD:
                continue

            # Trim back to target
            trim_value = pos.delta_pct * self.state.portfolio_value
            trim_value = max(trim_value, 0.0)
            if trim_value < 10:
                continue

            logger.info(
                "[Moderate] OVERWEIGHT trim: %s delta=+%.2f%% → sell $%.2f",
                ticker, pos.delta_pct * 100, trim_value,
            )
            proposals.append(TradeProposal(
                ticker    = ticker,
                action    = "SELL",
                engine    = self.ENGINE,
                reason    = "rebalance_overweight",
                value_usd = trim_value,
                price     = pos.current_price,
            ))
        return proposals

    # ------------------------------------------------------------------
    # Underweight buy
    # ------------------------------------------------------------------

    def _scan_underweights(self) -> List[TradeProposal]:
        if not self.risk.can_buy(self.ENGINE):
            logger.info("[Moderate] Buys paused (risk mode or cash floor)")
            return []

        proposals = []
        for ticker in MODERATE_TICKERS:
            pos    = self.state.get_position(ticker)
            target = self.state.target_pct_of(ticker)
            actual = self.state.actual_pct_of(ticker)
            delta  = actual - target  # negative = underweight

            if delta > -MODERATE_UNDERWEIGHT_THRESHOLD:
                continue

            buy_value = abs(delta) * self.state.portfolio_value
            buy_value = min(buy_value, self.state.cash_balance * 0.4)
            if buy_value < 10:
                continue

            price = pos.current_price if pos else 0.0

            logger.info(
                "[Moderate] UNDERWEIGHT buy: %s delta=%.2f%% → buy $%.2f",
                ticker, delta * 100, buy_value,
            )
            proposals.append(TradeProposal(
                ticker    = ticker,
                action    = "BUY",
                engine    = self.ENGINE,
                reason    = "rebalance_underweight",
                value_usd = buy_value,
                price     = price,
            ))
        return proposals

    # ------------------------------------------------------------------
    # Dividend accounting
    # ------------------------------------------------------------------

    def record_dividend(self, ticker: str, amount: float) -> None:
        """
        Called when a dividend payment is detected.
        Logs the event and routes proceeds to the cash bucket ledger.
        """
        import sqlalchemy as sa
        import datetime
        from db.database import get_db
        from core.cash_bucket import CashBucket

        logger.info("[Moderate] Dividend received: %s $%.4f", ticker, amount)

        with get_db() as db:
            db.execute(sa.text("""
                UPDATE dividend_events
                SET total_received = :amount, status = 'received'
                WHERE ticker = :ticker AND status = 'pending'
                  AND pay_date <= :today
                ORDER BY pay_date DESC
                LIMIT 1
            """), {
                "amount": amount,
                "ticker": ticker,
                "today":  datetime.date.today(),
            })

        # Update cash bucket source ledger
        cb = CashBucket(self.state)
        cb.record_source(dividends=amount)
