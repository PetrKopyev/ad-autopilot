import pytest

from app.platforms.base import PlatformError, RateLimited
from app.platforms.fake import FakeAdapter
from app.platforms.metrica import FakeMetrica, MetricaAccessError, MetricaCounter
from app.services.connections import check_connections


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
    for code in ("58", "513", "54", "9999"):
        items = await check_connections(
            1, {"direct": "ok"}, lambda *_: Failing(direct_error(code)), lambda _: FakeMetrica()
        )
        messages[code] = items[0]["message"]
    assert messages["58"].startswith("Яндекс ещё не одобрил доступ к API Директа")
    assert "не подключён Директ" in messages["513"]
    assert "нет прав" in messages["54"]
    assert messages["9999"] == "Ошибка Яндекс Директа."


@pytest.mark.parametrize(
    "error,text",
    [(RateLimited(), "Проверьте ещё раз через минуту"), (MetricaAccessError(), "Нет доступа к Метрике")],
)
async def test_metrica_failures_are_explained(error: Exception, text: str) -> None:
    items = await check_connections(1, {"direct": "ok"}, lambda *_: FakeAdapter(), lambda _: Failing(error))
    assert items[-1]["platform"] == "metrica" and not items[-1]["ok"] and text in items[-1]["message"]


async def test_account_without_counters_is_not_ready() -> None:
    items = await check_connections(1, {"direct": "ok"}, lambda *_: FakeAdapter(), lambda _: FakeMetrica())
    assert items[-1] == {
        "platform": "metrica",
        "ok": False,
        "message": "На этом аккаунте Яндекса нет счётчиков Метрики.",
    }


async def test_missing_direct_is_reported_and_platforms_are_not_called() -> None:
    def resolve(*_):
        raise AssertionError("площадку без кабинета не спрашиваем")

    items = await check_connections(
        1, {"vk": "revoked"}, resolve, lambda _: FakeMetrica([MetricaCounter(1, "С", "a.ru")])
    )
    assert items == [{"platform": "direct", "ok": False, "message": "Директ не подключён."}]
