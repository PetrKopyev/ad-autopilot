import asyncio
import logging
import signal
from contextlib import suppress

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db.session import SessionLocal
from app.demo_flags import demo_enabled, demo_frozen
from app.platforms.fake import FakeAdapter
from app.services.experiments import create_experiment, rewrite_rejected
from app.services.registry import AdapterRegistry, db_loader
from app.worker.clock import SystemClock
from app.worker.jobs import Worker, WorkerRepository

logger = logging.getLogger(__name__)


class DemoRegistry(AdapterRegistry):
    """Демо живёт в процессе API; API и планировщик используют одни симуляторы."""

    def resolve(self, business_id: int, platform: str):
        if self.mode != "fake":
            return super().resolve(business_id, platform)
        key = (business_id, platform)
        # API и тик планировщика обращаются из разных потоков: setdefault не заменит уже созданный симулятор.
        return self.fakes.get(key) or self.fakes.setdefault(key, FakeAdapter(clock=SystemClock()))


async def run_tick(registry: AdapterRegistry, kind: str = "due") -> None:
    if registry.mode == "fake":
        await run_in_threadpool(run_demo_tick, registry, kind)
    else:
        await run_worker_tick(registry, kind)


def run_demo_tick(registry: AdapterRegistry, kind: str) -> None:
    asyncio.run(run_worker_tick(registry, kind))


async def run_worker_tick(registry: AdapterRegistry, kind: str) -> None:
    with SessionLocal() as db:
        worker = Worker(
            WorkerRepository(db),
            registry.resolve,
            dry_run=settings.dry_run,
            experiment_factory=create_experiment,
            rewrite_rejected=rewrite_rejected,
        )
        if kind == "startup":
            await worker.startup()
        elif kind == "heartbeat":
            await worker.run(kind)
        elif kind == "prune":
            worker.prune()
        else:
            await worker.run_due(skip=demo_frozen)


def build_scheduler(registry: AdapterRegistry) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.tz_default)
    options = {"max_instances": 1, "coalesce": True}
    scheduler.add_job(run_tick, "interval", args=[registry, "heartbeat"], minutes=1, id="heartbeat", **options)
    scheduler.add_job(run_tick, "interval", args=[registry], minutes=30, id="run_due", **options)
    scheduler.add_job(run_tick, "cron", args=[registry, "prune"], hour=4, minute=10, id="prune", **options)
    return scheduler


def log_mode() -> None:
    # warning, а не info: под uvicorn журнал приложения показывает только предупреждения.
    if settings.dry_run:
        logger.warning("Пробный режим: автопилот только предлагает")
    else:
        logger.warning("Рабочий режим: автопилот применяет решения")


async def start_scheduler(registry: AdapterRegistry) -> AsyncIOScheduler:
    if demo_enabled():
        logger.warning("Демо-вход включён: стенд только для локальной проверки, без настоящих площадок.")
    await run_tick(registry, "startup")
    # Без отметки при старте главная до первой минутной проверки показывает «Проверки ещё не было».
    await run_tick(registry, "heartbeat")
    scheduler = build_scheduler(registry)
    scheduler.start()
    return scheduler


async def serve() -> None:
    log_mode()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop_event.set)
    if settings.platform_mode == "fake":
        logger.info("демо: планировщик внутри API")
        await stop_event.wait()
        return
    async with httpx.AsyncClient(timeout=30) as http:
        registry = AdapterRegistry(db_loader(SessionLocal), mode=settings.platform_mode, http=http)
        scheduler = await start_scheduler(registry)
        logger.info("Worker started")
        try:
            await stop_event.wait()
        finally:
            scheduler.shutdown(wait=True)
            # APScheduler исполняет shutdown на текущем event loop.
            await asyncio.sleep(0)
            logger.info("Worker stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with suppress(KeyboardInterrupt):
        asyncio.run(serve())


if __name__ == "__main__":
    main()
