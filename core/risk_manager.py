"""
trading_engine/core/risk_manager.py
Safety rail enforcement and risk mode classification.

RiskManager is the gate every proposed trade passes through before execution.
It enforces:
  - Max trades per day / week
  - Drawdown-based mode escalation
  - No leverage / no naked options (policy guards)
  - Cash bucket minimum
  - Position floor (never reduce below 0.5%)
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from config.settings import (
    MAX_TRADES_PER_DAY, MAX_TRADES_PER_WEEK,
    MIN_POSITION_PCT,
    CASH_MIN_PCT,
    DRAWDOWN_PAUSE_CAPTURE,
    DRAWDOWN_PAUSE_AGGR,
    DRAWDOWN_SAFE_ONLY,
)
from core.portfolio import PortfolioState
from utils.logger   import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Risk mode enum
# ---------------------------------------------------------------------------

class RiskMode(enum.Enum):
    NORMAL             = "normal"
    PAUSE_CAPTURE      = "pause_capture"
    PAUSE_AGGRESSIVE   = "pause_aggressive"
    SAFE_ONLY          = "safe_only"


def classify_risk_mode(drawdown_pct: float) -> RiskMode:
    if drawdown_pct >= DRAWDOWN_SAFE_ONLY:
        return RiskMode.SAFE_ONLY
    if drawdown_pct >= DRAWDOWN_PAUSE_AGGR:
        return RiskMode.PAUSE_AGGRESSIVE
    if drawdown_pct >= DRAWDOWN_PAUSE_CAPTURE:
        return RiskMode.PAUSE_CAPTURE
    return RiskMode.NORMAL


# ---------------------------------------------------------------------------
# Trade proposal
# ---------------------------------------------------------------------------

@dataclass
class TradeProposal:
    ticker:     str
    action:     str        # BUY | SELL
    engine:     str
    reason:     str
    value_usd:  float      # dollar amount to trade
    shares:     float = 0.0
    price:      float = 0.0
    is_option:  bool  = False
    is_capture: bool  = False


@dataclass
class TradeDecision:
    approved:  bool
    proposal:  TradeProposal
    reason:    str = ""


# ---------------------------------------------------------------------------
# Risk manager
# ---------------------------------------------------------------------------

class RiskManager:

    def __init__(self, state: PortfolioState):
        self.state     = state
        self.risk_mode = classify_risk_mode(state.drawdown_pct)

    # ------------------------------------------------------------------
    # Public gate
    # ------------------------------------------------------------------

    def approve(self, proposal: TradeProposal) -> TradeDecision:
        """
        Run all safety checks on a proposed trade.
        Returns TradeDecision(approved=True/False, reason=...).
        """
        checks = [
            self._check_trade_limits,
            self._check_drawdown_mode,
            self._check_cash_minimum,
            self._check_position_floor,
            self._check_no_leverage,
        ]
        for check in checks:
            decision = check(proposal)
            if not decision.approved:
                logger.warning(
                    "TRADE BLOCKED [%s] %s %s %.2f — %s",
                    proposal.engine, proposal.action, proposal.ticker,
                    proposal.value_usd, decision.reason,
                )
                return decision

        logger.debug(
            "TRADE APPROVED [%s] %s %s $%.2f",
            proposal.engine, proposal.action, proposal.ticker, proposal.value_usd,
        )
        return TradeDecision(approved=True, proposal=proposal)

    def current_mode(self) -> RiskMode:
        return self.risk_mode

    def can_buy(self, engine: str = "") -> bool:
        """Quick check: can this engine place buy orders right now?"""
        if self.risk_mode == RiskMode.SAFE_ONLY and engine != "safe":
            return False
        if self.risk_mode == RiskMode.PAUSE_AGGRESSIVE and engine == "aggressive":
            return False
        if self.state.cash_pct < CASH_MIN_PCT:
            return False
        return True

    def can_capture(self) -> bool:
        return self.risk_mode == RiskMode.NORMAL

    def remaining_trades_today(self) -> int:
        return max(0, MAX_TRADES_PER_DAY - self.state.trades_today)

    def remaining_trades_week(self) -> int:
        return max(0, MAX_TRADES_PER_WEEK - self.state.trades_this_week)

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_trade_limits(self, p: TradeProposal) -> TradeDecision:
        if self.state.trades_today >= MAX_TRADES_PER_DAY:
            return TradeDecision(False, p,
                f"Daily trade limit reached ({MAX_TRADES_PER_DAY}/day)")
        if self.state.trades_this_week >= MAX_TRADES_PER_WEEK:
            return TradeDecision(False, p,
                f"Weekly trade limit reached ({MAX_TRADES_PER_WEEK}/week)")
        return TradeDecision(True, p)

    def _check_drawdown_mode(self, p: TradeProposal) -> TradeDecision:
        mode = self.risk_mode

        if mode == RiskMode.SAFE_ONLY:
            if p.engine != "safe" and p.action == "BUY":
                return TradeDecision(False, p,
                    f"SAFE_ONLY mode: drawdown={self.state.drawdown_pct:.1%}; "
                    "only safe engine buys allowed")

        if mode == RiskMode.PAUSE_AGGRESSIVE:
            if p.engine == "aggressive" and p.action == "BUY":
                return TradeDecision(False, p,
                    f"PAUSE_AGGRESSIVE mode: drawdown={self.state.drawdown_pct:.1%}")

        if mode in (RiskMode.PAUSE_CAPTURE, RiskMode.PAUSE_AGGRESSIVE, RiskMode.SAFE_ONLY):
            if p.is_capture:
                return TradeDecision(False, p,
                    f"Capture trades paused in {mode.value} mode")

        return TradeDecision(True, p)

    def _check_cash_minimum(self, p: TradeProposal) -> TradeDecision:
        if p.action == "BUY" and self.state.cash_pct < CASH_MIN_PCT:
            return TradeDecision(False, p,
                f"Cash below minimum ({self.state.cash_pct:.1%} < {CASH_MIN_PCT:.1%})")
        return TradeDecision(True, p)

    def _check_position_floor(self, p: TradeProposal) -> TradeDecision:
        if p.action != "SELL":
            return TradeDecision(True, p)
        pos = self.state.get_position(p.ticker)
        if not pos:
            return TradeDecision(True, p)
        # Estimate remaining pct after sale
        sale_pct      = p.value_usd / self.state.portfolio_value if self.state.portfolio_value else 0
        remaining_pct = pos.actual_pct - sale_pct
        if remaining_pct < MIN_POSITION_PCT and remaining_pct > 0:
            return TradeDecision(False, p,
                f"Sale would reduce {p.ticker} below floor "
                f"({remaining_pct:.2%} < {MIN_POSITION_PCT:.2%})")
        return TradeDecision(True, p)

    def _check_no_leverage(self, p: TradeProposal) -> TradeDecision:
        # Naked options are never approved (only covered calls are generated upstream)
        # This is a backstop guard
        if p.is_option and p.action == "BUY":
            return TradeDecision(False, p,
                "Long option purchases not allowed (no leverage rule)")
        return TradeDecision(True, p)

    # ------------------------------------------------------------------
    # Increment counters (called after a trade is executed)
    # ------------------------------------------------------------------

    def record_trade(self) -> None:
        self.state.trades_today     += 1
        self.state.trades_this_week += 1
