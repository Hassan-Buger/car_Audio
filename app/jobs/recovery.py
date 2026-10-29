"""Worker startup recovery of interrupted runs and expired leases."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utc_now
from app.db.models import Job, Run

logger = logging.getLogger(__name__)


async def recover_interrupted_runs(session: AsyncSession) -> list[Run]:
    """Find interrupted runs left in 'running' state and prepare them for resumption.

    Invariant 16: Restarting a worker does not lose progress or reset counters.
    """
    query = select(Run).where(Run.status == "running").order_by(Run.started_at.asc())
    result = await session.execute(query)
    interrupted_runs = list(result.scalars().all())

    if interrupted_runs:
        for run in interrupted_runs:
            logger.warning(
                "Found interrupted run %s (type=%s, started_at=%s). Will resume from checkpoints.",
                run.id,
                run.type,
                run.started_at,
            )

    return interrupted_runs


async def recover_expired_jobs(session: AsyncSession) -> int:
    """Reset expired running jobs so another worker can claim them."""
    now = utc_now()
    query = select(Job).where(Job.status == "running", Job.lease_expires_at <= now)
    result = await session.execute(query)
    expired_jobs = list(result.scalars().all())

    for job in expired_jobs:
        logger.info(
            "Recovering expired job %s (owner was %s, expired at %s)",
            job.id,
            job.lease_owner,
            job.lease_expires_at,
        )
        job.status = "queued"
        job.lease_owner = None
        job.lease_expires_at = None

    if expired_jobs:
        await session.commit()

    return len(expired_jobs)
