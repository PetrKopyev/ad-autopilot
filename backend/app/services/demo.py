from contextlib import suppress
from datetime import datetime
from uuid import uuid4

from sqlalchemy import func, select

from app.content.site_reader import Page, SiteReadResult
from app.db.models import Business, PlatformAccount, User
from app.demo_flags import AdvanceProgress, demo_now
from app.platforms.fake import FakeAdapter, VirtualClock, demo_truths
from app.services.budget_guard import resume_reconnected
from app.services.changes import ChangeConflict
from app.worker.jobs import Worker, WorkerRepository

DEMO_UID_PREFIX = "demo-"
# Адрес салона-примера: мастер демо предзаполняет им сайт, а пустое поле в демо создаёт бизнес с ним же
# (чтение демо-сайта работает для любого адреса).
DEMO_SITE_URL = "https://salon.example/"

DEMO_DESCRIPTION = {
    "services": [
        {"name": "Маникюр", "price": 200000, "quote": "Маникюр — 2000 ₽"},
        {"name": "Педикюр", "price": 250000, "quote": "Педикюр — 2500 ₽"},
        {"name": "Наращивание ногтей", "price": 300000, "quote": "Наращивание ногтей — 3000 ₽"},
    ],
    "address": {"value": "Москва, улица Примерная, 10", "quote": "Москва, улица Примерная, 10"},
    "advantages": [{"value": "Запись онлайн каждый день", "quote": "Запись онлайн каждый день"}],
}


async def read_demo_site(url: str) -> SiteReadResult:
    text = (
        "Демонстрационный салон. Маникюр — 2000 ₽. Педикюр — 2500 ₽. Наращивание ногтей — 3000 ₽. "
        "Москва, улица Примерная, 10. Запись онлайн каждый день."
    )
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


def configure_adapter(adapter: FakeAdapter, spec, start: int = 0) -> None:
    """Группы Директа из демо-плана, по услуге на группу: сильная, слабая и пограничная; события считает Fake.
    start — сколько групп площадки получили эффективность в прошлых кампаниях запуска."""
    for group, truth in zip(spec.groups, demo_truths(start + len(spec.groups))[start:], strict=True):
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

    def expire(self, before: datetime) -> list[int]:
        """Возвращает id удалённых бизнесов: их каталоги с файлами удаляются целиком.

        Пользователь удаляется под блокировками своих бизнесов (см. AuthRepository.delete_user); занятый перемоткой
        или сборкой ролика остаётся до следующего входа в демо."""
        from app.auth.sessions import AuthRepository

        users = self.db.scalars(
            select(User.id).where(User.yandex_uid.startswith(DEMO_UID_PREFIX), User.created_at < before)
        ).all()
        removed = []
        for user_id in users:
            with suppress(ChangeConflict):
                removed += AuthRepository(self.db).delete_user(user_id)
        return removed

    def missing(self, business_ids: set[int]) -> set[int]:
        """Какие из этих бизнесов уже удалены."""
        return business_ids - set(self.db.scalars(select(Business.id).where(Business.id.in_(business_ids))))

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
            raise ChangeConflict("Демо-данные пропали после перезапуска сервера. Войдите в демо заново.")
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
            before = business.settings.get("_demo", {}).get("days", 0)
            self.remember(business, now=start.isoformat())
            with self.worker.rewinding():
                for day in range(1, progress.days + 1):
                    for _ in range(48):
                        clock.advance(hours=0.5)
                        self.remember(business, now=clock.now.isoformat())
                        await self.worker.run_business_due(business_id)
                    # Лимит 120 дней тратят только пройденные дни: оборванная перемотка (и упавший сервер) не
                    # списывает остаток, и «Повторить» досчитывает его, не тратя лимит дважды. Запас на весь запрос
                    # проверен в check под этой же блокировкой, а две перемотки одного бизнеса не идут.
                    self.remember(business, days=before + day)
                    progress.done = day

    async def reconnect(self, business_id: int) -> None:
        """Демо: кабинет, который владелец отключил, подключается снова без входа в Яндекс, и кампании, остановленные
        при отключении, продолжают работать, если реклама не на паузе."""
        repository = self.worker.repository
        with repository.business_lock(business_id):
            business = repository.business(business_id)
            account = repository.find(PlatformAccount, business_id=business_id, platform="direct")
            if account is None or account.status != "revoked":
                raise ChangeConflict("Кабинет Директа уже подключён.")
            account.status = "ok"
            account.access_token_enc = "demo"
            account.scopes = ["direct:api", "metrika:read"]
            repository.save(account)
            self.worker.step_key = f"owner:{uuid4()}"
            with self.worker.business_clock(business):
                await resume_reconnected(self.worker, business, "direct")
