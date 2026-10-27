"""Синхронная работа в async-коде останавливает цикл событий: все остальные запросы сервера ждут, пока она закончится.

Каждый тест подменяет синхронный шаг: прежде чем работать, шаг ждёт, пока цикл событий выполнит обратный вызов. Шаг,
выполняемый в цикле событий (или в потоке, которого цикл дожидается), этого не дождётся: значит, остальные запросы в это
время стояли бы. Проверка не зависит от скорости машины: под нагрузкой обратный вызов просто выполняется позже."""

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path

import httpx

from app.content import site_reader
from app.content.art import ArtBudget
from app.services import launch as launch_module
from app.services.registry import DemoArt
from tests.services.test_launch import campaigns, launch, ready
from tests.services.test_onboarding import NOW, TODAY, make_world

WAIT = 5


class Blocking:
    """Подменённые шаги и то, обслуживал ли цикл событий другие задачи, пока каждый из них шёл."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.served: list[bool] = []

    def slow[T](self, function: Callable[..., T]) -> Callable[..., T]:
        def wrapper(*args, **kwargs) -> T:
            # После первого простоя не ждём снова: поломку уже видно, а каждый шаг ждал бы WAIT секунд.
            if all(self.served):
                served = threading.Event()
                self.loop.call_soon_threadsafe(served.set)
                self.served.append(served.wait(WAIT))
            return function(*args, **kwargs)

        return wrapper

    @property
    def loop_kept_running(self) -> bool:
        return bool(self.served) and all(self.served)


async def test_cutting_images_at_launch_does_not_stop_other_requests(tmp_path: Path, monkeypatch) -> None:
    world, plan = await ready(tmp_path)
    blocking = Blocking()
    monkeypatch.setattr(launch_module, "make_formats", blocking.slow(launch_module.make_formats))
    await launch(world, plan)
    assert blocking.loop_kept_running
    assert campaigns(world) and all(
        asset.platform_refs for asset in world.repository.assets(world.business.id) if asset.selected
    )


async def test_parsing_site_does_not_stop_other_requests(monkeypatch) -> None:
    blocking = Blocking()
    monkeypatch.setattr(site_reader, "parse_html", blocking.slow(site_reader.parse_html))

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, html="<html><body>Маникюр и педикюр</body></html>")

    await site_reader.read_site("https://salon.example", transport=httpx.MockTransport(handle))
    assert blocking.loop_kept_running


async def test_drawing_demo_image_does_not_stop_other_requests(monkeypatch) -> None:
    blocking = Blocking()
    monkeypatch.setattr(DemoArt, "draw", staticmethod(blocking.slow(DemoArt.draw)))
    await DemoArt(ArtBudget(limit_kop=10_000)).generate("Светлый салон", seed=7, business_id=1)
    assert blocking.loop_kept_running


async def test_site_check_returns_connection_before_reading_site(tmp_path: Path) -> None:
    # Пока читается сайт и отвечает ИИ (десятки секунд), открытая транзакция держала соединение из пула.
    world = make_world(tmp_path)
    events = []
    world.repository.commit = lambda: events.append("commit")
    reader = world.onboarding.reader

    async def read(url: str):
        events.append("read")
        return await reader(url)

    world.onboarding.reader = read
    await world.onboarding.site_check(world.business.id, NOW, TODAY)
    assert events[:2] == ["commit", "read"]


async def test_reading_image_for_vk_upload_does_not_stop_other_requests(tmp_path: Path, monkeypatch) -> None:
    import respx
    from PIL import Image

    from app.platforms import vk
    from tests.platforms.test_vk import API, configuration

    file = tmp_path / "pic.png"
    Image.new("RGB", (600, 600)).save(file)
    blocking = Blocking()
    monkeypatch.setattr(vk, "image_size", blocking.slow(vk.image_size))
    monkeypatch.setattr(Path, "read_bytes", blocking.slow(Path.read_bytes))
    with respx.mock() as api:
        api.post(f"{API}/content/static.json").respond(json={"id": 501})
        async with vk.VkAdapter("tok", config=configuration(), href="https://shop.example/") as adapter:
            await adapter.upload_image(file)
    assert blocking.loop_kept_running


async def test_reading_file_for_simulated_upload_does_not_stop_other_requests(tmp_path: Path, monkeypatch) -> None:
    from app.platforms.fake import FakeAdapter

    file = tmp_path / "asset.bin"
    file.write_bytes(b"test-asset")
    blocking = Blocking()
    monkeypatch.setattr(Path, "read_bytes", blocking.slow(Path.read_bytes))
    await FakeAdapter().upload_image(file)
    assert blocking.loop_kept_running
