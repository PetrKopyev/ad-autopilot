import logging
import signal
from threading import Event
from types import FrameType

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings

logger = logging.getLogger(__name__)


def heartbeat() -> None:
    logger.info("Worker heartbeat")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop_event = Event()
    scheduler = BackgroundScheduler(timezone=settings.tz_default)
    scheduler.add_job(heartbeat, "interval", minutes=1, id="heartbeat", max_instances=1, coalesce=True)

    def request_shutdown(signum: int, frame: FrameType | None) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    scheduler.start()
    logger.info("Worker started")
    try:
        stop_event.wait()
    finally:
        scheduler.shutdown(wait=True)
        logger.info("Worker stopped")


if __name__ == "__main__":
    main()
