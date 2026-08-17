"""One-shot export-queue drain, for cPanel Cron Jobs.

Shared-hosting counterpart of `export_worker.run_worker()` (see
cron_outbox.py for why a persistent loop isn't used here).

Example cron line (every 2 minutes):
  */2 * * * * /home/USER/virtualenv/api/3.12/bin/python \
    /home/USER/api/scripts/cron_export.py >> /home/USER/logs/cron_export.log 2>&1
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.export_worker import run_export_cycle
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
    summary = await run_export_cycle(factory)
    logger.info(f"exports: completed={summary.completed} failed={summary.failed}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
