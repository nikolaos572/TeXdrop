"""Automatic deletion of old jobs, every day at 02:00 UTC (AUTO_DELETE_DAYS)."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, time, timedelta

from .jobs import JobService

log = logging.getLogger(__name__)

RUN_AT = time(2, 0)  # UTC


def seconds_until_next_run(now: datetime) -> float:
    """Seconds from `now` (timezone-aware) until the next 02:00 UTC."""
    now = now.astimezone(UTC)
    next_run = datetime.combine(now.date(), RUN_AT, tzinfo=UTC)
    if next_run <= now:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()


class CleanupScheduler:
    def __init__(self, service: JobService, max_age_days: int) -> None:
        self._service = service
        self._max_age_days = max_age_days
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="cleanup", daemon=True)

    def start(self) -> None:
        if self._max_age_days > 0:
            log.info("jobs finished more than %d day(s) ago are deleted daily at 02:00 UTC", self._max_age_days)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=10)

    def run_once(self) -> int:
        deleted = self._service.delete_expired(self._max_age_days)
        log.info("auto-delete: removed %d job(s) older than %d day(s)", deleted, self._max_age_days)
        return deleted

    def _loop(self) -> None:
        while not self._stop.wait(seconds_until_next_run(datetime.now(UTC))):
            try:
                self.run_once()
            except Exception:
                log.exception("auto-delete failed")
            self._stop.wait(60)  # a timer firing slightly early must not cause a second run
