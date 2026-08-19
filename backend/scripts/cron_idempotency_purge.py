"""One-shot idempotency-key retention purge, for cPanel Cron Jobs.

Shared-hosting counterpart of `idempotency_worker.run_worker()` (see
cron_outbox.py for why a persistent loop isn't used here). Hourly is
ample — expiry itself never depends on this running (see the module
docstring on idempotency_worker.py); a missed cycle just means expired
rows linger a bit longer, never a correctness issue.

Example cron line (hourly):
  7 * * * * /home/USER/virtualenv/api/3.12/bin/python \
    /home/USER/api/scripts/cron_idempotency_purge.py >> /home/USER/logs/cron_idempotency.log 2>&1
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from genesis.infrastructure.cron_lock import CRON_LOCK_IDEMPOTENCY_PURGE, try_cron_lock
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.idempotency_worker import run_purge_cycle
from genesis.infrastructure.worker_heartbeat import (
    record_worker_lock_skip,
    record_worker_success,
)
from genesis.logging import configure_logging, correlation_id_var, new_run_id
from genesis.settings import get_settings

#: Structured JSON logs (issue #4), same as the API: the ts field still
#: answers "did this cycle actually fire, and when?"; the per-cycle run
#: id set in main() correlates every record one cycle emits.
configure_logging()
logger = logging.getLogger(Path(__file__).stem)


async def main() -> int:
    correlation_id_var.set(new_run_id())
    settings = get_settings()
    if not settings.database_url:
        logger.error("DATABASE_URL is not configured")
        return 1
    factory = get_sessionmaker(settings.database_url)
    # Overlap guard: skip-and-log when a previous purge still holds the
    # lock (genesis.infrastructure.cron_lock).
    async with try_cron_lock(
        factory, CRON_LOCK_IDEMPOTENCY_PURGE, worker="idempotency_purge"
    ) as acquired:
        if not acquired:
            skips = await record_worker_lock_skip(factory, "idempotency_purge")
            logger.info(
                "idempotency: skipped — previous cycle still running (consecutive skips: %s)",
                skips,
            )
            return 0
        purged = await run_purge_cycle(factory)
        await record_worker_success(factory, "idempotency_purge")
    logger.info(f"idempotency: purged={purged}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
