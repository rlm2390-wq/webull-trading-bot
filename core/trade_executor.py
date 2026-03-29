PAPER_TRADING = True

"""
trading_engine/core/trade_executor.py
Converts approved TradeProposals into Webull orders and logs them to the DB.

Flow:
  1. Receive approved TradeDecision
  2. Calculate exact share count from dollar value
  3. Place limit order (mid-price) or market order
  4. Log to trades_log
  5. Return filled OrderResult
"""

from __future__ import annotations

import datetime
import uuid
from typing import Optional

import sqlalchemy as sa

from api.client_factory import get_client
from core.risk_manager  import TradeProposal, TradeDecision, RiskManager
from config.settings    import DRY_RUN
from db.database        import get_db
from utils.logger       import get_logger

logger = get_logger(__name__)


class TradeExecutor:

    def __init__(self, risk_manager: RiskManager):
        self.risk = risk_manager

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def execute(self, proposal: TradeProposal) -> Optional["OrderResult"]:
        """
        Gate through risk, then execute.
        Returns OrderResult on success, None if blocked or failed.
        """
        decision = self.risk.approve(proposal)
        if not decision.approved:
            return None

        result = self._place_order(proposal)
        if result:
            self._log_trade(proposal, result)
            self.risk.record_trade()
        return result

    def execute_many(self, proposals: list[TradeProposal]) -> list["OrderResult"]:
        results = []
        for p in proposals:
            # Re-check limits before each trade (counters update as we go)
            if self.risk.remaining_trades_today() <= 0:
                logger.warning("Daily trade limit hit — stopping batch")
                break
            if self.risk.remaining_trades_week() <= 0:
                logger.warning("Weekly trade limit hit — stopping batch")
                break
            r = self.execute(p)
            if r:
                results.append(r)
        return results

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def _place_order(self, p: TradeProposal) -> Optional["OrderResult"]:
        client = get_client()

        # --------------------------------------------------------------
        # PAPER TRADING SAFETY SWITCH
        # --------------------------------------------------------------
        if PAPER_TRADING:
            logger.info(
                "[PAPER TRADE] %s %s %s | value=$%.2f | reason=%s",
                p.engine.upper(), p.action, p.ticker,
                p.value_usd, p.reason
            )

            # Create a fake OrderResult-like object
            class FakeResult:
                def __init__(self):
                    self.order_id = f"paper-{uuid.uuid4()}"
                    self.shares   = p.shares or 0
                    self.price    = p.price or 0
                    self.status   = "paper"
                    self.dry_run  = True

            return FakeResult()
        # --------------------------------------------------------------

        try:
            # Calculate shares from dollar value
            if p.shares and p.shares > 0:
                shares = p.shares
            elif p.price and p.price > 0:
                shares = p.value_usd / p.price
            else:
                # Fetch live quote for price
                quote  = client.fetch_quote(p.ticker)
                shares = p.value_usd / quote.last
                p.price = quote.last

            shares = max(round(shares, 4), 0.0001)

            # Use limit orders at mid-price for safety
            if p.price and p.price > 0:
                # 0.05% slippage buffer on buys, 0.05% less on sells
                if p.action == "BUY":
                    limit = round(p.price * 1.0005, 4)
                else:
                    limit = round(p.price * 0.9995, 4)

                result = client.place_limit_order(
                    ticker=p.ticker, action=p.action,
                    shares=shares, price=limit, reason=p.reason,
                )
            else:
                result = client.place_market_order(
                    ticker=p.ticker, action=p.action,
                    shares=shares, reason=p.reason,
                )

            logger.info(
                "ORDER PLACED | %s %s %s | shares=%.4f | price=%s | id=%s",
                p.engine.upper(), p.action, p.ticker,
                shares, p.price, result.order_id,
            )
            return result

        except Exception as exc:
            logger.error(
                "ORDER FAILED | %s %s: %s", p.action, p.ticker, exc
            )
            self._log_failed_trade(p, str(exc))
            return None

    # ------------------------------------------------------------------
    # DB logging
    # ------------------------------------------------------------------

    def _log_trade(self, p: TradeProposal, r: "OrderResult") -> None:
        with get_db() as db:
            db.execute(sa.text("""
                INSERT INTO trades_log
                    (trade_id, ticker, engine, action, reason,
                     shares, limit_price, fill_value,
                     status, webull_order_id, dry_run)
                VALUES
                    (:tid, :ticker, :engine, :action, :reason,
                     :shares, :price, :value,
                     :status, :oid, :dry_run)
            """), {
                "tid":     str(uuid.uuid4()),
                "ticker":  p.ticker,
                "engine":  p.engine,
                "action":  p.action,
                "reason":  p.reason,
                "shares":  r.shares,
                "price":   r.price,
                "value":   p.value_usd,
                "status":  r.status,
                "oid":     r.order_id,
                "dry_run": r.dry_run or DRY_RUN,
            })

    def _log_failed_trade(self, p: TradeProposal, error: str) -> None:
        with get_db() as db:
            db.execute(sa.text("""
                INSERT INTO trades_log
                    (trade_id, ticker, engine, action, reason,
                     shares, limit_price, fill_value,
                     status, notes, dry_run)
                VALUES
                    (:tid, :ticker, :engine, :action, :reason,
                     0, 0, 0,
                     'failed', :error, :dry_run)
            """), {
                "tid":     str(uuid.uuid4()),
                "ticker":  p.ticker,
                "engine":  p.engine,
                "action":  p.action,
                "reason":  p.reason,
                "error":   error,
                "dry_run": DRY_RUN,
            })
