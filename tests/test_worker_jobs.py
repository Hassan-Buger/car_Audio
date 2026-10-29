"""Tests for job execution: run linkage, outcome mapping, resume and lock contention."""

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.lock import DIGISOUND_RUN_LOCK_KEY
from app.db.models import Job, Run
from app.jobs.queue import JobQueue
from app.scraper.client import ScraperClient
from app.worker import execute_job
from tests.support import BASE, ROOT, Item, build_settings, detail_html, listing_html

A = Item("alpha-amp", "ALPHA 1")
B = Item("beta-sub", "BETA/2", "200,00 €")


def mock_two(total: int = 2) -> None:
    respx.get(ROOT).mock(return_value=httpx.Response(200, text=listing_html([A, B], total=total)))
    for it in (A, B):
        respx.get(f"{BASE}{it.url_path}").mock(
            return_value=httpx.Response(200, text=detail_html(it))
        )


async def claim(session: AsyncSession, job_type: str) -> tuple[JobQueue, Job]:
    queue = JobQueue(session, worker_id="test-worker")
    await queue.enqueue_job(job_type)
    job = await queue.claim_next_job(60)
    assert job is not None
    return queue, job


class TestExecuteJob:
    @pytest.mark.asyncio
    @respx.mock
    async def test_complete_run_completes_job_and_links_run(self, db_session: AsyncSession) -> None:
        mock_two()
        settings = build_settings()
        queue, job = await claim(db_session, "discovery")
        outcome = await execute_job(db_session, queue, job, settings, ScraperClient(settings))
        assert outcome == "completed"
        finished = await queue.get_job(job.id)
        assert finished is not None and finished.status == "completed"
        run = (await db_session.execute(select(Run))).scalar_one()
        assert finished.run_id == run.id  # job is linked to its run (needed for resume)
        assert finished.result_summary is not None
        assert finished.result_summary["run_status"] == "complete"
        assert finished.result_summary["total_products"] == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_incomplete_run_fails_job_with_reason(self, db_session: AsyncSession) -> None:
        mock_two(total=9)  # source claims 9, only 2 present
        settings = build_settings()
        queue, job = await claim(db_session, "discovery")
        outcome = await execute_job(db_session, queue, job, settings, ScraperClient(settings))
        assert outcome == "failed"
        finished = await queue.get_job(job.id)
        assert finished is not None and finished.status == "failed"
        assert "Count discrepancy" in (finished.error or "")
        assert finished.result_summary is not None
        assert finished.result_summary["run_status"] == "incomplete"

    @pytest.mark.asyncio
    @respx.mock
    async def test_blocked_source_fails_job(self, db_session: AsyncSession) -> None:
        respx.get(ROOT).mock(return_value=httpx.Response(403, text="Forbidden"))
        settings = build_settings()
        queue, job = await claim(db_session, "discovery")
        outcome = await execute_job(db_session, queue, job, settings, ScraperClient(settings))
        assert outcome == "failed"
        finished = await queue.get_job(job.id)
        assert finished is not None and "access restricted" in (finished.error or "").lower()

    @pytest.mark.asyncio
    async def test_stock_job_skipped_when_catalog_empty(self, db_session: AsyncSession) -> None:
        queue, job = await claim(db_session, "stock")
        outcome = await execute_job(db_session, queue, job, build_settings())
        assert outcome == "skipped"
        finished = await queue.get_job(job.id)
        assert finished is not None and finished.status == "completed"
        assert "empty" in str(finished.result_summary)
        assert (await db_session.execute(select(Run))).first() is None  # no run recorded

    @pytest.mark.asyncio
    @respx.mock
    async def test_stock_job_records_a_stock_run(self, db_session: AsyncSession) -> None:
        mock_two()
        settings = build_settings()
        q1, j1 = await claim(db_session, "discovery")
        await execute_job(db_session, q1, j1, settings, ScraperClient(settings))
        q2, j2 = await claim(db_session, "stock")
        outcome = await execute_job(db_session, q2, j2, settings, ScraperClient(settings))
        assert outcome == "completed"
        types = (
            (await db_session.execute(select(Run.type).order_by(Run.started_at))).scalars().all()
        )
        assert types == ["discovery", "stock"]

    @pytest.mark.asyncio
    async def test_unknown_job_type_fails(self, db_session: AsyncSession) -> None:
        queue, job = await claim(db_session, "mystery")
        assert await execute_job(db_session, queue, job, build_settings()) == "failed"

    @pytest.mark.asyncio
    @respx.mock
    async def test_lock_contention_requeues_job_without_losing_it(
        self, db_session: AsyncSession, test_engine: AsyncEngine
    ) -> None:
        mock_two()
        settings = build_settings()
        queue, job = await claim(db_session, "discovery")
        async with test_engine.connect() as holder:
            await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": DIGISOUND_RUN_LOCK_KEY})
            outcome = await execute_job(db_session, queue, job, settings, ScraperClient(settings))
            await holder.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": DIGISOUND_RUN_LOCK_KEY}
            )
        assert outcome == "requeued"
        again = await queue.get_job(job.id)
        assert again is not None and again.status == "queued" and again.lease_owner is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_graceful_stop_requeues_job_and_restart_resumes_same_run(
        self, db_session: AsyncSession
    ) -> None:
        respx.get(ROOT, params={"start": 1}).mock(
            return_value=httpx.Response(200, text=listing_html([B], total=2))
        )
        respx.get(ROOT).mock(
            return_value=httpx.Response(200, text=listing_html([A], total=2, next_start=1))
        )
        for it in (A, B):
            respx.get(f"{BASE}{it.url_path}").mock(
                return_value=httpx.Response(200, text=detail_html(it))
            )
        settings = build_settings()

        queue, job = await claim(db_session, "discovery")
        outcome = await execute_job(
            db_session, queue, job, settings, ScraperClient(settings), should_stop=lambda: True
        )
        assert outcome == "requeued"
        interrupted = (await db_session.execute(select(Run))).scalar_one()
        assert interrupted.status == "running"

        # "Restarted worker" claims the very same job; it resumes the very same run
        job2 = await queue.claim_next_job(60)
        assert job2 is not None and job2.id == job.id and job2.run_id == interrupted.id
        outcome2 = await execute_job(db_session, queue, job2, settings, ScraperClient(settings))
        assert outcome2 == "completed"
        runs = (await db_session.execute(select(Run))).scalars().all()
        assert len(runs) == 1 and runs[0].status == "complete"
        assert runs[0].total_products_discovered == 2 and runs[0].new_products == 2
