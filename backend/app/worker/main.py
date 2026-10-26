import asyncio
import logging
import signal
from functools import partial
from threading import Event
from types import FrameType

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.db.session import SessionLocal
from app.platforms.base import AdPlatform
from app.platforms.fake import FakeAdapter
from app.services.experiments import create_experiment
from app.worker.jobs import Worker, WorkerRepository, dispatch

logger = logging.getLogger(__name__)

_fake_adapters: dict[int, FakeAdapter] = {}


def resolve_adapter(business_id: int, platform: str) -> AdPlatform:
    if settings.platform_mode != "fake":
        raise RuntimeError("Боевые адаптеры подключаются к воркеру вместе с кабинетами площадок.")
    return _fake_adapters.setdefault(business_id, FakeAdapter())


def startup() -> None:
    with SessionLocal() as db:
        asyncio.run(
            Worker(
                WorkerRepository(db),
                resolve_adapter,
                dry_run=settings.dry_run,
                experiment_factory=create_experiment,
            ).startup()
        )


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.tz_default)
    job = partial(
        dispatch,
        SessionLocal,
        resolve_adapter,
        dry_run=settings.dry_run,
        experiment_factory=create_experiment,
    )
    options = {"max_instances": 1, "coalesce": True}
    scheduler.add_job(job, "interval", ["heartbeat"], minutes=1, id="heartbeat", **options)
    scheduler.add_job(job, "interval", ["budget_guard"], minutes=30, id="budget_guard", **options)
    scheduler.add_job(job, "interval", ["sync_moderation"], hours=1, id="sync_moderation", **options)
    scheduler.add_job(job, "interval", ["sync_stats"], hours=3, id="sync_stats", **options)
    for kind in ("optimize", "experiments_tick", "rebalance_budget"):
        scheduler.add_job(job, "cron", [kind], hour=6, minute=kind_minute(kind), id=kind, **options)
    scheduler.add_job(job, "cron", ["clients_survey"], day_of_week="mon", hour=10, id="clients_survey", **options)
    scheduler.add_job(job, "cron", ["month_rollover"], hour=0, minute=5, id="month_rollover", **options)
    return scheduler


def kind_minute(kind: str) -> int:
    # Сначала решения и тесты, потом пересчёт лимитов уже по их итогам.
    return {"optimize": 0, "experiments_tick": 5, "rebalance_budget": 10}[kind]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop_event = Event()
    scheduler = build_scheduler()

    def request_shutdown(signum: int, frame: FrameType | None) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    startup()
    scheduler.start()
    logger.info("Worker started")
    try:
        stop_event.wait()
    finally:
        scheduler.shutdown(wait=True)
        logger.info("Worker stopped")


if __name__ == "__main__":
    main()
