import asyncio
import logging
import signal
from contextlib import suppress
from threading import Lock

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db.session import SessionLocal
from app.platforms.fake import FakeAdapter
from app.services.experiments import create_experiment
from app.services.registry import AdapterRegistry, db_loader
from app.worker.clock import SystemClock
from app.worker.jobs import Worker, WorkerRepository

logger = logging.getLogger(__name__)
demo_tick_lock = Lock()


class DemoRegistry(AdapterRegistry):
    """Демо живёт в процессе API; API и планировщик используют одни симуляторы."""

    def resolve(self, business_id: int, platform: str):
        if self.mode != "fake":
            return super().resolve(business_id, platform)
        key = (business_id, platform)
        if key not in self.fakes:
            self.fakes[key] = FakeAdapter(clock=SystemClock())
        return self.fakes[key]


async def run_tick(registry: AdapterRegistry, kind: str = "due") -> None:
    if registry.mode == "fake":
        await run_in_threadpool(run_demo_tick, registry, kind)
    else:
        await run_worker_tick(registry, kind)


def run_demo_tick(registry: AdapterRegistry, kind: str) -> None:
    with demo_tick_lock:
        asyncio.run(run_worker_tick(registry, kind))


async def run_worker_tick(registry: AdapterRegistry, kind: str) -> None:
    with SessionLocal() as db:
        worker = Worker(
            WorkerRepository(db),
            registry.resolve,
            dry_run=settings.dry_run,
            experiment_factory=create_experiment,
        )
        if kind == "startup":
            await worker.startup()
        elif kind == "heartbeat":
            await worker.run(kind)
        else:
            await worker.run_due()


def build_scheduler(registry: AdapterRegistry) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.tz_default)
    options = {"max_instances": 1, "coalesce": True}
    scheduler.add_job(run_tick, "interval", args=[registry, "heartbeat"], minutes=1, id="heartbeat", **options)
    scheduler.add_job(run_tick, "interval", args=[registry], minutes=30, id="run_due", **options)
    return scheduler


async def start_scheduler(registry: AdapterRegistry) -> AsyncIOScheduler:
    await run_tick(registry, "startup")
    scheduler = build_scheduler(registry)
    scheduler.start()
    return scheduler


async def serve() -> None:
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
