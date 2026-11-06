import logging
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import Business, PlatformAccount, WorkerRun
from app.platforms.base import AuthExpired, AuthSession
from app.worker.jobs import MemoryWorkerRepository, Worker

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


class FixedClock:
    @property
    def now(self) -> datetime:
        return NOW


def make_worker(repository: MemoryWorkerRepository) -> Worker:
    return Worker(repository, lambda business_id, platform: None, clock=FixedClock(), dry_run=False)


def direct_account(business_id: int, expires_at: datetime | None, token: str = "direct-token") -> PlatformAccount:
    return PlatformAccount(
        business_id=business_id,
        platform="direct",
        auth_mode="oauth",
        external_login="direct-login",
        access_token_enc=token,
        refresh_token_enc=f"{token}-refresh",
        expires_at=expires_at,
        status="ok",
    )


def patch_refresh(monkeypatch: pytest.MonkeyPatch, calls: list, failures: set[str] = frozenset()) -> None:
    from app.platforms.direct import DirectAdapter

    async def fake_refresh(self, session: AuthSession) -> AuthSession:
        calls.append(session.access_token)
        if session.access_token in failures:
            raise AuthExpired()
        return AuthSession(session.account_id, "new-access", "new-refresh", NOW + timedelta(days=180))

    monkeypatch.setattr(DirectAdapter, "refresh", fake_refresh)


async def test_renew_direct_tokens_renews_expiring_account(monkeypatch) -> None:
    repository = MemoryWorkerRepository()
    account = direct_account(1, NOW + timedelta(days=10))
    repository.save(account)
    calls = []
    patch_refresh(monkeypatch, calls)

    await make_worker(repository).renew_direct_tokens()

    assert calls == ["direct-token"]
    assert account.access_token_enc == "new-access"
    assert account.refresh_token_enc == "new-refresh"
    assert account.expires_at == NOW + timedelta(days=180)
    # Продлённый токен живёт полгода: следующий тик не тратит новый грант, пока запас не подошёл к концу.
    await make_worker(repository).renew_direct_tokens()
    assert calls == ["direct-token"]


async def test_renew_direct_tokens_notifies_once_and_continues_after_failure(monkeypatch, caplog) -> None:
    repository = MemoryWorkerRepository()
    repository.save(
        Business(id=1, user_id=1),
        Business(id=2, user_id=1),
        direct_account(1, NOW + timedelta(days=10), token="failing"),
        direct_account(2, NOW + timedelta(days=5), token="ok-token"),
    )
    calls = []
    patch_refresh(monkeypatch, calls, failures={"failing"})

    worker = make_worker(repository)
    with caplog.at_level(logging.WARNING):
        # Повторный вызов: упавший кабинет ещё на истечении, продлённый — уже нет.
        await worker.renew_direct_tokens()
        await worker.renew_direct_tokens()

    assert calls == ["failing", "ok-token", "failing"]
    # Отказ одного кабинета не останавливает шаг: второй продлён, токены упавшего не затёрты.
    failing = repository.find(PlatformAccount, business_id=1)
    assert failing.access_token_enc == "failing"
    assert failing.refresh_token_enc == "failing-refresh"
    assert failing.expires_at == NOW + timedelta(days=10)
    assert repository.find(PlatformAccount, business_id=2).access_token_enc == "new-access"
    # Уведомление одно на дату окончания токена: повторы тика не плодят дубли.
    notifications = repository.rows(WorkerRun, kind="notification")
    assert len(notifications) == 1
    entry = notifications[0].log[0]
    assert entry["reason_code"] == "DIRECT_REAUTH"
    assert entry["key"] == f"direct_reauth:1:{(NOW + timedelta(days=10)).date().isoformat()}"
    assert "Token renewal failed" in caplog.text
    assert "platform=direct" in caplog.text and "business=1" in caplog.text and "code=401" in caplog.text


async def test_renew_direct_tokens_skips_notice_when_business_missing(monkeypatch) -> None:
    repository = MemoryWorkerRepository()
    repository.save(direct_account(99, NOW + timedelta(days=10), token="failing"))
    calls = []
    patch_refresh(monkeypatch, calls, failures={"failing"})

    await make_worker(repository).renew_direct_tokens()

    assert calls == ["failing"]
    assert repository.rows(WorkerRun) == []


async def test_renew_direct_tokens_skips_fresh_timeless_and_vk_accounts(monkeypatch) -> None:
    repository = MemoryWorkerRepository()
    repository.save(
        Business(id=1, user_id=1),
        direct_account(1, NOW + timedelta(days=60)),
        direct_account(1, None),
        PlatformAccount(
            business_id=1,
            platform="vk",
            auth_mode="oauth",
            external_login="777",
            access_token_enc="vk-token",
            refresh_token_enc="vk-refresh",
            expires_at=NOW + timedelta(minutes=5),
            status="ok",
        ),
    )
    calls = []
    patch_refresh(monkeypatch, calls)

    await make_worker(repository).renew_direct_tokens()

    assert calls == []


async def test_run_due_renews_direct_tokens(monkeypatch) -> None:
    repository = MemoryWorkerRepository()
    repository.save(Business(id=1, user_id=1), direct_account(1, NOW + timedelta(days=10)))
    calls = []
    patch_refresh(monkeypatch, calls)

    # Шаг вызывается из обычного полуторачасового тика воркера.
    await make_worker(repository).run_due()

    assert calls == ["direct-token"]


async def test_renew_direct_tokens_network_failure_far_from_expiry_only_logs(monkeypatch, caplog) -> None:
    """Разовый сетевой сбой при запасе больше 7 дней — только warning, без уведомления."""
    from app.platforms.base import PlatformError
    from app.platforms.direct import DirectAdapter

    repository = MemoryWorkerRepository()
    repository.save(Business(id=1, user_id=1), direct_account(1, NOW + timedelta(days=20)))

    async def broken(self, session):
        raise PlatformError("Не удалось связаться с OAuth Яндекса.")

    monkeypatch.setattr(DirectAdapter, "refresh", broken)
    with caplog.at_level(logging.WARNING):
        await make_worker(repository).renew_direct_tokens()
    assert repository.rows(WorkerRun) == [] and "Token renewal failed" in caplog.text

    # Срок близко (меньше 7 дней): уведомление уходит и при сетевом сбое.
    repository.find(PlatformAccount, business_id=1).expires_at = NOW + timedelta(days=3)
    await make_worker(repository).renew_direct_tokens()
    assert len(repository.rows(WorkerRun, kind="notification")) == 1


async def test_renew_direct_tokens_does_not_save_token_of_account_revoked_meanwhile(monkeypatch) -> None:
    """Кабинет отключили, пока шло продление: новый токен не сохраняется, а отзывается у Яндекса."""
    from app.platforms.direct import DirectAdapter
    from app.worker import jobs

    repository = MemoryWorkerRepository()
    account = direct_account(1, NOW + timedelta(days=10))
    repository.save(Business(id=1, user_id=1), account)

    async def refresh(self, session):
        account.status = "revoked"
        return AuthSession(session.account_id, "new-access", "new-refresh", NOW + timedelta(days=180))

    revoked = []

    async def revoke(http, revocations):
        revoked.extend(revocations)

    monkeypatch.setattr(DirectAdapter, "refresh", refresh)
    monkeypatch.setattr(jobs, "revoke_tokens", revoke)
    await make_worker(repository).renew_direct_tokens()

    assert account.access_token_enc == "direct-token" and account.refresh_token_enc == "direct-token-refresh"
    assert [(item.platform, item.access_token) for item in revoked] == [("direct", "new-access")]


async def test_renew_direct_tokens_rolls_back_session_after_failed_save(monkeypatch) -> None:
    """После сбоя записи сессия откатывается, остальные кабинеты продлеваются."""
    repository = MemoryWorkerRepository()
    repository.save(
        Business(id=1, user_id=1),
        Business(id=2, user_id=1),
        direct_account(1, NOW + timedelta(days=10), token="first"),
        direct_account(2, NOW + timedelta(days=10), token="second"),
    )
    patch_refresh(monkeypatch, [])
    rollbacks = []
    original_save = repository.save

    def save(*items):
        if any(getattr(item, "access_token_enc", None) == "new-access" for item in items) and not rollbacks:
            raise RuntimeError("db down")
        original_save(*items)

    monkeypatch.setattr(repository, "save", save)
    monkeypatch.setattr(repository, "rollback", lambda: rollbacks.append(True))
    await make_worker(repository).renew_direct_tokens()

    assert rollbacks
    assert repository.find(PlatformAccount, business_id=2).access_token_enc == "new-access"


async def test_renew_direct_tokens_deleted_account_does_not_stop_next_one(monkeypatch) -> None:
    """Кабинет, удалённый между выборкой и итерацией, не обрывает шаг: уведомления нет, следующий продлевается."""
    from sqlalchemy.orm.exc import ObjectDeletedError

    class Gone(PlatformAccount):
        @property
        def business_id(self):
            raise ObjectDeletedError(None)

    repository = MemoryWorkerRepository()
    healthy = direct_account(2, NOW + timedelta(days=10), token="second")
    repository.save(Business(id=2, user_id=1), healthy)
    monkeypatch.setattr(repository, "expiring_direct_accounts", lambda now: [Gone(), healthy])
    patch_refresh(monkeypatch, [])

    await make_worker(repository).renew_direct_tokens()

    assert healthy.access_token_enc == "new-access"
    assert not repository.rows(WorkerRun, kind="notification")


async def test_renew_direct_tokens_revoke_failure_after_skip_sends_no_notice(monkeypatch, caplog) -> None:
    """Кабинет отключён владельцем, отзыв нового токена упал по сети: только warning, без DIRECT_REAUTH."""
    from app.platforms.direct import DirectAdapter
    from app.worker import jobs

    repository = MemoryWorkerRepository()
    account = direct_account(1, NOW + timedelta(days=1))
    repository.save(Business(id=1, user_id=1), account)

    async def refresh(self, session):
        account.status = "revoked"
        return AuthSession(session.account_id, "new-access", "new-refresh", NOW + timedelta(days=180))

    async def revoke(http, revocations):
        raise RuntimeError("network down")

    monkeypatch.setattr(DirectAdapter, "refresh", refresh)
    monkeypatch.setattr(jobs, "revoke_tokens", revoke)
    with caplog.at_level(logging.WARNING):
        await make_worker(repository).renew_direct_tokens()

    assert "Token revoke failed" in caplog.text
    assert not repository.rows(WorkerRun, kind="notification")
