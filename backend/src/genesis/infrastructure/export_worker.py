"""Export rendering worker: drains export queues per tenant.

Deployment counterpart of the outbox dispatcher: a long-running loop
that walks the active tenants (SECURITY DEFINER registry, 0003) and
renders each tenant's pending export jobs. Rendering itself lives in
genesis.application.exports.run_export_job — one REPEATABLE READ
transaction per job (blockers h, i); this module only composes
sessions, matching the batch-runner injection pattern (reuse-first).

Request handlers never import this module (import-linter contract):
the API enqueues jobs and, for operations, drives the same application
service directly.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from functools import partial

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from genesis.application.exports import ExportCycleSummary, run_pending_exports
from genesis.infrastructure.outbox_worker import list_active_tenants
from genesis.infrastructure.tenancy import tenant_snapshot_session

logger = logging.getLogger("genesis.infrastructure.exports")


async def run_export_cycle(
    factory: async_sessionmaker[AsyncSession],
) -> ExportCycleSummary:
    """One pass over every active tenant's export queue."""
    completed = 0
    failed = 0
    for tenant_id in await list_active_tenants(factory):
        summary = await run_tenant_exports(factory, tenant_id)
        completed += summary.completed
        failed += summary.failed
    return ExportCycleSummary(completed=completed, failed=failed)


async def run_tenant_exports(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> ExportCycleSummary:
    return await run_pending_exports(
        partial(tenant_snapshot_session, factory, tenant_id), tenant_id
    )


async def run_worker(
    factory: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: float = 5.0,
    stop: asyncio.Event | None = None,
) -> None:
    """Long-running export renderer loop; deployed as the worker process."""
    while stop is None or not stop.is_set():
        try:
            await run_export_cycle(factory)
        except Exception:
            logger.exception("export render cycle failed")
        await asyncio.sleep(interval_seconds)
