"""PostgreSQL advisory lock helper to ensure single active run."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Fixed 64-bit integer advisory lock key for Digisound runs
DIGISOUND_RUN_LOCK_KEY = 739102847291847192


class AdvisoryLock:
    """Manages PostgreSQL session-level advisory locks."""

    def __init__(self, session: AsyncSession, lock_key: int = DIGISOUND_RUN_LOCK_KEY) -> None:
        self.session = session
        self.lock_key = lock_key
        self.acquired = False

    async def try_acquire(self) -> bool:
        """Attempt to acquire the advisory lock non-blockingly."""
        query = text("SELECT pg_try_advisory_lock(:key)")
        result = await self.session.execute(query, {"key": self.lock_key})
        row = result.scalar()
        self.acquired = bool(row)
        if self.acquired:
            logger.info("Acquired PostgreSQL advisory lock key=%s", self.lock_key)
        else:
            logger.warning("Failed to acquire PostgreSQL advisory lock key=%s", self.lock_key)
        return self.acquired

    async def release(self) -> bool:
        """Release the advisory lock if previously acquired."""
        if not self.acquired:
            return False
        query = text("SELECT pg_advisory_unlock(:key)")
        result = await self.session.execute(query, {"key": self.lock_key})
        row = result.scalar()
        self.acquired = False
        logger.info("Released PostgreSQL advisory lock key=%s (success=%s)", self.lock_key, row)
        return bool(row)


@asynccontextmanager
async def run_advisory_lock(
    session: AsyncSession, lock_key: int = DIGISOUND_RUN_LOCK_KEY
) -> AsyncGenerator[bool, None]:
    """Context manager for acquiring and releasing advisory run lock."""
    lock = AdvisoryLock(session, lock_key)
    acquired = await lock.try_acquire()
    try:
        yield acquired
    finally:
        if acquired:
            await lock.release()
