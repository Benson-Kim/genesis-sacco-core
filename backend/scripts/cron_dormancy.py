"""One-shot nightly dormancy cycle, for cPanel Cron Jobs.

Shared-hosting counterpart of `dormancy_worker.run_worker()` (see
cron_outbox.py for why a persistent loop isn't used here). Run once a
day, off-peak.

Example cron line (02:30 daily):
  30 2 * * * /home/USER/virtualenv/api/3.12/bin/python \
    /home/USER/api/scripts/cron_dormancy.py >> /home/USER/logs/cron_dormancy.log 2>&1
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from genesis.infrastructure.cron_lock import CRON_LOCK_DORMANCY, try_cron_lock
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.dormancy_worker import run_dormancy_cycle
from genesis.settings import get_settings

#: Timestamped so a cron log answers "did this cycle actually
#: fire, and when?" — bare stdout prints cannot.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(Path(__file__).stem)


async def main() -> int:
    settings = get_settings()
    if not settings.database_url:
        logger.error("DATABASE_URL is not configured")
        return 1
    factory = get_sessionmaker(settings.database_url)
    # Overlap guard: skip-and-log when yesterday's cycle is somehow
    # still running (genesis.infrastructure.cron_lock).
    async with try_cron_lock(factory, CRON_LOCK_DORMANCY, worker="dormancy") as acquired:
        if not acquired:
            logger.info("dormancy: skipped — previous cycle still running")
            return 0
        summary = await run_dormancy_cycle(factory)
    logger.info(
        f"dormancy: tenants={summary.tenants} transitioned={summary.transitioned} "
        f"refused={summary.refused} errored={summary.errored}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
