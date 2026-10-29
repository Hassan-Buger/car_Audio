"""Delta feed safety with overlapping transactions (writers must not be skippable by readers)."""

import asyncio
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.deps import get_db
from app.db.models import Product
from app.db.repo import Repository
from app.main import app


def new_product(n: int = 1) -> Product:
    return Product(
        source="digisound",
        source_product_id=f"conc-{n}",
        sku=f"CONC-{n}",
        title="Concurrency Product",
        canonical_url=f"https://www.digisound.ee/products/car-stereo/conc-{n}",
        specifications=[],
        images=[],
        category_paths=[],
        source_category_ids=[],
        content_hash="h",
        commerce_hash="c",
    )


@pytest.fixture
async def factory(
    test_engine: AsyncEngine, db_session: AsyncSession
) -> async_sessionmaker[AsyncSession]:
    # db_session fixture guarantees tables are clean
    return async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def reader(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    async def _get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def seed_product(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as s:
        p = new_product()
        s.add(p)
        await s.commit()
        return p.id


class TestDeltaOverlappingTransactions:
    @pytest.mark.asyncio
    async def test_reader_waits_for_in_flight_writer_instead_of_skipping_it(
        self, factory: async_sessionmaker[AsyncSession], reader: AsyncClient
    ) -> None:
        pid = await seed_product(factory)

        async with factory() as writer:
            ch = await Repository(writer).record_change(pid, "created", ["all"], None, {"k": 1})
            seq = ch.seq  # allocated but NOT committed
            read_task = asyncio.create_task(reader.get("/api/products/delta"))
            await asyncio.sleep(0.4)
            assert not read_task.done(), "reader must wait for the in-flight writer"
            await writer.commit()

        resp = await asyncio.wait_for(read_task, timeout=5)
        body = resp.json()
        assert [i["seq"] for i in body["items"]] == [seq]
        assert body["window_until"] == seq

    @pytest.mark.asyncio
    async def test_rolled_back_writer_never_shows_up_and_does_not_block_forever(
        self, factory: async_sessionmaker[AsyncSession], reader: AsyncClient
    ) -> None:
        pid = await seed_product(factory)
        async with factory() as writer:
            await Repository(writer).record_change(pid, "created", ["all"], None, {})
            read_task = asyncio.create_task(reader.get("/api/products/delta"))
            await asyncio.sleep(0.2)
            await writer.rollback()
        resp = await asyncio.wait_for(read_task, timeout=5)
        assert resp.json()["items"] == []

    @pytest.mark.asyncio
    async def test_two_overlapping_writers_commit_in_sequence_order(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        pid = await seed_product(factory)
        order: list[str] = []

        async with factory() as w1, factory() as w2:
            c1 = await Repository(w1).record_change(pid, "created", ["all"], None, {})

            async def second() -> int:
                c2 = await Repository(w2).record_change(pid, "commerce_updated", ["p"], None, {})
                order.append("w2-inserted")
                await w2.commit()
                order.append("w2-committed")
                return c2.seq

            t2 = asyncio.create_task(second())
            await asyncio.sleep(0.4)
            assert order == [], "second writer must block while the first is in flight"
            order.append("w1-commit")
            await w1.commit()
            seq2 = await asyncio.wait_for(t2, timeout=5)

        assert order == ["w1-commit", "w2-inserted", "w2-committed"]
        assert seq2 > c1.seq  # sequence order == commit order, so no reader can skip a lower seq
