"""Per-worker Postgres advisory locks for the one-shot cron entrypoints.

The cPanel Cron Jobs deployment (docs/technical/mochahost-deployment.md
§3a) fires each `backend/scripts/cron_*.py` on a fixed schedule with no
knowledge of whether the previous invocation finished. A slow cycle —
a large export, a backlogged outbox — would otherwise run CONCURRENTLY
with the next tick's cycle. The workers are individually crash-safe
(SKIP LOCKED claims, idempotent providers), but overlap still doubles
load and turns "slow" into "slower" (scalability). This module gives
every one-shot a session-level ``pg_try_advisory_lock`` guard: the
second invocation observes the held lock, logs a skip, and exits 0 —
never queues behind the first (a queued lock would defeat the point:
ticks would pile up behind a stuck cycle).

Why SESSION-level (not ``pg_try_advisory_xact_lock``): the guard must
span the WHOLE cycle, which itself opens and commits many transactions
through its own sessions; the lock session here stays open (its
transaction never commits) for the duration of the ``try_cron_lock``
block, is explicitly unlocked on exit, and — the crash story — is
released by Postgres automatically when the connection dies with the
process, so a SIGKILLed cron run can never wedge the schedule.

Keys are two-int form: a fixed namespace classid (so these locks can
never collide with any future advisory-lock user in this database) and
a distinct per-worker objid. Advisory locks are cluster-wide per
database, deliberately NOT tenant-scoped: the guarded resource is the
worker PROCESS (which itself walks tenants), not tenant data — RLS and
tenant isolation are untouched.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("genesis.infrastructure.cron_lock")

#: Fixed classid namespacing every Genesis cron-worker lock (arbitrary
#: but stable; changing it would let an old and a new deploy overlap).
CRON_LOCK_NAMESPACE = 815301

#: Distinct per-worker objids — one lock per WORKER, so e.g. a slow
#: export cycle never blocks (or is blocked by) the dormancy cycle.
CRON_LOCK_OUTBOX = 1
CRON_LOCK_IDEMPOTENCY_PURGE = 2
CRON_LOCK_EXPORT = 3
CRON_LOCK_DORMANCY = 4

#: Registry of every worker key, asserted distinct by the test suite —
#: adding a new one-shot with a colliding objid is a red pipeline.
CRON_LOCK_KEYS: dict[str, int] = {
    "outbox": CRON_LOCK_OUTBOX,
    "idempotency_purge": CRON_LOCK_IDEMPOTENCY_PURGE,
    "export": CRON_LOCK_EXPORT,
    "dormancy": CRON_LOCK_DORMANCY,
}


@asynccontextmanager
async def try_cron_lock(
    factory: async_sessionmaker[AsyncSession], key: int, *, worker: str
) -> AsyncIterator[bool]:
    """Try to take the per-worker advisory lock; yield whether it was won.

    Non-blocking by design (``pg_try_advisory_lock``): a False yield
    means another invocation of the SAME worker is still running — the
    caller logs and exits cleanly instead of overlapping it. The lock
    session is held open across the block (the guarded cycle runs its
    own sessions/transactions from the same factory) and released in
    the ``finally`` — or by Postgres itself if the process dies.
    """
    async with factory() as session:
        acquired = bool(
            (
                await session.execute(
                    text("SELECT pg_try_advisory_lock(:ns, :key)"),
                    {"ns": CRON_LOCK_NAMESPACE, "key": key},
                )
            ).scalar_one()
        )
        if not acquired:
            logger.info(
                "%s: advisory lock (%s, %s) is held — a previous cycle is "
                "still running; skipping this tick",
                worker,
                CRON_LOCK_NAMESPACE,
                key,
            )
        try:
            yield acquired
        finally:
            if acquired:
                await session.execute(
                    text("SELECT pg_advisory_unlock(:ns, :key)"),
                    {"ns": CRON_LOCK_NAMESPACE, "key": key},
                )
