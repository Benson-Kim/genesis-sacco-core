"""Worker heartbeat writes for the cron one-shots (issue #4).

The four ``backend/scripts/cron_*.py`` entrypoints call exactly one of
these at cycle end:

* ``record_worker_success`` — stamps ``last_success_at`` and RESETS
  the consecutive-skip counter. A stale timestamp is the dead-man
  signal (worker silently not firing); alerting on it needs no log
  aggregation, only the /ops/metrics scrape.
* ``record_worker_lock_skip`` — increments ``consecutive_lock_skips``
  and returns the new count, turning the cron_lock.py skip LOG LINE
  into a COUNTABLE series: one skip is benign backpressure,
  consecutive skips mean a wedged or pathologically slow cycle (the
  !2 review's paging signal).

BEST-EFFORT BY DESIGN: a heartbeat write failure is logged loudly and
suppressed — it must never turn an otherwise-successful cycle into a
nonzero exit, and it must never mask the cycle's own exception. The
failure mode is still detectable: a success-write that keeps failing
leaves ``last_success_at`` stale, which is precisely the condition
the heartbeat exists to page on.

The table is GLOBAL (no tenant_id, no RLS — the cron_lock posture:
the row describes the worker process, not tenant data), so a plain
session from the factory writes it without any tenant GUC.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("genesis.infrastructure.worker_heartbeat")


async def record_worker_success(factory: async_sessionmaker[AsyncSession], worker: str) -> None:
    """Stamp a successful cycle and reset the consecutive-skip counter."""
    try:
        async with factory() as session:
            await session.execute(
                text(
                    "INSERT INTO worker_heartbeats "
                    "(worker, last_success_at, consecutive_lock_skips, updated_at) "
                    "VALUES (:worker, now(), 0, now()) "
                    "ON CONFLICT (worker) DO UPDATE SET "
                    "last_success_at = now(), consecutive_lock_skips = 0, "
                    "updated_at = now()"
                ),
                {"worker": worker},
            )
            await session.commit()
    except Exception:
        logger.exception(
            "%s: heartbeat success-write failed — suppressed so the cycle's "
            "own outcome is not masked; a persistently failing write leaves "
            "last_success_at stale, which is the pageable condition itself",
            worker,
        )


async def record_worker_lock_skip(
    factory: async_sessionmaker[AsyncSession], worker: str
) -> int | None:
    """Count a lock-skip; return the new consecutive total (None on failure)."""
    try:
        async with factory() as session:
            skips = (
                await session.execute(
                    text(
                        "INSERT INTO worker_heartbeats "
                        "(worker, consecutive_lock_skips, updated_at) "
                        "VALUES (:worker, 1, now()) "
                        "ON CONFLICT (worker) DO UPDATE SET "
                        "consecutive_lock_skips = "
                        "worker_heartbeats.consecutive_lock_skips + 1, "
                        "updated_at = now() "
                        "RETURNING consecutive_lock_skips"
                    ),
                    {"worker": worker},
                )
            ).scalar_one()
            await session.commit()
        return int(skips)
    except Exception:
        logger.exception(
            "%s: heartbeat skip-write failed — suppressed; the skip is still "
            "visible in this cycle's log line",
            worker,
        )
        return None
