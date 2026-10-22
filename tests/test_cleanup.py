from datetime import UTC, datetime, timedelta, timezone

import pytest

from texdrop.cleanup import CleanupScheduler, seconds_until_next_run


@pytest.mark.parametrize(
    ("now", "expected_hours"),
    [
        (datetime(2026, 10, 22, 0, 0, tzinfo=UTC), 2),
        (datetime(2026, 10, 22, 1, 59, 30, tzinfo=UTC), 1 / 120),
        (datetime(2026, 10, 22, 2, 0, tzinfo=UTC), 24),
        (datetime(2026, 10, 22, 23, 0, tzinfo=UTC), 3),
        # 05:00 in Athens (UTC+3) is 02:00 UTC: the schedule follows UTC, not local time.
        (datetime(2026, 10, 22, 4, 0, tzinfo=timezone(timedelta(hours=3))), 1),
    ],
)
def test_next_run_is_at_two_utc(now, expected_hours):
    assert seconds_until_next_run(now) == pytest.approx(expected_hours * 3600)


class FakeService:
    def __init__(self):
        self.calls = []

    def delete_expired(self, days):
        self.calls.append(days)
        return 3


def test_run_once_deletes_with_the_configured_age():
    service = FakeService()
    assert CleanupScheduler(service, 15).run_once() == 3
    assert service.calls == [15]


def test_disabled_when_zero_days():
    scheduler = CleanupScheduler(FakeService(), 0)
    scheduler.start()
    assert not scheduler._thread.is_alive()
    scheduler.stop()


def test_thread_starts_and_stops():
    scheduler = CleanupScheduler(FakeService(), 15)
    scheduler.start()
    assert scheduler._thread.is_alive()
    scheduler.stop()
    assert not scheduler._thread.is_alive()
