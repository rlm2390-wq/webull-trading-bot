"""
trading_engine/core/scheduler_log.py
Context manager that wraps each scheduled task run with a DB audit record.

Usage:
    with task_run("monday_deploy"):
        monday_engine.run(state)
"""

from __future__ import annotations

import datetime
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional

import sqlalchemy as sa

from db.database  import get_db
from utils.logger import get_logger

logger = get_logger(__name__)


@contextmanager
def task_run(task_name: str, details: Optional[Dict[str, Any]] = None):
    """
    Wraps a scheduled task. Logs success/failure to scheduler_log.
    """
    today  = datetime.date.today()
    dow    = today.strftime("%A").lower()
    start  = time.monotonic()
    status = "success"
    error  = None

    logger.info(">>> TASK START: %s", task_name)
    try:
        yield
    except Exception as exc:
        status = "error"
        error  = str(exc)
        logger.exception("Task %s failed: %s", task_name, exc)
        raise
    finally:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info("<<< TASK END: %s | status=%s | %dms", task_name, status, duration_ms)
        try:
            import json
            with get_db() as db:
                db.execute(sa.text("""
                    INSERT INTO scheduler_log
                        (task_name, day_of_week, status, duration_ms, error_msg, details)
                    VALUES
                        (:task, :dow, :status, :dur, :err, :details)
                """), {
                    "task":    task_name,
                    "dow":     dow,
                    "status":  status,
                    "dur":     duration_ms,
                    "err":     error,
                    "details": json.dumps(details) if details else None,
                })
        except Exception as log_exc:
            logger.warning("Could not write scheduler_log: %s", log_exc)
