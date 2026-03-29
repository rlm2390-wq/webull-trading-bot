"""
trading_engine/core/decision_loop.py
The 13-step decision tree, executed in strict order every run.

Step 1:  Refresh data
Step 2:  Detect new deposits
Step 3:  Update engine weights
Step 4:  Update ticker weights
Step 5:  Aggressive engine logic
Step 6:  Moderate engine logic
Step 7:  Dividend capture module
Step 8:  Safe engine logic
Step 9:  Cash bucket logic
Step 10: Risk mode logic
Step 11: Generate trade list
Step 12: Execute trades
Step 13: Log everything
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import List, Optional

import sqlalchemy as sa

from core.state_manager   import StateManager
from core.portfolio       import PortfolioState
from core.risk_manager    import RiskManager, RiskMode
from core.trade_executor  import TradeExecutor
from core.cash_bucket     import CashBucket
from core.scheduler_log   import task_run
from engines.aggressive_engine import AggressiveEngine
from engines.moderate_engine   import ModerateEngine
from engines.safe_engine       import SafeEngine
from modules.dividend_capture  import DividendCaptureModule
from modules.dividend_sync     import DividendSync
from db.database               import get_db
from utils.logger              import get_logger

logger = get_logger(__name__)


@dataclass
class RunContext:
    """Carries shared state through all 13 steps of a single run."""
    mode:          str                      # daily | monday | wednesday | friday
    state:         Optional[PortfolioState] = None
    risk:          Optional[RiskManager]    = None
    executor:      Optional[TradeExecutor]  = None
    cash:          Optional[CashBucket]     = None
    new_deposit:   float                    = 0.0
    proposals:     list                     = field(default_factory=list)
    previous_cash: float                    = 0.0


class DecisionLoop:
    """
    Executes the full 13-step decision tree for a given run mode.

    Usage:
        loop = DecisionLoop()
        loop.run("monday")
        loop.run("daily")
    """

    def __init__(self):
        self.state_manager = StateManager()
        self.div_sync      = DividendSync()

    # ------------------------------------------------------------------
    # Entry points (called by scheduler)
    # ------------------------------------------------------------------

    def run_daily(self):
        with task_run("daily_loop"):
            ctx = RunContext(mode="daily")
            self._execute(ctx)

    def run_monday(self):
        with task_run("monday_loop"):
            ctx = RunContext(mode="monday")
            self._execute(ctx)

    def run_wednesday(self):
        with task_run("wednesday_loop"):
            ctx = RunContext(mode="wednesday")
            self._execute(ctx)

    def run_friday(self):
        with task_run("friday_loop"):
            ctx = RunContext(mode="friday")
            self._execute(ctx)

    # ------------------------------------------------------------------
    # Master pipeline
    # ------------------------------------------------------------------

    def _execute(self, ctx: RunContext) -> None:
        logger.info("=" * 60)
        logger.info("DECISION LOOP START | mode=%s | %s", ctx.mode, datetime.datetime.now())
        logger.info("=" * 60)

        self._step1_refresh_data(ctx)
        self._step2_detect_deposits(ctx)
        self._step3_update_engine_weights(ctx)
        self._step4_update_ticker_weights(ctx)
        self._step5_aggressive_engine(ctx)
        self._step6_moderate_engine(ctx)
        self._step7_dividend_capture(ctx)
        self._step8_safe_engine(ctx)
        self._step9_cash_bucket(ctx)
        self._step10_risk_mode(ctx)
        self._step11_generate_trade_list(ctx)
        self._step12_execute_trades(ctx)
        self._step13_log_everything(ctx)

        logger.info("=" * 60)
        logger.info("DECISION LOOP END | mode=%s", ctx.mode)
        logger.info("=" * 60)

    # ------------------------------------------------------------------
    # Step 1 — Refresh data
    # ------------------------------------------------------------------

    def _step1_refresh_data(self, ctx: RunContext) -> None:
        logger.info("[Step 1] Refreshing portfolio data")

        # Save previous cash before refresh
        ctx.previous_cash = self._get_last_cash_balance()

        # Sync dividend calendar daily
        self.div_sync.sync()

        # Build fresh state
        ctx.state    = self.state_manager.build_state()
        ctx.risk     = RiskManager(ctx.state)
        ctx.executor = TradeExecutor(ctx.risk)
        ctx.cash     = CashBucket(ctx.state)

        logger.info(
            "[Step 1] Portfolio=%.2f | Cash=%.1f%% | Drawdown=%.1f%% | Mode=%s",
            ctx.state.portfolio_value,
            ctx.state.cash_pct * 100,
            ctx.state.drawdown_pct * 100,
            ctx.risk.current_mode().value,
        )

    # ------------------------------------------------------------------
    # Step 2 — Detect new deposits
    # ------------------------------------------------------------------

    def _step2_detect_deposits(self, ctx: RunContext) -> None:
        logger.info("[Step 2] Checking for new deposits")

        # Get today's known cash inflows from DB
        premiums, dividends, trims = self._get_todays_inflows()

        deposit = ctx.cash.detect_new_deposit(
            previous_cash   = ctx.previous_cash,
            premiums_today  = premiums,
            dividends_today = dividends,
            trims_today     = trims,
        )
        if deposit:
            ctx.new_deposit = deposit
            ctx.cash.mark_deposits_deploying()
            logger.info("[Step 2] New deposit: $%.2f — queued for deployment", deposit)
        else:
            logger.debug("[Step 2] No new deposit detected")

    # ------------------------------------------------------------------
    # Step 3 — Update engine weights
    # ------------------------------------------------------------------

    def _step3_update_engine_weights(self, ctx: RunContext) -> None:
        logger.info("[Step 3] Engine weight check")
        for eng, alloc in ctx.state.engine_allocs.items():
            logger.info(
                "  %-12s target=%.1f%%  actual=%.1f%%  delta=%+.1f%%",
                eng,
                alloc.target_pct * 100,
                alloc.actual_pct * 100,
                alloc.delta_pct  * 100,
            )

    # ------------------------------------------------------------------
    # Step 4 — Update ticker weights
    # ------------------------------------------------------------------

    def _step4_update_ticker_weights(self, ctx: RunContext) -> None:
        logger.info("[Step 4] Ticker weight check")
        overweights  = ctx.state.overweight_tickers(threshold=0.015)
        underweights = ctx.state.underweight_tickers(threshold=0.015)
        if overweights:
            logger.info("  Overweight: %s", [p.ticker for p in overweights])
        if underweights:
            logger.info("  Underweight: %s", [p.ticker for p in underweights])

    # ------------------------------------------------------------------
    # Step 5 — Aggressive engine
    # ------------------------------------------------------------------

    def _step5_aggressive_engine(self, ctx: RunContext) -> None:
        logger.info("[Step 5] Aggressive engine | mode=%s", ctx.mode)
        agg = AggressiveEngine(ctx.state, ctx.risk, ctx.executor)

        if ctx.mode in ("daily", "monday", "friday"):
            agg.run()

        if ctx.mode == "wednesday":
            agg.run()
            agg.run_covered_calls()

    # ------------------------------------------------------------------
    # Step 6 — Moderate engine
    # ------------------------------------------------------------------

    def _step6_moderate_engine(self, ctx: RunContext) -> None:
        logger.info("[Step 6] Moderate engine | mode=%s", ctx.mode)
        mod = ModerateEngine(ctx.state, ctx.risk, ctx.executor)

        if ctx.mode in ("friday", "monday"):
            mod.run()
        elif ctx.mode == "daily":
            # Daily: only act if drift is significant (>3%)
            overweights  = ctx.state.overweight_tickers(threshold=0.03)
            underweights = ctx.state.underweight_tickers(threshold=0.03)
            if overweights or underweights:
                mod.run()

    # ------------------------------------------------------------------
    # Step 7 — Dividend capture
    # ------------------------------------------------------------------

    def _step7_dividend_capture(self, ctx: RunContext) -> None:
        logger.info("[Step 7] Dividend capture | mode=%s", ctx.mode)
        cap = DividendCaptureModule(ctx.state, ctx.risk, ctx.executor)

        # Always increment hold days on open captures
        cap.increment_hold_days()

        if ctx.mode == "wednesday":
            cap.scan_entries()

        if ctx.mode in ("friday", "daily"):
            cap.scan_exits()

    # ------------------------------------------------------------------
    # Step 8 — Safe engine
    # ------------------------------------------------------------------

    def _step8_safe_engine(self, ctx: RunContext) -> None:
        logger.info("[Step 8] Safe engine | mode=%s", ctx.mode)
        safe = SafeEngine(ctx.state, ctx.risk, ctx.executor)

        # In SAFE_ONLY mode, route all cash to safe engine
        if ctx.risk.current_mode() == RiskMode.SAFE_ONLY:
            deployable = ctx.cash.monday_deploy_amount()
            if deployable > 0:
                safe.deploy_cash(deployable)
            return

        safe.run()

        # On Monday, also deploy any staged deposit allocation to safe engine
        if ctx.mode == "monday" and ctx.new_deposit:
            deposit_alloc = ctx.cash.get_pending_deposit_allocation()
            safe_amount   = deposit_alloc.get("safe", 0)
            if safe_amount > 10:
                safe.deploy_cash(safe_amount)

    # ------------------------------------------------------------------
    # Step 9 — Cash bucket logic
    # ------------------------------------------------------------------

    def _step9_cash_bucket(self, ctx: RunContext) -> None:
        logger.info("[Step 9] Cash bucket | cash=%.1f%%", ctx.state.cash_pct * 100)

        if ctx.mode == "monday":
            deploy_amount = ctx.cash.monday_deploy_amount()
            if deploy_amount <= 0:
                logger.info("[Step 9] Nothing to deploy")
                return

            # Split by engine targets, excluding safe (handled in step 8)
            deposit_alloc = ctx.cash.get_pending_deposit_allocation()

            agg_amount = deposit_alloc.get("aggressive", deploy_amount * 0.35)
            mod_amount = deposit_alloc.get("moderate",   deploy_amount * 0.40)

            logger.info(
                "[Step 9] Monday deploy: agg=$%.2f mod=$%.2f",
                agg_amount, mod_amount,
            )

            # Deploy into underweights first
            self._deploy_to_underweights(ctx, "aggressive", agg_amount)
            self._deploy_to_underweights(ctx, "moderate",   mod_amount)

            if ctx.new_deposit:
                ctx.cash.update_deposit_progress(deploy_amount)

        # If cash > 5%, deploy excess regardless of day
        elif ctx.state.cash_pct > 0.05:
            excess = (ctx.state.cash_pct - 0.05) * ctx.state.portfolio_value
            logger.info("[Step 9] Excess cash deploy: $%.2f", excess)
            self._deploy_to_underweights(ctx, "moderate", excess * 0.5)
            self._deploy_to_underweights(ctx, "safe",     excess * 0.5)

    # ------------------------------------------------------------------
    # Step 10 — Risk mode
    # ------------------------------------------------------------------

    def _step10_risk_mode(self, ctx: RunContext) -> None:
        mode = ctx.risk.current_mode()
        logger.info(
            "[Step 10] Risk mode: %s | drawdown=%.1f%%",
            mode.value, ctx.state.drawdown_pct * 100,
        )
        if mode != RiskMode.NORMAL:
            logger.warning(
                "  ⚠ RISK ALERT: %s — drawdown %.1f%%",
                mode.value.upper(), ctx.state.drawdown_pct * 100,
            )

    # ------------------------------------------------------------------
    # Step 11 — Generate trade list (summary log only at this point)
    # ------------------------------------------------------------------

    def _step11_generate_trade_list(self, ctx: RunContext) -> None:
        # Trades have already been executed in steps 5-9.
        # This step produces a summary for the log.
        today = datetime.date.today()
        with get_db() as db:
            rows = db.execute(sa.text("""
                SELECT action, ticker, engine, reason, fill_value, status
                FROM trades_log
                WHERE placed_at::date = :today
                ORDER BY placed_at DESC
            """), {"today": today}).fetchall()

        logger.info("[Step 11] Trades today: %d", len(rows))
        for r in rows:
            logger.info(
                "  %s %s [%s] reason=%s val=$%.2f status=%s",
                r[0], r[1], r[2], r[3], float(r[4] or 0), r[5],
            )

    # ------------------------------------------------------------------
    # Step 12 — Execute trades (already done inline; this is a no-op safety flush)
    # ------------------------------------------------------------------

    def _step12_execute_trades(self, ctx: RunContext) -> None:
        # All trades execute in-step (5-9). This step confirms no open
        # limit orders are stale and cancels orders older than 1 day.
        logger.info("[Step 12] Checking for stale open orders")
        try:
            client       = __import__("api.client_factory", fromlist=["get_client"]).get_client()
            open_orders  = client.fetch_open_orders()
            cutoff       = datetime.datetime.now() - datetime.timedelta(hours=6)
            cancelled    = 0
            for order in open_orders:
                placed_str = order.get("createTime", "")
                if not placed_str:
                    continue
                placed = datetime.datetime.fromisoformat(placed_str[:19])
                if placed < cutoff:
                    if client.cancel_order(str(order.get("orderId", ""))):
                        cancelled += 1
            if cancelled:
                logger.info("[Step 12] Cancelled %d stale orders", cancelled)
        except Exception as exc:
            logger.warning("[Step 12] Stale order check failed: %s", exc)

    # ------------------------------------------------------------------
    # Step 13 — Log everything
    # ------------------------------------------------------------------

    def _step13_log_everything(self, ctx: RunContext) -> None:
        logger.info("[Step 13] Final state snapshot")
        s = ctx.state
        logger.info(
            "  Portfolio: $%.2f | Cash: $%.2f (%.1f%%) | Drawdown: %.1f%%",
            s.portfolio_value, s.cash_balance,
            s.cash_pct * 100, s.drawdown_pct * 100,
        )
        logger.info(
            "  Trades today: %d/%d | This week: %d/%d",
            s.trades_today, 3, s.trades_this_week, 7,
        )
        for eng, alloc in s.engine_allocs.items():
            logger.info(
                "  [%s] target=%.1f%% actual=%.1f%% delta=%+.1f%%",
                eng.upper(),
                alloc.target_pct * 100,
                alloc.actual_pct * 100,
                alloc.delta_pct  * 100,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _deploy_to_underweights(
        self, ctx: RunContext, engine: str, amount_usd: float
    ) -> None:
        """Distribute deploy amount across underweight tickers in an engine."""
        if amount_usd <= 0:
            return

        underweights = [
            p for p in ctx.state.tickers_by_engine(engine)
            if p.delta_pct < -0.01
        ]
        if not underweights:
            # No underweights — spread evenly across engine tickers
            from config.settings import (
                AGGRESSIVE_TICKERS, MODERATE_TICKERS, SAFE_TICKERS
            )
            ticker_map = {
                "aggressive": AGGRESSIVE_TICKERS,
                "moderate":   MODERATE_TICKERS,
                "safe":       SAFE_TICKERS,
            }
            tickers = list(ticker_map.get(engine, {}).keys())
            if not tickers:
                return
            per_ticker = amount_usd / len(tickers)
            for ticker in tickers:
                pos = ctx.state.get_position(ticker)
                ctx.executor.execute(__import__("core.risk_manager", fromlist=["TradeProposal"]).TradeProposal(
                    ticker    = ticker,
                    action    = "BUY",
                    engine    = engine,
                    reason    = "cash_deploy_even",
                    value_usd = per_ticker,
                    price     = pos.current_price if pos else 0.0,
                ))
            return

        # Weight allocation by magnitude of underweight
        total_deficit = sum(abs(p.delta_pct) for p in underweights)
        for pos in underweights:
            weight    = abs(pos.delta_pct) / total_deficit if total_deficit else 0
            buy_value = amount_usd * weight
            if buy_value < 10:
                continue
            from core.risk_manager import TradeProposal
            ctx.executor.execute(TradeProposal(
                ticker    = pos.ticker,
                action    = "BUY",
                engine    = engine,
                reason    = "cash_deploy_underweight",
                value_usd = buy_value,
                price     = pos.current_price,
            ))

    def _get_last_cash_balance(self) -> float:
        """Retrieve most recent cash balance from DB."""
        with get_db() as db:
            row = db.execute(sa.text(
                "SELECT cash_balance FROM cash_bucket ORDER BY recorded_at DESC LIMIT 1"
            )).fetchone()
        return float(row[0]) if row else 0.0

    def _get_todays_inflows(self) -> tuple[float, float, float]:
        """Return (premiums, dividends, trims) for today from DB."""
        today = datetime.date.today()
        with get_db() as db:
            row = db.execute(sa.text("""
                SELECT COALESCE(SUM(source_premiums),  0),
                       COALESCE(SUM(source_dividends), 0),
                       COALESCE(SUM(source_trims),     0)
                FROM cash_bucket
                WHERE recorded_at::date = :today
            """), {"today": today}).fetchone()
        if row:
            return float(row[0]), float(row[1]), float(row[2])
        return 0.0, 0.0, 0.0
