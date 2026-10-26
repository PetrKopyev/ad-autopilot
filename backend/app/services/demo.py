from datetime import datetime, timedelta

from app.content.site_reader import Page, SiteReadResult
from app.db.models import Business, PlatformAccount
from app.platforms.fake import AdTruth, FakeAdapter, VirtualClock
from app.services.changes import ChangeConflict
from app.worker.clock import MOSCOW
from app.worker.jobs import Worker, WorkerRepository

DEMO_DESCRIPTION = {
    "services": [{"name": "Маникюр", "price": 200000, "quote": "Маникюр — 2000 ₽"}],
    "address": {"value": "Москва, улица Примерная, 10", "quote": "Москва, улица Примерная, 10"},
    "advantages": [{"value": "Запись онлайн каждый день", "quote": "Запись онлайн каждый день"}],
}


async def read_demo_site(url: str) -> SiteReadResult:
    text = "Демонстрационный салон. Маникюр — 2000 ₽. Москва, улица Примерная, 10. Запись онлайн каждый день."
    return SiteReadResult([Page(url, text, counter_ids=[12345])])


def prepare_business(repository, business: Business) -> None:
    if not any(account.platform == "direct" for account in repository.accounts(business.id)):
        repository.add(
            PlatformAccount(
                business_id=business.id,
                platform="direct",
                auth_mode="oauth",
                external_login="Демо-салон",
                access_token_enc="demo",
                status="ok",
                scopes=["direct:api", "metrika:read"],
            )
        )


def demo_now(business: Business, fallback: datetime) -> datetime:
    from app.config import settings

    value = business.settings.get("_demo", {}).get("now")
    return datetime.fromisoformat(value) if settings.platform_mode == "fake" and value else fallback


def bind_worker_clock(worker: Worker, business: Business) -> None:
    # Планировщик после перемотки продолжает работать в дате этого бизнеса.
    if not hasattr(worker, "_demo_base_clock"):
        worker._demo_base_clock = worker.clock
    base = worker._demo_base_clock
    fallback = base.now
    now = demo_now(business, fallback)
    worker.clock = VirtualClock(now) if now != fallback else base


def configure_adapter(adapter: FakeAdapter, spec) -> None:
    """Три группы из обычного плана: сильная, слабая и пограничная; события считает Fake."""
    for group, truth in zip(
        spec.groups,
        (AdTruth(0.08, 25, 0.2, 600), AdTruth(0.007, 62, 0.01, 600), AdTruth(0.035, 45, 0.06, 600)),
        strict=False,
    ):
        for ad in group.ads:
            adapter._truths[ad.id] = truth


class DemoRepository(WorkerRepository):
    def __init__(self, db, business_id: int) -> None:
        super().__init__(db)
        self.business_id = business_id

    def rows(self, model, business_id: int | None = None) -> list:
        return super().rows(model, self.business_id if business_id is None else business_id)


class DemoService:
    def __init__(self, worker: Worker) -> None:
        self.worker = worker

    async def advance(self, business_id: int, days: int) -> dict:
        if type(days) is not int or not 1 <= days <= 35:
            raise ValueError("Можно перемотать от 1 до 35 дней за один раз.")
        repository = self.worker.repository
        with repository.business_lock(business_id):
            business = repository.business(business_id)
            meta = business.settings.get("_demo", {})
            elapsed = meta.get("days", 0)
            if elapsed + days > 120:
                raise ChangeConflict("Можно перемотать не больше 120 дней на один демо-бизнес.")
            if business.status not in {"running", "paused", "budget_stop"}:
                raise ChangeConflict("Сначала запустите рекламу.")
            if not self.worker.demo_available(business):
                raise ChangeConflict("Демо-площадки потеряны после перезапуска API. Войдите в новое демо.")
            adapters = [self.worker.resolve_adapter(business_id, platform) for platform in ("direct", "vk")]
            if any(not isinstance(adapter, FakeAdapter) for adapter in adapters):
                raise ChangeConflict("Перемотка доступна только для демо-площадок.")
            start = demo_now(business, adapters[0].clock.now)
            clock = VirtualClock(start)
            self.worker.clock = clock
            for adapter in adapters:
                adapter.clock = clock
            # Резервируем лимит до запуска: прерванный запрос не даёт обойти 120 дней.
            business.settings = business.settings | {"_demo": {"days": elapsed + days, "now": start.isoformat()}}
            repository.save(business)
            end = start + timedelta(days=days)
            while clock.now < end:
                clock.advance(hours=0.5)
                business.settings = business.settings | {
                    "_demo": {"days": elapsed + days, "now": clock.now.isoformat()}
                }
                repository.save(business)
                await self.worker.run_due()
                # month_rollover привязывает часы обычного планировщика; здесь нужен тот же движущийся объект.
                self.worker.clock = clock
            return {
                "days": days,
                "total_days": elapsed + days,
                "now": clock.now,
                "today": clock.now.astimezone(MOSCOW).date(),
            }
