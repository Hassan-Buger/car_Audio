"""Tests for the DB-backed cron scheduler and first-start bootstrap."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Job, Run
from app.jobs.scheduler import JobScheduler


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "DATABASE_URL": "postgresql+asyncpg://postgres:postgres@localhost:5432/digisound_test",
        "DISCOVERY_CRON": "0 2 * * *",
        "STOCK_CRON": "0 */4 * * *",
        "SCHEDULE_TIMEZONE": "UTC",
        "BOOTSTRAP_DISCOVERY": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def job_count(session: AsyncSession, job_type: str | None = None) -> int:
    query = select(func.count(Job.id))
    if job_type:
        query = query.where(Job.type == job_type)
    return int((await session.execute(query)).scalar_one())


class TestCronMath:
    def test_last_fire_time_utc(self, db_session: AsyncSession) -> None:
        sched = JobScheduler(db_session, make_settings())
        now = datetime(2026, 10, 29, 14, 30, tzinfo=UTC)
        assert sched.last_fire_time("0 2 * * *", now) == datetime(2026, 10, 29, 2, 0, tzinfo=UTC)
        assert sched.last_fire_time("0 */4 * * *", now) == datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

    def test_last_fire_time_asia_karachi(self, db_session: AsyncSession) -> None:
        sched = JobScheduler(db_session, make_settings(SCHEDULE_TIMEZONE="Asia/Karachi"))
        # 22:00 UTC on Oct 29 is 03:00 on Oct 30 in Karachi (UTC+5); 02:00 PKT = 21:00 UTC.
        now = datetime(2026, 10, 29, 22, 0, tzinfo=UTC)
        assert sched.last_fire_time("0 2 * * *", now) == datetime(2026, 10, 29, 21, 0, tzinfo=UTC)
        assert sched.next_fire_time("0 2 * * *", now) == datetime(2026, 10, 30, 21, 0, tzinfo=UTC)

    def test_empty_or_invalid_cron_disables_schedule(self, db_session: AsyncSession) -> None:
        sched = JobScheduler(db_session, make_settings())
        now = datetime(2026, 10, 29, 14, 30, tzinfo=UTC)
        assert sched.last_fire_time("", now) is None
        assert sched.last_fire_time("not a cron", now) is None
        assert sched.next_fire_time("", now) is None


class TestScheduleDueness:
    @pytest.mark.asyncio
    async def test_due_when_no_job_since_last_fire(self, db_session: AsyncSession) -> None:
        sched = JobScheduler(db_session, make_settings())
        now = datetime(2026, 10, 29, 2, 5, tzinfo=UTC)
        assert await sched.is_due("discovery", "0 2 * * *", now) is True

    @pytest.mark.asyncio
    async def test_fires_exactly_once_across_fresh_scheduler_instances(
        self, db_session: AsyncSession
    ) -> None:
        """Regression: the old scheduler was rebuilt every poll with last_check=now, never firing."""
        settings = make_settings(STOCK_CRON="")
        now = datetime(2026, 10, 29, 2, 5, tzinfo=UTC)
        first = await JobScheduler(db_session, settings).check_and_enqueue_scheduled_jobs(now)
        assert len(first) == 1 and first[0].startswith("discovery:")
        # A "restarted worker" builds a brand new scheduler and polls again: no duplicate.
        again = await JobScheduler(db_session, settings).check_and_enqueue_scheduled_jobs(
            now + timedelta(minutes=10)
        )
        assert again == []
        assert await job_count(db_session, "discovery") == 1

    @pytest.mark.asyncio
    async def test_not_due_when_job_created_after_fire_time(self, db_session: AsyncSession) -> None:
        sched = JobScheduler(db_session, make_settings(STOCK_CRON=""))
        db_session.add(
            Job(
                type="discovery",
                status="completed",
                params={},
                created_at=datetime(2026, 10, 29, 2, 1, tzinfo=UTC),
            )
        )
        await db_session.commit()
        now = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
        assert await sched.is_due("discovery", "0 2 * * *", now) is False

    @pytest.mark.asyncio
    async def test_due_again_on_next_day(self, db_session: AsyncSession) -> None:
        sched = JobScheduler(db_session, make_settings(STOCK_CRON=""))
        db_session.add(
            Job(
                type="discovery",
                status="completed",
                params={},
                created_at=datetime(2026, 10, 29, 2, 1, tzinfo=UTC),
            )
        )
        await db_session.commit()
        now = datetime(2026, 10, 30, 2, 0, 30, tzinfo=UTC)
        assert await sched.is_due("discovery", "0 2 * * *", now) is True

    @pytest.mark.asyncio
    async def test_long_outage_produces_one_catch_up_job(self, db_session: AsyncSession) -> None:
        db_session.add(
            Job(
                type="stock",
                status="completed",
                params={},
                created_at=datetime(2026, 10, 20, 0, 0, tzinfo=UTC),
            )
        )
        await db_session.commit()
        settings = make_settings(DISCOVERY_CRON="")
        now = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)
        enq = await JobScheduler(db_session, settings).check_and_enqueue_scheduled_jobs(now)
        assert len(enq) == 1 and enq[0].startswith("stock:")
        assert await job_count(db_session, "stock") == 2

    @pytest.mark.asyncio
    async def test_disabled_schedules_enqueue_nothing(self, db_session: AsyncSession) -> None:
        settings = make_settings(DISCOVERY_CRON="", STOCK_CRON="")
        assert await JobScheduler(db_session, settings).check_and_enqueue_scheduled_jobs() == []


class TestBootstrap:
    @pytest.mark.asyncio
    async def test_disabled_flag_does_nothing(self, db_session: AsyncSession) -> None:
        sched = JobScheduler(db_session, make_settings(BOOTSTRAP_DISCOVERY=False))
        assert await sched.bootstrap_discovery_if_needed() is None
        assert await job_count(db_session) == 0

    @pytest.mark.asyncio
    async def test_enqueues_exactly_one_job(self, db_session: AsyncSession) -> None:
        settings = make_settings(BOOTSTRAP_DISCOVERY=True)
        first = await JobScheduler(db_session, settings).bootstrap_discovery_if_needed()
        assert first is not None
        # Restart / redeploy: bootstrap again, must not duplicate
        second = await JobScheduler(db_session, settings).bootstrap_discovery_if_needed()
        assert second is None
        assert await job_count(db_session, "discovery") == 1

    @pytest.mark.asyncio
    async def test_failed_first_job_is_not_retried_by_bootstrap(
        self, db_session: AsyncSession
    ) -> None:
        settings = make_settings(BOOTSTRAP_DISCOVERY=True)
        await JobScheduler(db_session, settings).bootstrap_discovery_if_needed()
        job = (await db_session.execute(select(Job))).scalar_one()
        job.status = "failed"
        await db_session.commit()
        assert await JobScheduler(db_session, settings).bootstrap_discovery_if_needed() is None
        assert await job_count(db_session, "discovery") == 1

    @pytest.mark.asyncio
    async def test_existing_run_suppresses_bootstrap(self, db_session: AsyncSession) -> None:
        db_session.add(Run(type="discovery", status="complete"))
        await db_session.commit()
        settings = make_settings(BOOTSTRAP_DISCOVERY=True)
        assert await JobScheduler(db_session, settings).bootstrap_discovery_if_needed() is None
        assert await job_count(db_session) == 0
