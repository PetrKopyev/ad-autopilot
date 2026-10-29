import logging

import pytest

from app.platforms.base import AuthExpired, PlatformError, RateLimited
from app.platforms.fake import FakeAdapter
from app.platforms.metrica import FakeMetrica, MetricaAccessError, MetricaAuthError, MetricaCounter
from app.services.connections import DIRECT_FAILED, NO_COUNTERS, RECONNECT, check_connections


def keep(*_) -> None:
    raise AssertionError("кабинет с рабочим токеном не помечаем")


class Failing:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def whoami(self) -> dict[str, str]:
        raise self.error

    async def list_counters(self) -> list:
        raise self.error


def direct_error(code: str) -> PlatformError:
    error = PlatformError("Ошибка Яндекс Директа.", status_code=200)
    error.direct_code = code
    return error


async def test_direct_errors_are_explained_in_plain_words() -> None:
    messages = {}
    for code in ("58", "513", "54", "3000", "9999"):
        items = await check_connections(
            1, {"direct": "ok"}, lambda *_: Failing(direct_error(code)), lambda _: FakeMetrica(), keep
        )
        messages[code] = items[0]["message"]
    assert messages["58"].startswith("Яндекс ещё не одобрил доступ к API Директа")
    assert "не подключён Директ" in messages["513"]
    assert "нет прав" in messages["54"]
    assert messages["3000"].startswith("Яндекс закрыл этому аккаунту доступ к API Директа")


async def test_unknown_direct_error_is_logged_and_not_shown(caplog: pytest.LogCaptureFixture) -> None:
    error = PlatformError("Invalid request: внутренняя подробность", status_code=200)
    error.direct_code = 9999
    with caplog.at_level(logging.WARNING, logger="app.services.connections"):
        items = await check_connections(1, {"direct": "ok"}, lambda *_: Failing(error), lambda _: FakeMetrica(), keep)
    assert items[0]["message"] == DIRECT_FAILED
    assert "9999" in caplog.text and "внутренняя подробность" in caplog.text


async def test_expired_token_marks_account_for_reconnect() -> None:
    expired = []

    def resolve(_, platform: str):
        return Failing(AuthExpired()) if platform == "vk" else FakeAdapter()

    metrica = Failing(MetricaAuthError())
    items = await check_connections(
        1, {"direct": "ok", "vk": "ok"}, resolve, lambda _: metrica, lambda *args: expired.append(args)
    )
    assert expired == [(1, "vk"), (1, "direct")]
    assert [(item["platform"], item["message"]) for item in items if not item["ok"]] == [
        ("vk", RECONNECT),
        ("metrica", RECONNECT),
    ]


async def test_expired_direct_skips_metrica() -> None:
    expired = []

    def metrica(_):
        raise AssertionError("Метрику по истёкшему токену Директа не спрашиваем")

    items = await check_connections(
        1, {"direct": "ok"}, lambda *_: Failing(AuthExpired()), metrica, lambda *args: expired.append(args)
    )
    assert items == [{"platform": "direct", "ok": False, "message": RECONNECT}] and expired == [(1, "direct")]


@pytest.mark.parametrize(
    "error,text",
    [
        (RateLimited(), "Проверьте ещё раз через минуту"),
        (MetricaAccessError(), "Нет доступа к счётчикам сайта. Подключите"),
    ],
)
async def test_metrica_failures_are_explained(error: Exception, text: str) -> None:
    items = await check_connections(1, {"direct": "ok"}, lambda *_: FakeAdapter(), lambda _: Failing(error), keep)
    assert items[-1]["platform"] == "metrica" and not items[-1]["ok"] and text in items[-1]["message"]


async def test_account_without_counters_is_not_ready() -> None:
    items = await check_connections(1, {"direct": "ok"}, lambda *_: FakeAdapter(), lambda _: FakeMetrica(), keep)
    assert items[-1] == {
        "platform": "metrica",
        "ok": False,
        "message": NO_COUNTERS,
    }


async def test_missing_direct_is_reported_and_platforms_are_not_called() -> None:
    def resolve(*_):
        raise AssertionError("площадку без кабинета не спрашиваем")

    items = await check_connections(
        1, {"vk": "revoked"}, resolve, lambda _: FakeMetrica([MetricaCounter(1, "С", "a.ru")]), keep
    )
    assert items == [{"platform": "direct", "ok": False, "message": "Не подключён. Нажмите «Подключить Директ»."}]
