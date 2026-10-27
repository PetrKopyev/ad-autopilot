from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Literal

from app.db.models import Business

ADVANCING = "Идёт перемотка времени. Дождитесь окончания."
# Перемотка держит соединение пула блокировок минутами: больше этого числа сразу пул кончится для остальных.
MAX_ADVANCES = 10


def demo_enabled() -> bool:
    from app.config import settings

    return settings.platform_mode == "fake" and settings.demo_login


def demo_now(business: Business, fallback: datetime) -> datetime:
    value = business.settings.get("_demo", {}).get("now")
    return datetime.fromisoformat(value) if value and demo_enabled() else fallback


@dataclass
class AdvanceProgress:
    days: int
    done: int = 0
    state: Literal["running", "done", "error"] = "running"
    error: str = ""
    locked: bool = False


class AdvanceJobs:
    """Перемотки в памяти процесса API: прогресс для опроса и признак занятого бизнеса."""

    def __init__(self) -> None:
        self._guard = Lock()
        self._items: dict[int, AdvanceProgress] = {}

    def start(self, business_id: int, days: int) -> AdvanceProgress | None:
        """None — у бизнеса перемотка уже идёт; ServerBusy — идёт MAX_ADVANCES перемоток других бизнесов."""
        from app.services.changes import ServerBusy

        with self._guard:
            if self.running(business_id):
                return None
            if sum(progress.state == "running" for progress in self._items.values()) >= MAX_ADVANCES:
                raise ServerBusy
            self._items[business_id] = progress = AdvanceProgress(days)
            return progress

    def get(self, business_id: int) -> AdvanceProgress | None:
        return self._items.get(business_id)

    def running(self, business_id: int) -> bool:
        progress = self._items.get(business_id)
        return progress is not None and progress.state == "running"

    def holds_lock(self, business_id: int) -> bool:
        progress = self._items.get(business_id)
        return progress is not None and progress.state == "running" and progress.locked

    def forget(self, business_id: int) -> None:
        self._items.pop(business_id, None)

    def businesses(self) -> set[int]:
        return set(self._items)


advance_jobs = AdvanceJobs()


def demo_frozen(business: Business) -> bool:
    """После перемотки время демо-бизнеса движется только перемоткой: фоновый тик его не трогает."""
    return advance_jobs.running(business.id) or ("now" in business.settings.get("_demo", {}) and demo_enabled())
