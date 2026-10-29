"""Background worker process executing crawler jobs, leases, and schedules."""

import asyncio
import logging
import signal
import sys
import uuid
from typing import Any

from app.config import get_settings
from app.db.session import get_sessionmaker
from app.jobs.queue import JobQueue
from app.jobs.recovery import recover_expired_jobs, recover_interrupted_runs
from app.jobs.scheduler import JobScheduler
from app.sync.engine import SyncEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("worker")


async def _heartbeat_loop(
    session_factory: Any, worker_id: str, job_id: uuid.UUID, stop_event: asyncio.Event
) -> None:
    """Periodically extend lease while job is running."""
    while not stop_event.is_set():
        try:
            await asyncio.sleep(20)
            async with session_factory() as hb_session:
                hb_queue = JobQueue(hb_session, worker_id)
                await hb_queue.heartbeat(job_id, extend_seconds=60)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Heartbeat failed for job %s: %s", job_id, exc)


class WorkerService:
    """Manages the long-running worker lifecycle."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        self.running = False
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        """Run the worker process until stopped."""
        self.running = True
        logger.info(
            "Starting Digisound Worker (id=%s, env=%s)", self.worker_id, self.settings.ENVIRONMENT
        )

        session_factory = get_sessionmaker()

        # Startup recovery
        async with session_factory() as session:
            expired_count = await recover_expired_jobs(session)
            if expired_count:
                logger.info("Startup: recovered %s expired jobs", expired_count)

            interrupted = await recover_interrupted_runs(session)
            if interrupted:
                logger.info("Startup: found %s interrupted runs to resume", len(interrupted))

        poll_interval = 5.0
        while self.running:
            try:
                async with session_factory() as session:
                    scheduler = JobScheduler(session, self.settings)
                    await scheduler.check_and_enqueue_scheduled_jobs()

                    queue = JobQueue(session, self.worker_id)
                    job = await queue.claim_next_job(lease_duration_seconds=60)

                    if job:
                        logger.info("Processing job %s (type=%s)", job.id, job.type)
                        job_id = job.id
                        job_type = job.type
                        run_id = job.run_id

                        # Background heartbeat task
                        heartbeat_stop = asyncio.Event()
                        hb_task = asyncio.create_task(
                            _heartbeat_loop(session_factory, self.worker_id, job_id, heartbeat_stop)
                        )

                        try:
                            engine = SyncEngine(session, self.settings)
                            if job_type == "discovery":
                                run_result = await engine.execute_discovery_run(run_id=run_id)
                                summary = {
                                    "run_id": str(run_result.id),
                                    "status": run_result.status,
                                    "total_products": run_result.total_products_discovered,
                                    "new": run_result.new_products,
                                    "updated": run_result.updated_products,
                                }
                                await queue.complete_job(job_id, result_summary=summary)
                            elif job_type == "stock":
                                # Stock refresh run
                                run_result = await engine.execute_discovery_run(run_id=run_id)
                                summary = {
                                    "run_id": str(run_result.id),
                                    "status": run_result.status,
                                    "updated": run_result.updated_products,
                                }
                                await queue.complete_job(job_id, result_summary=summary)
                            else:
                                await queue.fail_job(job_id, f"Unknown job type: {job_type}")

                        except Exception as e:
                            logger.exception("Error executing job %s: %s", job_id, e)
                            await queue.fail_job(job_id, str(e))
                        finally:
                            heartbeat_stop.set()
                            hb_task.cancel()
                            try:
                                await hb_task
                            except asyncio.CancelledError:
                                pass

                    else:
                        # No job claimed; wait before next poll
                        try:
                            await asyncio.wait_for(self._stop_event.wait(), timeout=poll_interval)
                        except TimeoutError:
                            pass

            except Exception as e:
                logger.exception("Unexpected error in worker loop: %s", e)
                await asyncio.sleep(poll_interval)

        logger.info("Worker %s shut down cleanly", self.worker_id)

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
