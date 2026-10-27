import asyncio
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


@pytest.fixture
def worker_main(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@invalid/test")
    monkeypatch.setenv("APP_BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-only")
    from app.worker import main

    monkeypatch.setattr(
        main, "settings", SimpleNamespace(platform_mode="live", dry_run=False, tz_default="Europe/Moscow")
    )
    return main


async def test_live_worker_uses_database_registry_and_one_http_client(worker_main, monkeypatch):
    main = worker_main
    loader = MagicMock(wraps=main.db_loader)
    monkeypatch.setattr(main, "db_loader", loader)
    scheduler = MagicMock()
    seen = []

    async def start(registry):
        assert isinstance(registry, main.AdapterRegistry)
        assert registry.mode == "live" and not registry.http.is_closed
        from app.services.registry import AccountInfo

        registry.load = lambda *_: AccountInfo("ok", "https://salon.example", 12, True, "test-only")
        direct, vk = registry.resolve(1, "direct"), registry.resolve(1, "vk")
        assert direct._client is registry.http and vk._client is registry.http
        seen.append(registry)
        return scheduler

    monkeypatch.setattr(main, "start_scheduler", start)
    monkeypatch.setattr(main.asyncio, "Event", lambda: SimpleNamespace(wait=AsyncMock(), set=MagicMock()))
    monkeypatch.setattr(main.asyncio, "get_running_loop", MagicMock())
    await main.serve()
    loader.assert_called_once_with(main.SessionLocal)
    assert seen[0].http.is_closed
    scheduler.shutdown.assert_called_once_with(wait=True)


async def test_scheduler_calls_shared_run_due_every_half_hour_and_heartbeat_each_minute(worker_main, monkeypatch):
    main = worker_main
    async with httpx.AsyncClient() as http:
        registry = main.AdapterRegistry(lambda *_: None, mode="fake", http=http)
        scheduler = main.build_scheduler(registry)
        jobs = {job.id: job for job in scheduler.get_jobs()}
        assert set(jobs) == {"heartbeat", "run_due", "prune"}
        assert jobs["heartbeat"].trigger.interval.total_seconds() == 60
        assert jobs["run_due"].trigger.interval.total_seconds() == 1800
        assert str(jobs["prune"].trigger) == "cron[hour='4', minute='10']"
        db = MagicMock()
        monkeypatch.setattr(main, "SessionLocal", db)
        worker = MagicMock()
        worker.run_due = AsyncMock()
        worker.run = AsyncMock()
        worker.startup = AsyncMock()
        worker_type = MagicMock(return_value=worker)
        monkeypatch.setattr(main, "Worker", worker_type)
        await jobs["run_due"].func(*jobs["run_due"].args)
        await jobs["heartbeat"].func(*jobs["heartbeat"].args)
        await jobs["prune"].func(*jobs["prune"].args)
        worker.run_due.assert_awaited_once_with(skip=main.demo_frozen)
        worker.run.assert_awaited_once_with("heartbeat")
        worker.prune.assert_called_once_with()
        assert worker_type.call_args.args[1] == registry.resolve


async def test_fake_api_lifespan_shares_registry_with_scheduler_and_stops_it(worker_main, monkeypatch):
    from app import main
    from app.platforms.base import Operation

    scheduler = MagicMock()
    start = AsyncMock(return_value=scheduler)
    monkeypatch.setattr(main, "settings", SimpleNamespace(platform_mode="fake"))
    monkeypatch.setattr(main, "start_scheduler", start)
    async with main.lifespan(main.app):
        registry = main.app.state.platform_adapters
        assert start.await_args.args[0] is registry
        api_adapter = registry.resolve(1, "direct")
        refs = await api_adapter.create_campaign_tree(api_adapter.scenario.plan)
        worker_adapter = start.await_args.args[0].resolve(1, "direct")
        assert worker_adapter is api_adapter
        await worker_adapter.apply(
            Operation("set_status", "campaign", refs.campaign_id, "status", "active", "paused_auto")
        )
        assert (await api_adapter.get_state(refs))[refs.campaign_id].status == "paused_auto"
        assert registry.resolve(2, "direct") is not api_adapter
    scheduler.shutdown.assert_called_once_with(wait=True)


async def test_fake_mode_does_not_start_a_separate_worker(worker_main, monkeypatch):
    main = worker_main
    main.settings.platform_mode = "fake"
    start = AsyncMock(side_effect=AssertionError("Демо работает в API"))
    monkeypatch.setattr(main, "start_scheduler", start)
    wait = AsyncMock()
    monkeypatch.setattr(main.asyncio, "Event", lambda: SimpleNamespace(wait=wait, set=MagicMock()))
    monkeypatch.setattr(main.asyncio, "get_running_loop", MagicMock())
    await main.serve()
    wait.assert_awaited_once()
    start.assert_not_awaited()


async def test_fake_tick_runs_in_thread_and_keeps_event_loop_responsive(worker_main, monkeypatch):
    main = worker_main
    release = Event()
    caller = get_ident()
    seen = []

    async def tick(registry, kind):
        seen.append((get_ident(), release.wait(timeout=1)))

    monkeypatch.setattr(main, "run_worker_tick", tick)
    asyncio.get_running_loop().call_later(0.01, release.set)
    await main.run_tick(SimpleNamespace(mode="fake"))
    assert len(seen) == 1 and seen[0][0] != caller and seen[0][1]


async def test_scheduler_marks_heartbeat_on_start(worker_main, monkeypatch):
    main = worker_main
    calls = []

    async def tick(registry, kind="due"):
        calls.append(kind)

    scheduler = MagicMock()
    monkeypatch.setattr(main, "run_tick", tick)
    monkeypatch.setattr(main, "build_scheduler", lambda registry: scheduler)
    assert await main.start_scheduler(None) is scheduler
    assert calls == ["startup", "heartbeat"]
    scheduler.start.assert_called_once_with()
