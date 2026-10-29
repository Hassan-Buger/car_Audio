"""PostgreSQL advisory locks.

Two kinds of locks are used:

* ``run_advisory_lock`` - a *session-level* lock that guarantees a single active
  discovery/stock run. It is taken on a **dedicated connection that is held open for the
  whole run**. Taking a session-level lock on a pooled ``AsyncSession`` is unsafe because the
  session hands its connection back to the pool on every commit, which would leave the lock
  stranded on a pooled connection and make the later ``pg_advisory_unlock`` run on a different
  connection.
* ``lock_change_writers`` / ``lock_change_readers`` - *transaction-level* locks that serialise
  writers of the ``product_changes`` feed so that sequence numbers become visible in order
  (see the delta endpoint).
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

logger = logging.getLogger(__name__)

# Fixed 64-bit integer advisory lock keys for Digisound
DIGISOUND_RUN_LOCK_KEY = 739102847291847192
DIGISOUND_CHANGE_FEED_LOCK_KEY = 739102847291847193


@asynccontextmanager
async def run_advisory_lock(
    bind: AsyncEngine, lock_key: int = DIGISOUND_RUN_LOCK_KEY
) -> AsyncGenerator[bool, None]:
    """Hold a session-level advisory lock on a dedicated connection for the context duration.

    Yields True when the lock was acquired, False when another process holds it.
    """
    autocommit_engine = bind.execution_options(isolation_level="AUTOCOMMIT")
    async with autocommit_engine.connect() as conn:
        acquired = bool(
            (
                await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key})
            ).scalar()
        )
        if acquired:
            logger.info("Acquired PostgreSQL advisory run lock key=%s", lock_key)
        else:
            logger.warning("Advisory run lock key=%s is held by another process", lock_key)
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    released = (
                        await conn.execute(
                            text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
                        )
                    ).scalar()
                    logger.info(
                        "Released advisory run lock key=%s (success=%s)", lock_key, released
                    )
                except Exception:  # pragma: no cover - connection lost; server frees the lock
                    logger.exception("Could not release advisory lock cleanly; invalidating")
                    await conn.invalidate()


async def lock_change_writers(session: AsyncSession) -> None:
    """Serialise writers of product_changes until the current transaction ends."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": DIGISOUND_CHANGE_FEED_LOCK_KEY}
    )


async def lock_change_readers(session: AsyncSession) -> None:
    """Wait for in-flight change writers to finish (shared lock, held until transaction end).

    After this returns, every sequence number lower than MAX(seq) is either committed or
    rolled back, so MAX(seq) is a safe upper bound for a delta window.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": DIGISOUND_CHANGE_FEED_LOCK_KEY}
    )
