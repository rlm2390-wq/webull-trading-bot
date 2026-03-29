"""
trading_engine/engines/safe_engine.py
Safe Engine (25% target allocation).

Tickers: SCHD, VOO, VTI, QQQ, HDV

Logic:
  - Buy dips of -5% or more (intraday from prev close)
  - Buy underweights (>2% below target)
  - Trim overweights (>2% above target)
  - Always active, even in PAUSE_AGGRESSIVE mode
  - In SAFE_ONLY mode: receives all new cash
"""

from __future__ import annotations

from typing import List

from api.client_factory  import get_client
from config.settings     import (
    SAFE_TICKERS,
    SAFE_DIP_THRESHOLD,
    SAFE_OVERWEIGHT_TRIM,
    SAFE_UNDERWEIGHT_BUY,
    ENGINE_TARGETS,
)
from core.portfolio      import PortfolioState
from core.risk_manager   import RiskManager, TradeProposal, RiskMode
from core.trade_executor import TradeExecutor
from utils.logger        import get_logger

logger = get_logger(__name__)


class SafeEngine:

    ENGINE = "safe"

    def __init__(self, state: PortfolioState, risk: RiskManager, executor: TradeExecutor):
        self.state    = state
        self.risk     = risk
        self.executor = executor

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def run(self) -> List[TradeProposal]:
        proposals: List[TradeProposal] = []
        proposals += self._scan_dip_buys()
        proposals += self._scan_overweights()
        proposals += self._scan_underweights()
        executed = self.executor.execute_many(proposals)
        logger.info("[Safe] %d proposals → %d executed", len(proposals), len(executed))
        return proposals

    def deploy_cash(self, amount_usd: float) -> List[TradeProposal]:
        """
        Called in SAFE_ONLY mode to route all new cash into safe tickers.
        Distributes proportionally to within-engine weights.
        """
        proposals = []
        engine_target = ENGINE_TARGETS["safe"]  # 0.25
        for ticker, global_target in SAFE_TICKERS.items():
            within_engine_weight = global_target / engine_target if engine_target else 0
            buy_value = amount_usd * within_engine_weight
            if buy_value < 10:
                continue

            pos   = self.state.get_position(ticker)
            price = pos.current_price if pos else 0.0

            proposals.append(TradeProposal(
                ticker    = ticker,
                action    = "BUY",
                engine    = self.ENGINE,
                reason    = "safe_only_cash_deploy",
                value_usd = buy_value,
                price     = price,
            ))
        executed = self.executor.execute_many(proposals)
        logger.info("[Safe/deploy_cash] $%.2f → %d trades", amount_usd, len(executed))
        return proposals

    # ------------------------------------------------------------------
    # Dip buys (-5% or more from prev close)
    # ------------------------------------------------------------------

    def _scan_dip_buys(self) -> List[TradeProposal]:
        if not self.risk.can_buy(self.ENGINE):
            return []

        client    = get_client()
        proposals = []

        try:
            quotes = client.fetch_quotes(list(SAFE_TICKERS.keys()))
        except Exception as exc:
            logger.error("[Safe] Quote fetch failed: %s", exc)
            return []

        for ticker in SAFE_TICKERS:
            quote = quotes.get(ticker)
            if not quote:
                continue
            if quote.change_pct > -SAFE_DIP_THRESHOLD:
                continue

            # Buy proportional to dip severity (deeper dip → larger buy)
            severity  = min(abs(quote.change_pct) / SAFE_DIP_THRESHOLD, 3.0)
            target_pct = SAFE_TICKERS[ticker]
            buy_value  = self.state.portfolio_value * target_pct * 0.1 * severity
            buy_value  = min(buy_value, self.state.cash_balance * 0.3)
            if buy_value < 10:
                continue

            logger.info(
                "[Safe] DIP buy: %s change=%.1f%% → $%.2f",
                ticker, quote.change_pct * 100, buy_value,
            )
            proposals.append(TradeProposal(
                ticker    = ticker,
                action    = "BUY",
                engine    = self.ENGINE,
                reason    = f"dip_buy_{abs(int(quote.change_pct*100))}pct",
                value_usd = buy_value,
                price     = quote.last,
            ))
        return proposals

    # ------------------------------------------------------------------
    # Overweight trim
    # ------------------------------------------------------------------

    def _scan_overweights(self) -> List[TradeProposal]:
        proposals = []
        for ticker in SAFE_TICKERS:
            pos = self.state.get_position(ticker)
            if not pos:
                continue
            if pos.delta_pct < SAFE_OVERWEIGHT_TRIM:
                continue

            trim_value = pos.delta_pct * self.state.portfolio_value
            if trim_value < 10:
                continue

            logger.info(
                "[Safe] OVERWEIGHT trim: %s delta=+%.2f%% → sell $%.2f",
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
            return []

        proposals = []
        for ticker in SAFE_TICKERS:
            pos    = self.state.get_position(ticker)
            target = self.state.target_pct_of(ticker)
            actual = self.state.actual_pct_of(ticker)
            delta  = actual - target

            if delta > -SAFE_UNDERWEIGHT_BUY:
                continue

            buy_value = abs(delta) * self.state.portfolio_value
            buy_value = min(buy_value, self.state.cash_balance * 0.3)
            if buy_value < 10:
                continue

            price = pos.current_price if pos else 0.0
            logger.info(
                "[Safe] UNDERWEIGHT buy: %s delta=%.2f%% → buy $%.2f",
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
