"""Pytest fixtures for database and async test environment."""

import os
from collections.abc import AsyncGenerator

TEST_DB_URL = os.environ.get(
    "DATABASE_URL_TEST",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/digisound_test",
)
# The application treats DATABASE_URL as "configured" only when it is set explicitly, so the
# test-suite points it at the dedicated test database before the app is imported.
os.environ.setdefault("DATABASE_URL", TEST_DB_URL)

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Ensure all models are loaded
import app.db.models  # noqa: F401, E402
from app.db.base import Base  # noqa: E402


@pytest_asyncio.fixture
async def test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Provide isolated test database engine."""
    engine = create_async_engine(TEST_DB_URL, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Provide isolated transactional database session per test with clean tables."""
    async with test_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())

    session_factory = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session
        await session.rollback()
