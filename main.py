"""
trading_engine/main.py
Main entry point for the trading engine.

Usage:
    python main.py                    # start the scheduler (production)
    python main.py --run-now daily    # run one cycle immediately and exit
    python main.py --run-now monday
    python main.py --dry-run daily    # dry run (no real orders)
    python main.py --check-db         # verify DB connectivity and exit
"""

import argparse
import os
import sys

# Load .env before any other imports
from dotenv import load_dotenv
load_dotenv()

from utils.logger   import setup_logging
from db.database    import health_check, init_db
from utils.logger   import get_logger

logger = get_logger(__name__)


def parse_args():
    p = argparse.ArgumentParser(description="Automated Trading Engine")
    p.add_argument("--run-now",  metavar="MODE",
                   help="Run one cycle immediately (daily|monday|wednesday|friday)")
    p.add_argument("--dry-run",  metavar="MODE",
                   help="Dry-run one cycle (no real orders placed)")
    p.add_argument("--check-db", action="store_true",
                   help="Verify database connectivity and exit")
    p.add_argument("--init-db",  action="store_true",
                   help="Run schema.sql against the database and exit")
    return p.parse_args()


def run_cycle(mode: str):
    from core.decision_loop import DecisionLoop
    loop = DecisionLoop()
    dispatch = {
        "monday":    loop.run_monday,
        "wednesday": loop.run_wednesday,
        "friday":    loop.run_friday,
        "daily":     loop.run_daily,
    }
    fn = dispatch.get(mode)
    if not fn:
        logger.error("Unknown mode: %s. Choose: daily|monday|wednesday|friday", mode)
        sys.exit(1)
    logger.info("Running one-shot cycle: %s", mode)
    fn()


def init_database():
    """Apply schema.sql to the database."""
    import pathlib
    schema_path = pathlib.Path(__file__).parent / "db" / "schema.sql"
    if not schema_path.exists():
        logger.error("schema.sql not found at %s", schema_path)
        sys.exit(1)
    from db.database import _engine
    from sqlalchemy import text
    sql = schema_path.read_text()
    with _engine.connect() as conn:
        conn.execute(text(sql))
        conn.commit()
    logger.info("Database schema applied successfully")


def main():
    setup_logging()
    args = parse_args()

    # --check-db
    if args.check_db:
        ok = health_check()
        print("Database:", "OK" if ok else "UNREACHABLE")
        sys.exit(0 if ok else 1)

    # --init-db
    if args.init_db:
        if not health_check():
            logger.critical("DB unreachable")
            sys.exit(1)
        init_database()
        sys.exit(0)

    # --dry-run
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
        logger.info("DRY RUN mode enabled")
        if not health_check():
            logger.critical("DB unreachable")
            sys.exit(1)
        run_cycle(args.dry_run)
        sys.exit(0)

    # --run-now
    if args.run_now:
        if not health_check():
            logger.critical("DB unreachable")
            sys.exit(1)
        run_cycle(args.run_now)
        sys.exit(0)

    # Default: start the scheduler
    if not health_check():
        logger.critical("DB unreachable — aborting startup")
        sys.exit(1)

    from scheduler.scheduler import main as start_scheduler
    start_scheduler()


if __name__ == "__main__":
    main()
