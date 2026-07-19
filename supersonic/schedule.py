"""Portfolio queue + scheduled overnight builds."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from supersonic.config import load_secrets
from supersonic.loop.orchestrator import run_factory
from supersonic.store import create_run, dequeue_next_queued, get_running_project_id, list_queue

logger = logging.getLogger(__name__)

_scheduler_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_next_queued() -> Optional[str]:
    """Start the next queued project if nothing is running. Returns run_id."""
    if get_running_project_id():
        return None
    item = dequeue_next_queued()
    if not item:
        return None
    secrets = load_secrets()
    run = create_run(item["project_id"])
    try:
        run_factory(run, secrets, item.get("seed") or "")
        return run.id
    except Exception:
        logger.exception("scheduled run failed project=%s", item["project_id"])
        return run.id


def tick() -> None:
    secrets = load_secrets()
    if not secrets.schedule_enabled or get_running_project_id():
        return
    last_path = secrets.schedule_state_file()
    try:
        state = json.loads(last_path.read_text()) if last_path.exists() else {}
        last = state.get("last_run_at")
        hours = max(secrets.schedule_interval_hours, 1)
        if last:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            if elapsed < hours:
                return
        rid = run_next_queued()
        if rid:
            last_path.parent.mkdir(parents=True, exist_ok=True)
            last_path.write_text(json.dumps({"last_run_at": _now(), "last_run_id": rid}))
    except Exception:
        logger.exception("scheduler tick failed")


def _loop() -> None:
    while not _stop.is_set():
        try:
            tick()
        except Exception:
            logger.exception("scheduler loop error")
        _stop.wait(60)


def start_scheduler() -> None:
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _stop.clear()
    _scheduler_thread = threading.Thread(target=_loop, name="sonic-scheduler", daemon=True)
    _scheduler_thread.start()
    logger.info("Supersonic scheduler started")


def stop_scheduler() -> None:
    _stop.set()


def queue_status() -> dict:
    return {
        "enabled": load_secrets().schedule_enabled,
        "interval_hours": load_secrets().schedule_interval_hours,
        "running_project": get_running_project_id(),
        "queue": list_queue(),
    }
