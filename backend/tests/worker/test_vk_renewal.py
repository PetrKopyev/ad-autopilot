import logging
from datetime import UTC, datetime, timedelta

import pytest

from app.auth.vk import RENEW_MARGIN
from app.db.models import Business, PlatformAccount
from app.platforms.base import AuthExpired
from app.worker.jobs import MemoryWorkerRepository, Worker

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


class FixedClock:
    @property
    def now(self) -> datetime:
        return NOW


def make_worker(repository: MemoryWorkerRepository) -> Worker:
    return Worker(repository, lambda business_id, platform: None, clock=FixedClock(), dry_run=False)


def vk_account(business_id: int, expires_at: datetime | None) -> PlatformAccount:
    return PlatformAccount(
        business_id=business_id,
        platform="vk",
        auth_mode="oauth",
        external_login="777",
        access_token_enc="token",
        refresh_token_enc="refresh",
        expires_at=expires_at,
        status="ok",
    )


def patch_renewal(monkeypatch: pytest.MonkeyPatch, calls: list, failures: set[int] = frozenset()) -> None:
    from app.auth import vk as vk_auth

    async def fake_renew(db, business_id, **kwargs):
        calls.append((business_id, kwargs.get("force")))
        if business_id in failures:
            raise AuthExpired()

    monkeypatch.setattr(vk_auth, "renew_account", fake_renew)


async def test_renew_vk_tokens_renews_expiring_account_once(monkeypatch) -> None:
    repository = MemoryWorkerRepository()
    account = vk_account(1, NOW + timedelta(minutes=30))
    repository.save(account)
    calls = []
    patch_renewal(monkeypatch, calls)

    await make_worker(repository).renew_vk_tokens()

    assert calls == [(1, True)]
    # Продлённый токен живёт сутки: следующий тик не тратит новый грант, пока запас не подошёл к концу.
    account.expires_at = NOW + timedelta(hours=24)
    await make_worker(repository).renew_vk_tokens()
    assert calls == [(1, True)]


async def test_renew_vk_tokens_skips_fresh_direct_and_timeless_accounts(monkeypatch) -> None:
    repository = MemoryWorkerRepository()
    repository.save(
        Business(id=1, user_id=1),
        vk_account(1, NOW + RENEW_MARGIN + timedelta(minutes=1)),
        vk_account(2, None),
        PlatformAccount(
            business_id=1,
            platform="direct",
            auth_mode="oauth",
            access_token_enc="direct-token",
            # Срок неизвестен: шаг продления Директа такие кабинеты не трогает, как и VK-шаг.
            expires_at=None,
            status="ok",
        ),
    )
    calls = []
    patch_renewal(monkeypatch, calls)

    # Шаг вызывается из обычного полуторачасового тика воркера.
    await make_worker(repository).run_due()

    assert calls == []


async def test_renew_vk_tokens_continues_after_failing_account(monkeypatch, caplog) -> None:
    repository = MemoryWorkerRepository()
    repository.save(
        vk_account(1, NOW - timedelta(minutes=5)),
        vk_account(2, NOW + timedelta(minutes=10)),
    )
    calls = []
    patch_renewal(monkeypatch, calls, failures={1})

    with caplog.at_level(logging.WARNING):
        await make_worker(repository).renew_vk_tokens()

    # Отказ одного кабинета не останавливает шаг: второй продлён, ошибка ушла в журнал с площадкой и кодом.
    assert calls == [(1, True), (2, True)]
    assert "Token renewal failed" in caplog.text
    assert "platform=vk" in caplog.text and "business=1" in caplog.text and "code=401" in caplog.text
