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

BEST-EFFORT OVERLAP REDUCTION, NOT MUTUAL EXCLUSION (#21): the lock
session sits idle in transaction for the WHOLE cycle (the
``pg_try_advisory_lock`` SELECT opens a transaction that never commits
until block exit), so ``idle_in_transaction_session_timeout``, a DB
restart, or a network blip can free the lock while the cycle is still
running — the next tick then overlaps it. Degradation is safe (the
workers are SKIP LOCKED and idempotent; overlap is the pre-lock
behavior), but it is NOT prevented. The one detectable symptom is
``pg_advisory_unlock`` returning false at cycle end — logged as a
WARNING naming the worker (the lost-mid-cycle signal).

TRANSACTION-POOLING CAVEAT: session-level advisory locks BREAK behind
pgbouncer in transaction-pooling mode — the lock and the unlock land on
different backends, so the guard silently stops guarding. Today's
cPanel deployment uses direct connections; the hosting exit (#11) must
re-check this before fronting the app with a transaction pooler.
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

    The guard is best-effort (see the module docstring): the unlock in
    the ``finally`` is wrapped so that

    - an unlock FAILURE (e.g. the cycle raised BECAUSE the DB died, so
      the unlock dies too) is logged and suppressed — the cycle's own
      exception always surfaces, never the unlock's (#21); Postgres
      frees the lock when the dead session's connection closes anyway;
    - an unlock returning false means the lock was NOT held at cycle
      end — it was lost mid-cycle (idle-in-transaction timeout, DB
      restart, network reset) and a concurrent tick may have overlapped
      this cycle. Logged as a WARNING naming the worker.
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
                try:
                    released = bool(
                        (
                            await session.execute(
                                text("SELECT pg_advisory_unlock(:ns, :key)"),
                                {"ns": CRON_LOCK_NAMESPACE, "key": key},
                            )
                        ).scalar_one()
                    )
                except Exception:
                    # Never mask the guarded cycle's own exception with
                    # an unlock failure (the usual cause is the SAME dead
                    # DB that failed the cycle). Postgres releases the
                    # lock when this session's connection closes.
                    logger.exception(
                        "%s: advisory unlock (%s, %s) failed — suppressed so the "
                        "cycle's own outcome is not masked; Postgres frees the "
                        "lock when the session's connection closes",
                        worker,
                        CRON_LOCK_NAMESPACE,
                        key,
                    )
                else:
                    if not released:
                        logger.warning(
                            "%s: advisory lock (%s, %s) was no longer held at cycle "
                            "end — lost mid-cycle (idle-in-transaction timeout, DB "
                            "restart, or network reset); a concurrent tick may have "
                            "overlapped this cycle",
                            worker,
                            CRON_LOCK_NAMESPACE,
                            key,
                        )
