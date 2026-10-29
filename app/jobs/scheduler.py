"""Cron scheduling evaluator for periodic discovery and stock sync jobs."""

import logging
from datetime import UTC, datetime

from croniter import croniter
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.base import utc_now
from app.jobs.queue import JobQueue

logger = logging.getLogger(__name__)


class JobScheduler:
    """Evaluates cron schedules and enqueues recurring jobs."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.queue = JobQueue(session)
        self._last_discovery_check: datetime = utc_now()
        self._last_stock_check: datetime = utc_now()

    def should_trigger(self, cron_expr: str, last_check: datetime, now: datetime) -> bool:
        """Check if cron expression should have fired between last_check and now."""
        try:
            it = croniter(cron_expr, last_check)
            next_time = it.get_next(datetime)
            # Ensure next_time is timezone-aware
            if next_time.tzinfo is None:
                next_time = next_time.replace(tzinfo=UTC)
            return bool(next_time <= now)
        except Exception as e:
            logger.error("Invalid cron expression %r: %s", cron_expr, e)
            return False

    async def check_and_enqueue_scheduled_jobs(self) -> list[str]:
        """Evaluate schedules and enqueue jobs whose cron times have arrived."""
        now = utc_now()
        enqueued: list[str] = []

        # Check discovery schedule
        if self.should_trigger(self.settings.DISCOVERY_CRON, self._last_discovery_check, now):
            job = await self.queue.enqueue_job("discovery", params={"source": "cron"})
            enqueued.append(f"discovery:{job.id}")
            self._last_discovery_check = now

        # Check stock schedule
        if self.should_trigger(self.settings.STOCK_CRON, self._last_stock_check, now):
            job = await self.queue.enqueue_job("stock", params={"source": "cron"})
            enqueued.append(f"stock:{job.id}")
            self._last_stock_check = now

        return enqueued
