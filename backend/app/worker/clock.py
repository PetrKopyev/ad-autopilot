from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")


class Clock(Protocol):
    @property
    def now(self) -> datetime: ...


class SystemClock:
    @property
    def now(self) -> datetime:
        return datetime.now(UTC)


def today(clock: Clock):
    return clock.now.astimezone(MOSCOW).date()
