"""
trading_engine/scheduler/scheduler.py
APScheduler configuration implementing the full weekly rhythm.
"""

from __future__ import annotations

import datetime
import signal
import sys

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from core.decision_loop import DecisionLoop
from core.scheduler_log import task_run
from db.database        import health_check
from utils.logger       import get_logger, setup_logging

logger = get_logger(__name__)

ET = pytz.timezone("America/New_York")

# US market holidays (update annually)
MARKET_HOLIDAYS_2025 = {
    datetime.date(2025, 1,  1),
    datetime.date(2025, 1, 20),
    datetime.date(2025, 2, 17),
    datetime.date(2025, 4, 18),
    datetime.date(2025, 5, 26),
    datetime.date(2025, 6, 19),
    datetime.date(2025, 7,  4),
    datetime.date(2025, 9,  1),
    datetime.date(2025, 11, 27),
    datetime.date(2025, 12, 25),
}

MARKET_HOLIDAYS_2026 = {
    datetime.date(2026, 1,  1),
    datetime.date(2026, 1, 19),
    datetime.date(2026, 2, 16),
    datetime.date(2026, 4,  3),
    datetime.date(2026, 5, 25),
    datetime.date(2026, 6, 19),
    datetime.date(2026, 7,  3),
    datetime.date(2026, 9,  7),
    datetime.date(2026, 11, 26),
    datetime.date(2026, 12, 25),
}

MARKET_HOLIDAYS = MARKET_HOLIDAYS_2025 | MARKET_HOLIDAYS_2026


def is_market_day() -> bool:
    today = datetime.date.today()
    if today.weekday() >= 5:
        return False
    if today in MARKET_HOLIDAYS:
        return False
    return True


def is_early_trading() -> bool:
    now = datetime.datetime.now(ET).time()
    return datetime.time(4, 0) <= now < datetime.time(9, 30)


def get_day_mode() -> str:
    dow = datetime.date.today().weekday()
    return {0: "monday", 2: "wednesday", 4: "friday"}.get(dow, "daily")


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------

_loop = DecisionLoop()


def job_morning_run():
    logger.info("Heartbeat: scheduler is alive (morning run)")

    if is_early_trading():
        logger.info("Early trading session — full engines allowed (Option A)")

    if not is_market_day():
        logger.info("Market closed today — skipping morning run")
        return

    mode = get_day_mode()
    logger.info("▶ MORNING RUN | mode=%s", mode)

    with task_run(f"morning_{mode}"):
        if mode == "monday":
            _loop.run_monday()
        elif mode == "wednesday":
            _loop.run_wednesday()
        elif mode == "friday":
            _loop.run_friday()
        else:
            _loop.run_daily()


def job_midday_check():
    logger.info("Heartbeat: scheduler is alive (midday check)")

    if is_early_trading():
        logger.info("Early trading session — full engines allowed (Option A)")

    if not is_market_day():
        return

    with task_run("midday_check"):
        _loop.run_daily()


def job_eod_run():
    logger.info("Heartbeat: scheduler is alive (EOD run)")

    if is_early_trading():
        logger.info("Early trading session — full engines allowed (Option A)")

    if not is_market_day():
        return

    with task_run("eod_run"):
        _loop.run_daily()


def job_heartbeat():
    logger.info("Heartbeat: scheduler is alive")


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def on_job_executed(event):
    logger.debug("Job executed: %s", event.job_id)


def on_job_error(event):
    logger.error("Job ERROR: %s | %s", event.job_id, event.exception)


def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=ET)

    # Heartbeat FIRST so it always registers
    scheduler.add_job(
        job_heartbeat,
        trigger="interval",
        minutes=5,
        id="heartbeat",
        name="Scheduler Heartbeat",
    )

    scheduler.add_job(
        job_morning_run,
        trigger="cron",
        day_of_week="mon-fri",
        hour=9,
        minute=35,
        id="morning_run",
        name="Morning Decision Loop",
        misfire_grace_time=300,
    )

    scheduler.add_job(
        job_midday_check,
        trigger="cron",
        day_of_week="mon-fri",
        hour=12,
        minute=0,
        id="midday_check",
        name="Midday Dip Check",
        misfire_grace_time=300,
    )

    scheduler.add_job(
        job_eod_run,
        trigger="cron",
        day_of_week="mon-fri",
        hour=15,
        minute=45,
        id="eod_run",
        name="End-of-Day Snapshot",
        misfire_grace_time=300,
    )

    scheduler.add_listener(on_job_executed, EVENT_JOB_EXECUTED)
    scheduler.add_listener(on_job_error, EVENT_JOB_ERROR)

    return scheduler


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def main():
    setup_logging()
    logger.info("Trading Engine starting up...")

    if not health_check():
        logger.critical("Database unreachable — aborting")
        sys.exit(1)

    logger.info("Database OK")

    scheduler = build_scheduler()

    def shutdown(signum, frame):
        logger.info("Shutdown signal received — stopping scheduler")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("Scheduler started. Jobs:")

    now = datetime.datetime.now(ET)
    for job in scheduler.get_jobs():
        next_run = job.trigger.get_next_fire_time(None, now)
        logger.info("  • %s — next run: %s", job.name, next_run)

    scheduler.start()


if __name__ == "__main__":
    main()
