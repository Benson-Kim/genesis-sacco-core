"""Idempotency-key retention purge worker (P13.17c / DSA-3).

Deployment counterpart of the outbox retention purge (the
export_worker composition precedent): a long-running loop that walks
the active tenants and reclaims each tenant's expired idempotency
keys. The purge itself lives in
genesis.application.idempotency_purge.purge_expired_idempotency_keys
(shared batch runner, FOR UPDATE SKIP LOCKED, injected session scope);
this module only composes sessions. Request handlers never import this
module (import-linter contract).

Tenant discovery (honest trade-off, recorded): the walk uses the 0003
active_tenant_ids() registry — an ALL-active-tenant sweep, not a
purgeable-tenant registry like DSA-6's outbox_purgeable_tenant_ids()
(0024). A new SECURITY DEFINER registry would need its genesis_app
EXECUTE grant added to the CI app-role bootstrap, which lives in
.gitlab-ci.yml — untouchable in this MR by standing rule. The sweep
runs on the slow purge cadence (hourly), and a tenant with nothing
expired costs one indexed zero-row DELETE: a lock-free no-op. If tenant
counts ever make this measurable, a follow-up migration adds the
registry alongside its bootstrap grant.

Expiry semantics NEVER depend on this worker running: the
``expires_at > now()`` fence in api/idempotency.py holds before any
purge (FM3).
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from genesis.application.idempotency_purge import purge_expired_idempotency_keys
from genesis.infrastructure.outbox_worker import list_active_tenants
from genesis.infrastructure.tenancy import tenant_session

logger = logging.getLogger("genesis.infrastructure.idempotency")

#: Hourly, like the outbox retention purge (0024 rationale): the
#: retention window is measured in hours/days, so an hourly reclaim is
#: ample and the purge never becomes a hot loop.
PURGE_INTERVAL_SECONDS = 3600.0


async def run_purge_cycle(factory: async_sessionmaker[AsyncSession]) -> int:
    """One purge pass over every active tenant; returns rows purged.

    Per-tenant error isolation (the !37 pattern): a failed tenant is
    logged with its stack trace and the cycle keeps walking — its
    expired rows are simply reclaimed on a later cycle (the purge is
    idempotent by side-effect counts). Never a silent pass (gate 1.2).
    """
    purged = 0
    for tenant_id in await list_active_tenants(factory):
        try:
            purged += await purge_expired_idempotency_keys(
                partial(tenant_session, factory, tenant_id), tenant_id
            )
        except Exception:
            logger.exception("idempotency purge errored for tenant %s", tenant_id)
    return purged


async def run_worker(
    factory: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: float = PURGE_INTERVAL_SECONDS,
    stop: asyncio.Event | None = None,
) -> None:
    """Long-running purge loop; deployed as the worker process."""
    while stop is None or not stop.is_set():
        try:
            await run_purge_cycle(factory)
        except Exception:
            logger.exception("idempotency purge cycle failed")
        await asyncio.sleep(interval_seconds)
