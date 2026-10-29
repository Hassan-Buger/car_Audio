"""Database-backed job queue with lease ownership and heartbeats."""

import hashlib
import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utc_now
from app.db.models import Job

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 60


class JobQueue:
    """Manages transactional job queue operations."""

    def __init__(self, session: AsyncSession, worker_id: str | None = None) -> None:
        self.session = session
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"

    async def enqueue_job(self, job_type: str, params: dict[str, Any] | None = None) -> Job:
        """Enqueue a new job.

        If a job of the same type is already queued or actively running with a valid lease,
        returns that existing job instead of creating a duplicate.
        """
        await self.lock_job_type(job_type)
        active_job = await self.get_active_job(job_type)
        if active_job:
            logger.info("Active or queued %s job %s already exists", job_type, active_job.id)
            await self.session.commit()  # release the enqueue lock
            return active_job

        job = Job(
            type=job_type,
            status="queued",
            params=params or {},
            created_at=utc_now(),
        )
        self.session.add(job)
        await self.session.commit()
        logger.info("Enqueued %s job %s", job_type, job.id)
        return job

    async def lock_job_type(self, job_type: str) -> None:
        """Serialise enqueue decisions for a job type (transaction-level advisory lock)."""
        digest = hashlib.sha256(f"digisound-enqueue:{job_type}".encode()).digest()
        key = int.from_bytes(digest[:8], "big", signed=True)
        await self.session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})

    async def get_job(self, job_id: uuid.UUID) -> Job | None:
        """Fetch job by ID."""
        query = select(Job).where(Job.id == job_id)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_active_job(self, job_type: str) -> Job | None:
        """Find active (queued or running with valid lease) job of given type."""
        now = utc_now()
        query = (
            select(Job)
            .where(
                Job.type == job_type,
                or_(
                    Job.status == "queued",
                    (Job.status == "running") & (Job.lease_expires_at > now),
                ),
            )
            .order_by(Job.created_at.asc())
            .limit(1)
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def claim_next_job(
        self, lease_duration_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> Job | None:
        """Claim the next queued or expired running job atomically."""
        now = utc_now()
        # Find candidate jobs
        query = (
            select(Job)
            .where(
                or_(
                    Job.status == "queued",
                    (Job.status == "running") & (Job.lease_expires_at <= now),
                )
            )
            .order_by(Job.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        result = await self.session.execute(query)
        job = result.scalar_one_or_none()

        if job:
            job.status = "running"
            job.lease_owner = self.worker_id
            job.lease_expires_at = now + timedelta(seconds=lease_duration_seconds)
            if not job.started_at:
                job.started_at = now
            await self.session.commit()
            logger.info("Worker %s claimed job %s (type=%s)", self.worker_id, job.id, job.type)
            return job

        return None

    async def heartbeat(
        self, job_id: uuid.UUID, extend_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> bool:
        """Extend the lease for an active running job."""
        now = utc_now()
        query = select(Job).where(
            Job.id == job_id, Job.status == "running", Job.lease_owner == self.worker_id
        )
        result = await self.session.execute(query)
        job = result.scalar_one_or_none()
        if job:
            job.lease_expires_at = now + timedelta(seconds=extend_seconds)
            await self.session.commit()
            return True
        return False

    async def complete_job(
        self, job_id: uuid.UUID, result_summary: dict[str, Any] | None = None
    ) -> None:
        """Mark job completed."""
        job = await self.get_job(job_id)
        if job:
            job.status = "completed"
            job.lease_expires_at = None
            job.finished_at = utc_now()
            job.result_summary = result_summary
            await self.session.commit()
            logger.info("Job %s marked completed", job_id)

    async def fail_job(
        self,
        job_id: uuid.UUID,
        error_message: str,
        result_summary: dict[str, Any] | None = None,
    ) -> None:
        """Mark job failed."""
        job = await self.get_job(job_id)
        if job:
            job.status = "failed"
            job.lease_expires_at = None
            job.finished_at = utc_now()
            job.error = error_message
            if result_summary is not None:
                job.result_summary = result_summary
            await self.session.commit()
            logger.error("Job %s marked failed: %s", job_id, error_message)

    async def attach_run(self, job_id: uuid.UUID, run_id: uuid.UUID) -> None:
        """Record which run a job is executing so a restarted worker resumes the same run."""
        job = await self.get_job(job_id)
        if job and job.run_id != run_id:
            job.run_id = run_id
            await self.session.commit()

    async def requeue_job(self, job_id: uuid.UUID) -> None:
        """Release a claimed job back to the queue (e.g. the single-run lock is busy)."""
        job = await self.get_job(job_id)
        if job:
            job.status = "queued"
            job.lease_owner = None
            job.lease_expires_at = None
            await self.session.commit()
            logger.info("Job %s returned to the queue", job_id)
