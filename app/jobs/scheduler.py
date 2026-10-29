"""Cron scheduling for periodic discovery and stock sync jobs.

The scheduler is stateless: whether a schedule is *due* is decided from the database, not from
in-process memory. A schedule is due when its most recent cron fire time (evaluated in
``SCHEDULE_TIMEZONE``) has passed and no job of that type has been created since. This makes
schedules survive worker restarts and redeploys, and means a long outage produces exactly one
catch-up job rather than a burst.
"""

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.base import utc_now
from app.db.models import Job, Run
from app.jobs.queue import JobQueue

logger = logging.getLogger(__name__)


class JobScheduler:
    """Evaluates cron schedules and enqueues recurring jobs."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.queue = JobQueue(session)

    # ------------------------------------------------------------------ cron maths
    def _zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.settings.SCHEDULE_TIMEZONE)
        except ZoneInfoNotFoundError:
            logger.error(
                "Unknown SCHEDULE_TIMEZONE %r; falling back to UTC", self.settings.SCHEDULE_TIMEZONE
            )
            return ZoneInfo("UTC")

    def should_trigger(self, cron_expr: str, last_check: datetime, now: datetime) -> bool:
        """True when the cron expression fires in the interval (last_check, now]."""
        if not cron_expr or not cron_expr.strip():
            return False
        try:
            next_time = croniter(cron_expr, last_check).get_next(datetime)
            if next_time.tzinfo is None:
                next_time = next_time.replace(tzinfo=UTC)
            return bool(next_time <= now)
        except Exception as e:
            logger.error("Invalid cron expression %r: %s", cron_expr, e)
            return False

    def last_fire_time(self, cron_expr: str, now: datetime) -> datetime | None:
        """Most recent cron fire time at or before ``now`` (timezone-aware, UTC), or None."""
        if not cron_expr or not cron_expr.strip():
            return None
        try:
            local_now = now.astimezone(self._zone())
            prev = croniter(cron_expr, local_now).get_prev(datetime)
            if prev.tzinfo is None:
                prev = prev.replace(tzinfo=self._zone())
            return prev.astimezone(UTC)
        except Exception as e:
            logger.error("Invalid cron expression %r: %s", cron_expr, e)
            return None

    def next_fire_time(self, cron_expr: str, now: datetime) -> datetime | None:
        """Next cron fire time after ``now`` (UTC), or None when disabled/invalid."""
        if not cron_expr or not cron_expr.strip():
            return None
        try:
            local_now = now.astimezone(self._zone())
            nxt = croniter(cron_expr, local_now).get_next(datetime)
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=self._zone())
            return nxt.astimezone(UTC)
        except Exception:
            return None

    # ------------------------------------------------------------------ due evaluation
    async def _job_created_since(self, job_type: str, since: datetime) -> bool:
        query = select(func.count(Job.id)).where(Job.type == job_type, Job.created_at >= since)
        return int((await self.session.execute(query)).scalar_one()) > 0

    async def is_due(self, job_type: str, cron_expr: str, now: datetime) -> bool:
        """A schedule is due when its latest fire time has no job created at/after it."""
        fire = self.last_fire_time(cron_expr, now)
        if fire is None:
            return False
        return not await self._job_created_since(job_type, fire)

    async def check_and_enqueue_scheduled_jobs(self, now: datetime | None = None) -> list[str]:
        """Enqueue every job whose cron time has arrived and has not been enqueued yet."""
        now = now or utc_now()
        enqueued: list[str] = []
        for job_type, cron_expr in (
            ("discovery", self.settings.DISCOVERY_CRON),
            ("stock", self.settings.STOCK_CRON),
        ):
            if await self.is_due(job_type, cron_expr, now):
                job = await self.queue.enqueue_job(job_type, params={"source": "cron"})
                enqueued.append(f"{job_type}:{job.id}")
                logger.info("Cron enqueued %s job %s (schedule %r)", job_type, job.id, cron_expr)
        return enqueued

    async def bootstrap_discovery_if_needed(self) -> str | None:
        """Enqueue exactly one discovery job on a never-crawled deployment.

        Only acts when BOOTSTRAP_DISCOVERY is enabled and no discovery job or run has ever been
        recorded, so restarts and redeploys never create duplicates and a failed first crawl is
        not retried in a tight loop (the normal cron schedule takes over).
        """
        if not self.settings.BOOTSTRAP_DISCOVERY:
            return None
        await self.queue.lock_job_type("discovery")
        jobs = int(
            (
                await self.session.execute(
                    select(func.count(Job.id)).where(Job.type == "discovery")
                )
            ).scalar_one()
        )
        runs = int(
            (
                await self.session.execute(
                    select(func.count(Run.id)).where(Run.type == "discovery")
                )
            ).scalar_one()
        )
        if jobs or runs:
            await self.session.commit()  # release the enqueue lock
            return None
        job = await self.queue.enqueue_job("discovery", params={"source": "bootstrap"})
        logger.info("Bootstrap enqueued first discovery job %s", job.id)
        return str(job.id)
