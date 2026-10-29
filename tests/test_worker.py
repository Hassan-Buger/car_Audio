"""Unit and integration tests for job queue, worker lease, and scheduler."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.queue import JobQueue
from app.jobs.recovery import recover_expired_jobs
from app.jobs.scheduler import JobScheduler


class TestJobQueue:
    """Tests for job queue, leases, and deduplication."""

    @pytest.mark.asyncio
    async def test_enqueue_and_claim_job(self, db_session: AsyncSession) -> None:
        queue = JobQueue(db_session, worker_id="worker-test-1")
        job = await queue.enqueue_job("discovery", params={"test": True})
        assert job.status == "queued"
        assert job.type == "discovery"

        # Claim job
        claimed = await queue.claim_next_job(lease_duration_seconds=30)
        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.status == "running"
        assert claimed.lease_owner == "worker-test-1"
        assert claimed.lease_expires_at is not None

        # Verify heartbeat
        hb_success = await queue.heartbeat(claimed.id, extend_seconds=60)
        assert hb_success is True

        # Complete job
        await queue.complete_job(claimed.id, result_summary={"status": "complete"})
        finished = await queue.get_job(claimed.id)
        assert finished is not None
        assert finished.status == "completed"
        assert finished.lease_expires_at is None

    @pytest.mark.asyncio
    async def test_duplicate_enqueue_returns_existing_active_job(
        self, db_session: AsyncSession
    ) -> None:
        # Invariant 14: Only one discovery or stock run can be active
        queue = JobQueue(db_session, worker_id="worker-test-1")
        job1 = await queue.enqueue_job("discovery")
        job2 = await queue.enqueue_job("discovery")
        assert job1.id == job2.id

    @pytest.mark.asyncio
    async def test_claim_expired_lease_job(self, db_session: AsyncSession) -> None:
        queue = JobQueue(db_session, worker_id="worker-old")
        job = await queue.enqueue_job("stock")

        # Claim with very short lease
        claimed = await queue.claim_next_job(lease_duration_seconds=-10)  # already expired
        assert claimed is not None

        # A second worker should be able to claim it because lease is expired
        queue2 = JobQueue(db_session, worker_id="worker-new")
        claimed2 = await queue2.claim_next_job(lease_duration_seconds=60)
        assert claimed2 is not None
        assert claimed2.id == job.id
        assert claimed2.lease_owner == "worker-new"

    @pytest.mark.asyncio
    async def test_recover_expired_jobs(self, db_session: AsyncSession) -> None:
        queue = JobQueue(db_session, worker_id="dead-worker")
        job = await queue.enqueue_job("discovery")
        await queue.claim_next_job(lease_duration_seconds=-30)  # expired

        count = await recover_expired_jobs(db_session)
        assert count == 1

        recovered = await queue.get_job(job.id)
        assert recovered is not None
        assert recovered.status == "queued"
        assert recovered.lease_owner is None


class TestJobScheduler:
    """Tests for cron scheduling evaluation."""

    def test_cron_trigger_evaluation(self, db_session: AsyncSession) -> None:
        scheduler = JobScheduler(db_session)
        # Daily at 02:00
        cron = "0 2 * * *"
        t1 = datetime(2026, 10, 29, 1, 55, tzinfo=UTC)
        t2 = datetime(2026, 10, 29, 2, 5, tzinfo=UTC)
        assert scheduler.should_trigger(cron, t1, t2) is True

        t3 = datetime(2026, 10, 29, 2, 10, tzinfo=UTC)
        t4 = datetime(2026, 10, 29, 2, 20, tzinfo=UTC)
        assert scheduler.should_trigger(cron, t3, t4) is False
