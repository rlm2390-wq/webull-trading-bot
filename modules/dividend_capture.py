"""
trading_engine/modules/dividend_capture.py
Dividend Capture Module — strict, small, moderate-engine-only.

Eligible tickers: JEPI, JEPQ, QYLD, RYLD, MPLX, DIVO, VYM, DGRO, SCHD

Entry conditions (ALL must pass):
  1. Ex-date within 3 days
  2. RSI < 65
  3. No -3% day
  4. No earnings within 7 days
  5. Cash bucket > 3%
  6. Ticker not overweight
  7. Total capture exposure < 2%
  8. Risk mode == NORMAL

Position size: 0.5-1% of portfolio per capture

Exit rules (ANY triggers exit):
  - Price returns to pre-ex-date level
  - Price +0.5-1% from entry
  - 5 trading days elapsed
  - RSI > 70

All profits → cash bucket.
"""

from __future__ import annotations

import datetime
import uuid
from typing import List, Optional

import sqlalchemy as sa

from api.client_factory  import get_client
from config.settings     import (
    DIVIDEND_CAPTURE_TICKERS,
    CAPTURE_EX_DATE_WINDOW,
    CAPTURE_RSI_ENTRY_MAX,
    CAPTURE_RSI_EXIT_MIN,
    CAPTURE_NO_LOSS_DAY_PCT,
    CAPTURE_EARNINGS_WINDOW,
    CAPTURE_SIZE_MIN_PCT,
    CAPTURE_SIZE_MAX_PCT,
    CAPTURE_MAX_TOTAL_PCT,
    CAPTURE_MAX_HOLD_DAYS,
    CAPTURE_PROFIT_TARGET_PCT,
    CASH_MIN_PCT,
)
from core.portfolio      import PortfolioState
from core.risk_manager   import RiskManager, TradeProposal, RiskMode
from core.trade_executor import TradeExecutor
from db.database         import get_db
from utils.logger        import get_logger

logger = get_logger(__name__)


class DividendCaptureModule:

    ENGINE = "capture"

    def __init__(self, state: PortfolioState, risk: RiskManager, executor: TradeExecutor):
        self.state    = state
        self.risk     = risk
        self.executor = executor

    # ------------------------------------------------------------------
    # Entry scan (Wednesday)
    # ------------------------------------------------------------------

    def scan_entries(self) -> List[TradeProposal]:
        """Check all eligible tickers for capture entry opportunities."""
        if not self.risk.can_capture():
            logger.info("[Capture] Skipped — risk mode: %s", self.risk.current_mode().value)
            return []

        if self.state.cash_pct < CASH_MIN_PCT:
            logger.info("[Capture] Skipped — cash below minimum")
            return []

        # Check total current capture exposure
        current_exposure = self._current_capture_exposure()
        if current_exposure >= CAPTURE_MAX_TOTAL_PCT:
            logger.info(
                "[Capture] Skipped — max exposure reached (%.2f%%)",
                current_exposure * 100,
            )
            return []

        client    = get_client()
        proposals = []

        # Get upcoming dividends
        upcoming = self._get_upcoming_dividends()
        if not upcoming:
            logger.debug("[Capture] No upcoming ex-dates found")
            return []

        for event in upcoming:
            ticker  = event["ticker"]
            ex_date = event["ex_date"]

            days_to_ex = (ex_date - datetime.date.today()).days
            if not (0 <= days_to_ex <= CAPTURE_EX_DATE_WINDOW):
                continue

            proposal = self._evaluate_entry(client, ticker, ex_date, current_exposure)
            if proposal:
                proposals.append(proposal)
                current_exposure += proposal.value_usd / self.state.portfolio_value

        executed = self.executor.execute_many(proposals)
        for p, r in zip(proposals[:len(executed)], executed):
            self._record_capture_open(p, r, next(
                e["ex_date"] for e in upcoming if e["ticker"] == p.ticker
            ))

        logger.info("[Capture/Entry] %d signals → %d entries", len(proposals), len(executed))
        return proposals

    # ------------------------------------------------------------------
    # Exit scan (Friday + daily)
    # ------------------------------------------------------------------

    def scan_exits(self) -> List[TradeProposal]:
        """Check all open capture positions for exit conditions."""
        open_captures = self._get_open_captures()
        if not open_captures:
            return []

        client    = get_client()
        tickers   = [r["ticker"] for r in open_captures]
        proposals = []

        try:
            quotes = client.fetch_quotes(tickers)
        except Exception as exc:
            logger.error("[Capture] Quote fetch failed: %s", exc)
            return []

        for cap in open_captures:
            ticker      = cap["ticker"]
            entry_price = float(cap["entry_price"])
            hold_days   = int(cap["hold_days"] or 0)
            cap_id      = cap["id"]
            quote       = quotes.get(ticker)

            if not quote:
                continue

            current_price = quote.last
            gain_pct      = (current_price - entry_price) / entry_price if entry_price else 0

            exit_reason: Optional[str] = None

            # 1. Profit target (+0.5%)
            if gain_pct >= CAPTURE_PROFIT_TARGET_PCT:
                exit_reason = "price_target"

            # 2. Max hold days (5 trading days)
            elif hold_days >= CAPTURE_MAX_HOLD_DAYS:
                exit_reason = "days_held"

            # 3. RSI > 70
            elif (rsi := fetch_rsi(client, ticker)) and rsi > CAPTURE_RSI_EXIT_MIN:
                exit_reason = f"rsi_{rsi:.0f}"

            # 4. Price returned to pre-ex-date level (approximate: within 0.1% of entry)
            elif abs(gain_pct) < 0.001 and hold_days >= 2:
                exit_reason = "pre_ex_return"

            if exit_reason:
                logger.info(
                    "[Capture] EXIT signal: %s reason=%s gain=%.2f%%",
                    ticker, exit_reason, gain_pct * 100,
                )
                pos       = self.state.get_position(ticker)
                cap_shares = float(cap["shares"])
                sell_value = cap_shares * current_price

                proposals.append(TradeProposal(
                    ticker    = ticker,
                    action    = "SELL",
                    engine    = self.ENGINE,
                    reason    = f"capture_exit_{exit_reason}",
                    value_usd = sell_value,
                    shares    = cap_shares,
                    price     = current_price,
                    is_capture= True,
                ))

        executed = self.executor.execute_many(proposals)
        for p, r in zip(proposals[:len(executed)], executed):
            self._record_capture_close(p, r, exit_reason="capture_exit")

        logger.info("[Capture/Exit] %d signals → %d exits", len(proposals), len(executed))
        return proposals

    # ------------------------------------------------------------------
    # Entry evaluation
    # ------------------------------------------------------------------

    def _evaluate_entry(
        self,
        client,
        ticker:     str,
        ex_date:    datetime.date,
        current_exp: float,
    ) -> Optional[TradeProposal]:

        # 1. Get quote
        try:
            quote = client.fetch_quote(ticker)
        except Exception as exc:
            logger.warning("[Capture] Quote failed for %s: %s", ticker, exc)
            return None

        # 2. No bad day (-3%)
        if is_bad_day(quote, threshold_pct=CAPTURE_NO_LOSS_DAY_PCT):
            logger.debug("[Capture] %s: bad day %.1f%%", ticker, quote.change_pct * 100)
            return None

        # 3. RSI < 65
        rsi = fetch_rsi(client, ticker)
        if rsi is None or rsi >= CAPTURE_RSI_ENTRY_MAX:
            logger.debug("[Capture] %s: RSI %.1f >= %d", ticker, rsi or 0, CAPTURE_RSI_ENTRY_MAX)
            return None

        # 4. No earnings within 7 days
        if has_earnings_within(client, ticker, days=CAPTURE_EARNINGS_WINDOW):
            logger.info("[Capture] %s: earnings within 7 days — skip", ticker)
            return None

        # 5. Ticker not overweight
        pos = self.state.get_position(ticker)
        if pos and pos.delta_pct > 0:
            logger.debug("[Capture] %s: already overweight", ticker)
            return None

        # 6. Room under max exposure
        room_pct    = CAPTURE_MAX_TOTAL_PCT - current_exp
        size_pct    = min(CAPTURE_SIZE_MAX_PCT, max(CAPTURE_SIZE_MIN_PCT, room_pct))
        buy_value   = self.state.portfolio_value * size_pct
        buy_value   = min(buy_value, self.state.cash_balance * 0.5)
        if buy_value < 50:
            return None

        logger.info(
            "[Capture] ENTRY: %s ex=%s RSI=%.1f size=$%.2f",
            ticker, ex_date, rsi, buy_value,
        )
        return TradeProposal(
            ticker     = ticker,
            action     = "BUY",
            engine     = self.ENGINE,
            reason     = f"capture_entry_ex{ex_date}",
            value_usd  = buy_value,
            price      = quote.last,
            is_capture = True,
        )

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _get_upcoming_dividends(self) -> list[dict]:
        """Get dividend events within the ex-date window from DB."""
        today    = datetime.date.today()
        window   = today + datetime.timedelta(days=CAPTURE_EX_DATE_WINDOW)
        with get_db() as db:
            rows = db.execute(sa.text("""
                SELECT ticker, ex_date, amount
                FROM dividend_events
                WHERE ticker = ANY(:tickers)
                  AND ex_date BETWEEN :today AND :window
                  AND status = 'pending'
                ORDER BY ex_date
            """), {
                "tickers": DIVIDEND_CAPTURE_TICKERS,
                "today":   today,
                "window":  window,
            }).fetchall()
        return [{"ticker": r[0], "ex_date": r[1], "amount": float(r[2] or 0)} for r in rows]

    def _get_open_captures(self) -> list[dict]:
        with get_db() as db:
            rows = db.execute(sa.text("""
                SELECT id, ticker, shares, entry_price, hold_days, ex_date
                FROM capture_positions
                WHERE status = 'open'
                ORDER BY opened_at
            """)).fetchall()
        return [
            {"id": r[0], "ticker": r[1], "shares": r[2],
             "entry_price": r[3], "hold_days": r[4], "ex_date": r[5]}
            for r in rows
        ]

    def _current_capture_exposure(self) -> float:
        """Return current capture exposure as a fraction of portfolio."""
        with get_db() as db:
            row = db.execute(sa.text("""
                SELECT COALESCE(SUM(shares * entry_price), 0)
                FROM capture_positions WHERE status = 'open'
            """)).scalar()
        total_captured = float(row or 0)
        pv = self.state.portfolio_value
        return total_captured / pv if pv else 0.0

    def _record_capture_open(self, proposal: TradeProposal, order_result, ex_date: datetime.date) -> None:
        shares = proposal.value_usd / proposal.price if proposal.price else 0
        with get_db() as db:
            db.execute(sa.text("""
                INSERT INTO capture_positions
                    (ticker, ex_date, shares, entry_price, cost_basis, status)
                VALUES (:ticker, :ex_date, :shares, :price, :cost, 'open')
            """), {
                "ticker":   proposal.ticker,
                "ex_date":  ex_date,
                "shares":   shares,
                "price":    proposal.price,
                "cost":     proposal.value_usd,
            })

    def _record_capture_close(self, proposal: TradeProposal, order_result, exit_reason: str) -> None:
        with get_db() as db:
            db.execute(sa.text("""
                UPDATE capture_positions
                SET status       = 'closed',
                    closed_at    = NOW(),
                    exit_price   = :exit_price,
                    exit_value   = :exit_value,
                    total_pnl    = :exit_value - cost_basis,
                    exit_reason  = :reason
                WHERE ticker = :ticker AND status = 'open'
                ORDER BY opened_at ASC
                LIMIT 1
            """), {
                "ticker":     proposal.ticker,
                "exit_price": proposal.price,
                "exit_value": proposal.value_usd,
                "reason":     exit_reason,
            })

    def increment_hold_days(self) -> None:
        """Call daily to tick hold_days on open capture positions."""
        with get_db() as db:
            db.execute(sa.text("""
                UPDATE capture_positions
                SET hold_days = hold_days + 1
                WHERE status = 'open'
            """))
