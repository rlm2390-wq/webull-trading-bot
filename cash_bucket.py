"""
trading_engine/core/cash_bucket.py
Cash bucket state and deployment logic.

Handles:
  - Reading current cash state
  - Detecting new deposits
  - Calculating deploy amounts (Monday rule, excess rule)
  - Staging deposit deployment over 2-4 days
  - Persisting deposit events
"""

from __future__ import annotations

import datetime
import math
from typing import Optional

import sqlalchemy as sa

from config.settings import (
    CASH_MIN_PCT,
    CASH_TARGET_HIGH_PCT,
    CASH_DEPLOY_PCT_MONDAY,
    CASH_DEPLOY_EXTRA_THRESHOLD,
    DEPOSIT_DETECTION_THRESHOLD,
    DEPOSIT_DEPLOY_DAYS_MIN,
    DEPOSIT_DEPLOY_DAYS_MAX,
    ENGINE_TARGETS,
)
from core.portfolio import PortfolioState
from db.database    import get_db
from utils.logger   import get_logger

logger = get_logger(__name__)


class CashBucket:

    def __init__(self, state: PortfolioState):
        self.state = state

    # ------------------------------------------------------------------
    # Deploy amount for today
    # ------------------------------------------------------------------

    def monday_deploy_amount(self) -> float:
        """
        On Monday, deploy 25-33% (config: 30%) of cash bucket.
        If cash > 5%, deploy the excess above 5% on top.
        """
        cash   = self.state.cash_balance
        pv     = self.state.portfolio_value
        if not pv:
            return 0.0

        # Base deploy
        deploy = cash * CASH_DEPLOY_PCT_MONDAY

        # Extra if cash > high threshold
        if self.state.cash_pct > CASH_DEPLOY_EXTRA_THRESHOLD:
            excess  = (self.state.cash_pct - CASH_DEPLOY_EXTRA_THRESHOLD) * pv
            deploy += excess

        # Never deploy so much that cash falls below minimum
        min_cash = CASH_MIN_PCT * pv
        deploy   = min(deploy, cash - min_cash)
        deploy   = max(deploy, 0.0)

        logger.info("Monday deploy amount: $%.2f (cash=%.1f%%)", deploy, self.state.cash_pct * 100)
        return deploy

    def can_deploy(self) -> bool:
        return self.state.cash_pct >= CASH_MIN_PCT

    # ------------------------------------------------------------------
    # New deposit detection
    # ------------------------------------------------------------------

    def detect_new_deposit(
        self,
        previous_cash:   float,
        premiums_today:  float = 0.0,
        dividends_today: float = 0.0,
        trims_today:     float = 0.0,
    ) -> Optional[float]:
        """
        Compute implied new deposit using the formula:
          new_deposit = current_cash - previous_cash - (premiums + dividends + trims)

        Returns deposit amount if > threshold, else None.
        """
        current_cash = self.state.cash_balance
        implied      = current_cash - previous_cash - premiums_today - dividends_today - trims_today

        if implied >= DEPOSIT_DETECTION_THRESHOLD:
            logger.info("New deposit detected: $%.2f", implied)
            self._record_deposit(implied)
            return implied
        return None

    def _record_deposit(self, amount: float) -> None:
        today      = datetime.date.today()
        deploy_end = today + datetime.timedelta(days=DEPOSIT_DEPLOY_DAYS_MAX)
        with get_db() as db:
            db.execute(sa.text("""
                INSERT INTO deposits_log
                    (amount, deploy_start, deploy_end, status)
                VALUES (:amount, :start, :end, 'pending')
            """), {
                "amount": amount,
                "start":  today,
                "end":    deploy_end,
            })

    # ------------------------------------------------------------------
    # Staged deposit deployment
    # ------------------------------------------------------------------

    def get_pending_deposit_allocation(self) -> dict[str, float]:
        """
        Return per-engine dollar amounts from pending deposits to deploy today.
        Spreads evenly across DEPLOY_DAYS_MIN to DEPLOY_DAYS_MAX.
        """
        today = datetime.date.today()
        with get_db() as db:
            rows = db.execute(sa.text("""
                SELECT id, amount, deploy_start, deploy_end, deployed_pct
                FROM deposits_log
                WHERE status IN ('pending', 'deploying')
                  AND deploy_start <= :today
                  AND deploy_end   >= :today
            """), {"today": today}).fetchall()

        if not rows:
            return {}

        total_to_deploy = 0.0
        for row in rows:
            dep_id, amount, start, end, deployed_pct = row
            days_total     = max((end - start).days, 1)
            days_elapsed   = max((today - start).days + 1, 1)
            target_deployed = min(days_elapsed / days_total, 1.0) * 100
            still_to_deploy = ((target_deployed - (deployed_pct or 0)) / 100) * amount
            total_to_deploy += max(still_to_deploy, 0.0)

        # Split by engine targets
        return {
            eng: total_to_deploy * pct
            for eng, pct in ENGINE_TARGETS.items()
        }

    # ------------------------------------------------------------------
    # Post-trade bookkeeping
    # ------------------------------------------------------------------

    def record_source(
        self,
        dividends: float = 0.0,
        premiums:  float = 0.0,
        trims:     float = 0.0,
        deposits:  float = 0.0,
    ) -> None:
        """Record cash inflows for today's cash_bucket row."""
        with get_db() as db:
            db.execute(sa.text("""
                UPDATE cash_bucket
                SET source_dividends = source_dividends + :div,
                    source_premiums  = source_premiums  + :prem,
                    source_trims     = source_trims     + :trims,
                    source_deposits  = source_deposits  + :dep
                WHERE id = (SELECT id FROM cash_bucket ORDER BY recorded_at DESC LIMIT 1)
            """), {
                "div":   dividends,
                "prem":  premiums,
                "trims": trims,
                "dep":   deposits,
            })

    def mark_deposits_deploying(self) -> None:
        with get_db() as db:
            db.execute(sa.text("""
                UPDATE deposits_log SET status = 'deploying'
                WHERE status = 'pending'
            """))

    def update_deposit_progress(self, amount_deployed: float) -> None:
        """Increment deployed_pct on active deposit rows."""
        today = datetime.date.today()
        with get_db() as db:
            rows = db.execute(sa.text("""
                SELECT id, amount FROM deposits_log
                WHERE status = 'deploying'
                  AND deploy_start <= :today AND deploy_end >= :today
            """), {"today": today}).fetchall()

            remaining = amount_deployed
            for dep_id, dep_amount in rows:
                if remaining <= 0:
                    break
                portion    = min(remaining, dep_amount)
                add_pct    = (portion / dep_amount) * 100 if dep_amount else 0
                remaining -= portion
                db.execute(sa.text("""
                    UPDATE deposits_log
                    SET deployed_pct = LEAST(deployed_pct + :add, 100),
                        status = CASE WHEN deployed_pct + :add >= 100
                                      THEN 'deployed' ELSE status END
                    WHERE id = :id
                """), {"add": add_pct, "id": dep_id})
