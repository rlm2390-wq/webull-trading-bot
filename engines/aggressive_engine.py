"""
trading_engine/engines/aggressive_engine.py
Aggressive Engine (35% target allocation).

Tickers: MARA, CLSK, SOXL, BITX, TNA, NVDA, SMCI

Logic:
  - Trim on +15% / +25% / +40% gain
  - Dip buy on -10% / -20% / -30% from cost
  - Covered call scan (MARA, CLSK, SOXL, BITX)
  - Respect risk mode: paused if drawdown >= 20%
"""

from __future__ import annotations

from typing import List

from api.client_factory  import get_client
from config.settings     import (
    AGGRESSIVE_TICKERS,
    AGGRESSIVE_TRIM_RULES,
    AGGRESSIVE_DIP_RULES,
    COVERED_CALL_TICKERS,
    COVERED_CALL_OTM_MIN,
    COVERED_CALL_OTM_MAX,
    COVERED_CALL_EXP_MIN,
    COVERED_CALL_EXP_MAX,
)
from core.portfolio      import PortfolioState
from core.risk_manager   import RiskManager, TradeProposal, RiskMode
from core.trade_executor import TradeExecutor
from utils.logger        import get_logger

logger = get_logger(__name__)


class AggressiveEngine:

    ENGINE = "aggressive"

    def __init__(self, state: PortfolioState, risk: RiskManager, executor: TradeExecutor):
        self.state    = state
        self.risk     = risk
        self.executor = executor

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def run(self) -> List[TradeProposal]:
        """
        Build the full trade list for the aggressive engine.
        Returns list of proposals (some may be blocked by risk gate).
        """
        proposals: List[TradeProposal] = []

        if self.risk.current_mode() == RiskMode.SAFE_ONLY:
            logger.info("[Aggressive] Skipped — SAFE_ONLY mode active")
            return proposals

        proposals += self._scan_trims()
        proposals += self._scan_dip_buys()

        executed = self.executor.execute_many(proposals)
        logger.info("[Aggressive] %d proposals → %d executed", len(proposals), len(executed))
        return proposals

    def run_covered_calls(self) -> List[TradeProposal]:
        """Runs on Wednesday. Separate from equity logic."""
        if self.risk.current_mode() in (RiskMode.PAUSE_AGGRESSIVE, RiskMode.SAFE_ONLY):
            logger.info("[Aggressive/CC] Skipped — risk mode %s", self.risk.current_mode().value)
            return []
        proposals = self._scan_covered_calls()
        self.executor.execute_many(proposals)
        return proposals

    # ------------------------------------------------------------------
    # Trim logic
    # ------------------------------------------------------------------

    def _scan_trims(self) -> List[TradeProposal]:
        proposals = []
        for ticker in AGGRESSIVE_TICKERS:
            pos = self.state.get_position(ticker)
            if not pos or pos.shares <= 0 or pos.avg_cost <= 0:
                continue

            gain_pct = (pos.current_price - pos.avg_cost) / pos.avg_cost

            # Find the highest applicable trim rule
            trim_pct = 0.0
            for rule in sorted(AGGRESSIVE_TRIM_RULES, key=lambda r: r["gain_pct"], reverse=True):
                if gain_pct >= rule["gain_pct"]:
                    trim_pct = rule["trim_pct"]
                    break

            if trim_pct <= 0:
                continue

            trim_value = pos.market_value * trim_pct
            logger.info(
                "[Aggressive] TRIM signal: %s gain=%.1f%% → trim %.0f%% ($%.2f)",
                ticker, gain_pct * 100, trim_pct * 100, trim_value,
            )
            proposals.append(TradeProposal(
                ticker    = ticker,
                action    = "SELL",
                engine    = self.ENGINE,
                reason    = f"trim_{int(gain_pct*100)}pct_gain",
                value_usd = trim_value,
                price     = pos.current_price,
            ))
        return proposals

    # ------------------------------------------------------------------
    # Dip buy logic
    # ------------------------------------------------------------------

    def _scan_dip_buys(self) -> List[TradeProposal]:
        if not self.risk.can_buy(self.ENGINE):
            logger.info("[Aggressive] Buys paused (risk mode or cash floor)")
            return []

        proposals = []
        for ticker in AGGRESSIVE_TICKERS:
            pos = self.state.get_position(ticker)
            if not pos or pos.avg_cost <= 0:
                continue

            loss_pct = (pos.current_price - pos.avg_cost) / pos.avg_cost  # negative = loss

            # Find deepest applicable dip rule
            buy_pct = 0.0
            for rule in sorted(AGGRESSIVE_DIP_RULES, key=lambda r: r["loss_pct"], reverse=True):
                if loss_pct <= -rule["loss_pct"]:
                    buy_pct = rule["buy_pct"]
                    break

            if buy_pct <= 0:
                continue

            buy_value = self.state.portfolio_value * buy_pct
            # Don't buy if we'd exhaust too much cash
            if buy_value > self.state.cash_balance * 0.5:
                buy_value = self.state.cash_balance * 0.25

            logger.info(
                "[Aggressive] DIP signal: %s loss=%.1f%% → buy %.0f%% ($%.2f)",
                ticker, loss_pct * 100, buy_pct * 100, buy_value,
            )
            proposals.append(TradeProposal(
                ticker    = ticker,
                action    = "BUY",
                engine    = self.ENGINE,
                reason    = f"dip_buy_{int(abs(loss_pct)*100)}pct_down",
                value_usd = buy_value,
                price     = pos.current_price,
            ))
        return proposals

    # ------------------------------------------------------------------
    # Covered call scanning (Wednesday)
    # ------------------------------------------------------------------

    def _scan_covered_calls(self) -> List[TradeProposal]:
        from api.market_data import next_fridays, expiry_in_range
        client    = get_client()
        proposals = []

        for ticker in COVERED_CALL_TICKERS:
            pos = self.state.get_position(ticker)
            if not pos or pos.shares < 100:
                logger.debug("[CC] %s: skipped (< 100 shares)", ticker)
                continue

            # No covered calls during earnings week
            if has_earnings_within(client, ticker, days=7):
                logger.info("[CC] %s: skipped — earnings within 7 days", ticker)
                continue

            # Find valid expiry Fridays in range
            valid_expiries = [
                f for f in next_fridays(count=4)
                if expiry_in_range(f, COVERED_CALL_EXP_MIN, COVERED_CALL_EXP_MAX)
            ]
            if not valid_expiries:
                logger.debug("[CC] %s: no valid expiry found", ticker)
                continue

            expiry = valid_expiries[0]

            # Calculate OTM strike range
            price      = pos.current_price
            strike_low  = round(price * (1 + COVERED_CALL_OTM_MIN), 2)
            strike_high = round(price * (1 + COVERED_CALL_OTM_MAX), 2)

            # Fetch option chain and find best premium in range
            try:
                chain = client.fetch_option_chain(
                    ticker      = ticker,
                    expiry      = expiry.isoformat(),
                    option_type = "call",
                )
            except Exception as exc:
                logger.warning("[CC] %s: chain fetch failed: %s", ticker, exc)
                continue

            candidates = [
                c for c in chain
                if strike_low <= c.strike <= strike_high and c.bid > 0
            ]
            if not candidates:
                logger.info("[CC] %s: no candidates in %.0f%%–%.0f%% OTM range",
                            ticker, COVERED_CALL_OTM_MIN*100, COVERED_CALL_OTM_MAX*100)
                continue

            # Pick highest bid premium
            best       = max(candidates, key=lambda c: c.bid)
            contracts  = int(pos.shares // 100)
            limit_px   = round((best.bid + best.ask) / 2, 2)

            logger.info(
                "[CC] %s: SELL %d x $%.2f call exp=%s premium=$%.2f",
                ticker, contracts, best.strike, expiry, limit_px,
            )
            proposals.append(TradeProposal(
                ticker    = ticker,
                action    = "SELL_TO_OPEN",
                engine    = self.ENGINE,
                reason    = f"covered_call_{expiry}",
                value_usd = limit_px * contracts * 100,
                price     = limit_px,
                shares    = float(contracts),
                is_option = True,
            ))

            # Execute covered calls directly (special order type)
            try:
                client.place_covered_call(
                    ticker       = ticker,
                    strike       = best.strike,
                    expiry       = expiry.isoformat(),
                    contracts    = contracts,
                    limit_price  = limit_px,
                )
            except Exception as exc:
                logger.error("[CC] %s: order failed: %s", ticker, exc)

        return proposals
