"""One-shot outbox dispatch + retention purge, for cPanel Cron Jobs.

Shared hosting under Passenger cannot keep a long-running
`outbox_worker.run_worker()` loop alive, so this invokes exactly the
same per-cycle functions the real worker uses
(`run_dispatch_cycle`/`run_purge_cycle`) once and exits — schedule it
in cPanel's Cron Jobs UI instead (see
docs/technical/mochahost-deployment.md). OTP events are routed onto
the OTP delivery port (genesis.infrastructure.otp_delivery — a real
SMS/email gateway drops in at `default_otp_delivery()`, not here);
every other event goes to the same `StubProvider` placeholder as
before (genesis.infrastructure.providers).

Example cron line (every 2 minutes, using the venv cPanel created for
the Python app — adjust the app root path):
  */2 * * * * /home/USER/virtualenv/api/3.12/bin/python \
    /home/USER/api/scripts/cron_outbox.py >> /home/USER/logs/cron_outbox.log 2>&1
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from genesis.infrastructure.cron_lock import CRON_LOCK_OUTBOX, try_cron_lock
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.otp_delivery import OtpRoutingProvider, default_otp_delivery
from genesis.infrastructure.outbox_worker import run_dispatch_cycle, run_purge_cycle
from genesis.infrastructure.providers import StubProvider
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
    provider = OtpRoutingProvider(default_otp_delivery(), StubProvider(channel="stub"))
    # Overlap guard: a slow cycle must never run concurrently with the
    # next cron tick's cycle — skip-and-log when the lock is held
    # (genesis.infrastructure.cron_lock).
    async with try_cron_lock(factory, CRON_LOCK_OUTBOX, worker="outbox") as acquired:
        if not acquired:
            logger.info("outbox: skipped — previous cycle still running")
            return 0
        delivered = await run_dispatch_cycle(factory, provider)
        purged = await run_purge_cycle(factory)
    logger.info(f"outbox: delivered={delivered} purged={purged}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
