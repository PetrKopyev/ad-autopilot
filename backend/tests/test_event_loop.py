"""Синхронная работа в async-коде останавливает цикл событий: все остальные запросы сервера ждут, пока она закончится.

Каждый тест замедляет синхронный шаг и проверяет, что параллельная задача за это время продолжает работать."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from app.content import site_reader
from app.content.art import ArtBudget
from app.services import launch as launch_module
from app.services.registry import DemoArt
from tests.services.test_launch import campaigns, launch, ready
from tests.services.test_onboarding import NOW, TODAY, make_world

SLOW = 0.3


def slow[T](function: Callable[..., T]) -> Callable[..., T]:
    def wrapper(*args, **kwargs) -> T:
        time.sleep(SLOW)
        return function(*args, **kwargs)

    return wrapper


async def longest_pause(work: Awaitable[object]) -> float:
    """Самая долгая пауза параллельной задачи, пока идёт work."""
    task = asyncio.ensure_future(work)
    pauses, last = [0.0], time.monotonic()
    while not task.done():
        await asyncio.sleep(0.01)
        now = time.monotonic()
        pauses.append(now - last)
        last = now
    await task
    return max(pauses)


async def test_cutting_images_at_launch_does_not_stop_other_requests(tmp_path: Path, monkeypatch) -> None:
    world, plan = await ready(tmp_path)
    monkeypatch.setattr(launch_module, "make_formats", slow(launch_module.make_formats))
    assert await longest_pause(launch(world, plan)) < SLOW / 2
    assert campaigns(world) and all(
        asset.platform_refs for asset in world.repository.assets(world.business.id) if asset.selected
    )


async def test_parsing_site_does_not_stop_other_requests(monkeypatch) -> None:
    monkeypatch.setattr(site_reader, "parse_html", slow(site_reader.parse_html))

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, html="<html><body>Маникюр и педикюр</body></html>")

    work = site_reader.read_site("https://salon.example", transport=httpx.MockTransport(handle))
    assert await longest_pause(work) < SLOW / 2


async def test_drawing_demo_image_does_not_stop_other_requests(monkeypatch) -> None:
    monkeypatch.setattr(DemoArt, "draw", staticmethod(slow(DemoArt.draw)))
    art = DemoArt(ArtBudget(limit_kop=10_000))
    assert await longest_pause(art.generate("Светлый салон", seed=7, business_id=1)) < SLOW / 2


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
