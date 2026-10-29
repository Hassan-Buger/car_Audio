"""Background worker process executing crawler jobs, leases, and schedules."""

import asyncio
import logging
import signal
import sys
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import Job
from app.db.repo import Repository
from app.db.session import get_sessionmaker
from app.jobs.queue import JobQueue
from app.jobs.recovery import recover_expired_jobs, recover_interrupted_runs
from app.jobs.scheduler import JobScheduler
from app.scraper.client import ScraperClient
from app.sync.engine import RunLockHeldError, SyncEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("worker")

POLL_INTERVAL_SECONDS = 5.0
LEASE_SECONDS = 60
HEARTBEAT_EVERY_SECONDS = 20


async def _heartbeat_loop(
    session_factory: Any,
    worker_id: str,
    job_id: uuid.UUID,
    stop_event: asyncio.Event,
    interval: float = HEARTBEAT_EVERY_SECONDS,
) -> None:
    """Periodically extend lease while job is running."""
    while not stop_event.is_set():
        try:
            await asyncio.sleep(interval)
            async with session_factory() as hb_session:
                hb_queue = JobQueue(hb_session, worker_id)
                await hb_queue.heartbeat(job_id, extend_seconds=LEASE_SECONDS)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Heartbeat failed for job %s: %s", job_id, exc)


async def execute_job(
    session: AsyncSession,
    queue: JobQueue,
    job: Job,
    settings: Settings,
    scraper_client: ScraperClient | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """Execute one claimed job. Returns 'completed', 'failed', 'skipped' or 'requeued'.

    A job only counts as completed when its run is ``complete`` - an ``incomplete`` or
    ``failed`` run (blocked source, failed requests, count discrepancy) marks the job failed
    with the run's reason so it is never mistaken for a verified crawl.
    """
    job_id, job_type, run_id = job.id, job.type, job.run_id
    engine = SyncEngine(session, settings, scraper_client, should_stop=should_stop)

    async def _attach(run: Any) -> None:
        await queue.attach_run(job_id, run.id)

    try:
        if job_type == "discovery":
            run = await engine.execute_discovery_run(run_id=run_id, run_created_callback=_attach)
        elif job_type == "stock":
            if await Repository(session).count_products() == 0:
                await queue.complete_job(
                    job_id,
                    result_summary={"skipped": "catalog is empty; run discovery first"},
                )
                return "skipped"
            run = await engine.execute_stock_run(run_id=run_id, run_created_callback=_attach)
        else:
            await queue.fail_job(job_id, f"Unknown job type: {job_type}")
            return "failed"
    except RunLockHeldError:
        logger.info("Run lock busy; returning job %s to the queue", job_id)
        await queue.requeue_job(job_id)
        return "requeued"
    except Exception as exc:
        logger.exception("Error executing job %s", job_id)
        await session.rollback()
        await queue.fail_job(job_id, f"{type(exc).__name__}: {exc}"[:2000])
        return "failed"

    if run.status == "running":
        # Graceful shutdown mid-run: keep the run resumable and hand the job back.
        await queue.requeue_job(job_id)
        return "requeued"

    summary = {
        "run_id": str(run.id),
        "run_status": run.status,
        "total_products": run.total_products_discovered,
        "new": run.new_products,
        "updated": run.updated_products,
        "unchanged": run.unchanged_products,
        "failed_requests": run.failed_requests,
        "categories": f"{run.completed_categories}/{run.total_categories}",
    }
    if run.status == "complete":
        await queue.complete_job(job_id, result_summary=summary)
        return "completed"
    await queue.fail_job(
        job_id,
        f"Run {run.status}: {run.error_summary or 'see run record'}"[:2000],
        result_summary=summary,
    )
    return "failed"


class WorkerService:
    """Manages the long-running worker lifecycle."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        self.running = False
        self._stop_event = asyncio.Event()

    async def _startup(self, session_factory: Any) -> None:
        """Recover leases/runs and bootstrap the first crawl; retried until the DB is ready."""
        delay = 2.0
        while self.running:
            try:
                async with session_factory() as session:
                    expired_count = await recover_expired_jobs(session)
                    if expired_count:
                        logger.info("Startup: recovered %s expired jobs", expired_count)
                    interrupted = await recover_interrupted_runs(session)
                    if interrupted:
                        logger.info(
                            "Startup: found %s interrupted runs to resume", len(interrupted)
                        )
                    bootstrapped = await JobScheduler(
                        session, self.settings
                    ).bootstrap_discovery_if_needed()
                    if bootstrapped:
                        logger.info("Startup: bootstrap discovery job %s enqueued", bootstrapped)
                return
            except Exception as exc:
                logger.warning(
                    "Startup not ready (%s: %s); retrying in %.0fs", type(exc).__name__, exc, delay
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, 60.0)

    async def run(self) -> None:
        """Run the worker process until stopped."""
        self.running = True
        logger.info(
            "Starting Digisound Worker (id=%s, env=%s, discovery_cron=%r, stock_cron=%r, tz=%s)",
            self.worker_id,
            self.settings.ENVIRONMENT,
            self.settings.DISCOVERY_CRON,
            self.settings.STOCK_CRON,
            self.settings.SCHEDULE_TIMEZONE,
        )
        session_factory = get_sessionmaker()
        await self._startup(session_factory)

        while self.running:
            try:
                async with session_factory() as session:
                    await JobScheduler(session, self.settings).check_and_enqueue_scheduled_jobs()

                    queue = JobQueue(session, self.worker_id)
                    job = await queue.claim_next_job(lease_duration_seconds=LEASE_SECONDS)

                    if job:
                        logger.info("Processing job %s (type=%s)", job.id, job.type)
                        heartbeat_stop = asyncio.Event()
                        hb_task = asyncio.create_task(
                            _heartbeat_loop(session_factory, self.worker_id, job.id, heartbeat_stop)
                        )
                        try:
                            outcome = await execute_job(
                                session,
                                queue,
                                job,
                                self.settings,
                                should_stop=lambda: not self.running,
                            )
                            logger.info("Job %s finished: %s", job.id, outcome)
                        finally:
                            heartbeat_stop.set()
                            hb_task.cancel()
                            try:
                                await hb_task
                            except asyncio.CancelledError:
                                pass
                        if outcome == "requeued":
                            await self._sleep(POLL_INTERVAL_SECONDS)
                    else:
                        await self._sleep(POLL_INTERVAL_SECONDS)

            except Exception as e:
                logger.exception("Unexpected error in worker loop: %s", e)
                await self._sleep(POLL_INTERVAL_SECONDS)

        logger.info("Worker %s shut down cleanly", self.worker_id)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass

    def stop(self) -> None:
        """Signal worker to stop gracefully."""
        self.running = False
        self._stop_event.set()


async def main() -> None:
    """Worker entry point."""
    worker = WorkerService()

    def _handle_signal() -> None:
        logger.info("Received termination signal; stopping worker...")
        worker.stop()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows signal handlers for SIGTERM may not be supported by add_signal_handler
            pass

    await worker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Worker interrupted by user; exiting.")
        sys.exit(0)
