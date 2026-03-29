"""
trading_engine/modules/dividend_sync.py
Syncs upcoming dividend ex-dates from Webull into the dividend_events table.
Run daily so the capture module always has fresh data.
"""

from __future__ import annotations

import datetime
from typing import List

import sqlalchemy as sa

from api.client_factory  import get_client
from config.settings     import DIVIDEND_CAPTURE_TICKERS
from db.database         import get_db
from utils.logger        import get_logger

logger = get_logger(__name__)


class DividendSync:

    def sync(self) -> int:
        """
        Fetch upcoming dividends for all capture-eligible tickers
        and upsert into dividend_events.
        Returns count of records upserted.
        """
        client  = get_client()
        count   = 0

        try:
            events = client.fetch_dividend_calendar(DIVIDEND_CAPTURE_TICKERS)
        except Exception as exc:
            logger.error("DividendSync: fetch failed: %s", exc)
            return 0

        for ev in events:
            ticker   = ev.get("ticker", "")
            ex_date  = ev.get("ex_date", "")
            pay_date = ev.get("pay_date", "")
            amount   = ev.get("amount", 0.0)

            if not ticker or not ex_date:
                continue

            try:
                ex_dt  = datetime.date.fromisoformat(ex_date[:10])
                pay_dt = datetime.date.fromisoformat(pay_date[:10]) if pay_date else None
            except ValueError:
                continue

            # Skip past ex-dates older than 30 days
            if ex_dt < datetime.date.today() - datetime.timedelta(days=30):
                continue

            with get_db() as db:
                db.execute(sa.text("""
                    INSERT INTO dividend_events (ticker, ex_date, pay_date, amount, status)
                    VALUES (:ticker, :ex_date, :pay_date, :amount, 'pending')
                    ON CONFLICT (ticker, ex_date) DO UPDATE
                    SET amount   = EXCLUDED.amount,
                        pay_date = EXCLUDED.pay_date
                """), {
                    "ticker":   ticker,
                    "ex_date":  ex_dt,
                    "pay_date": pay_dt,
                    "amount":   amount,
                })
            count += 1

        logger.info("DividendSync: upserted %d dividend events", count)
        return count
