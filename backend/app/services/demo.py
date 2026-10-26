from datetime import datetime

from sqlalchemy import delete, func, select

from app.content.site_reader import Page, SiteReadResult
from app.db.models import Asset, Business, PlatformAccount, User
from app.demo_flags import AdvanceProgress, demo_now
from app.platforms.fake import AdTruth, FakeAdapter, VirtualClock
from app.services.changes import ChangeConflict
from app.worker.jobs import Worker, WorkerRepository

DEMO_UID_PREFIX = "demo-"

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

    def rows(self, model, business_id: int | None = None, **filters) -> list:
        return super().rows(model, self.business_id if business_id is None else business_id, **filters)


class DemoUsers:
    """Демо-пользователи живут сутки; вместе с пользователем удаляются его бизнесы и файлы."""

    def __init__(self, db) -> None:
        self.db = db

    def expire(self, before: datetime) -> tuple[list[int], list[str]]:
        users = select(User.id).where(User.yandex_uid.startswith(DEMO_UID_PREFIX), User.created_at < before)
        businesses = list(self.db.scalars(select(Business.id).where(Business.user_id.in_(users))))
        paths = list(self.db.scalars(select(Asset.file_path).where(Asset.business_id.in_(businesses))))
        self.db.execute(delete(User).where(User.id.in_(users)))
        self.db.commit()
        return businesses, paths

    def count(self) -> int:
        return self.db.scalar(select(func.count(User.id)).where(User.yandex_uid.startswith(DEMO_UID_PREFIX)))


class DemoService:
    def __init__(self, worker: Worker) -> None:
        self.worker = worker

    def check(self, business: Business, days: int) -> list[FakeAdapter]:
        if type(days) is not int or not 1 <= days <= 35:
            raise ValueError("Можно перемотать от 1 до 35 дней за один раз.")
        if business.settings.get("_demo", {}).get("days", 0) + days > 120:
            raise ChangeConflict("Можно перемотать не больше 120 дней на один демо-бизнес.")
        if business.status not in {"running", "paused", "budget_stop"}:
            raise ChangeConflict("Сначала запустите рекламу.")
        if not self.worker.demo_available(business):
            raise ChangeConflict("Демо-площадки потеряны после перезапуска API. Войдите в новое демо.")
        adapters = [self.worker.resolve_adapter(business.id, platform) for platform in ("direct", "vk")]
        if any(not isinstance(adapter, FakeAdapter) for adapter in adapters):
            raise ChangeConflict("Перемотка доступна только для демо-площадок.")
        return adapters

    def remember(self, business: Business, **values) -> None:
        # Под блокировкой бизнеса: перечитываем настройки и меняем только поля _demo.
        self.worker.repository.refresh(business)
        business.settings = business.settings | {"_demo": business.settings.get("_demo", {}) | values}
        self.worker.repository.save(business)

    async def advance(self, business_id: int, progress: AdvanceProgress) -> None:
        repository = self.worker.repository
        with repository.business_lock(business_id):
            progress.locked = True
            business = repository.business(business_id)
            repository.refresh(business)
            adapters = self.check(business, progress.days)
            start = demo_now(business, adapters[0].clock.now)
            clock = VirtualClock(start)
            self.worker.clock = clock
            for adapter in adapters:
                adapter.clock = clock
            # Резервируем лимит до запуска: прерванная перемотка не даёт обойти 120 дней.
            self.remember(
                business, days=business.settings.get("_demo", {}).get("days", 0) + progress.days, now=start.isoformat()
            )
            for day in range(1, progress.days + 1):
                for _ in range(48):
                    clock.advance(hours=0.5)
                    self.remember(business, now=clock.now.isoformat())
                    await self.worker.run_due()
                progress.done = day
