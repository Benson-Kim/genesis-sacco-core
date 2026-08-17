"""Nightly dormancy cycle: runs the P13.13 job for every active tenant.

Deployment counterpart of the outbox/export workers: walks the active
tenants through the SECURITY DEFINER registry (migration 0003 — the
established due-tenant discovery; never a cross-tenant scan) and runs
the dormancy job once per tenant with that tenant's own session scope.
The job itself lives in genesis.application.dormancy; this module only
composes sessions (the export_worker injection pattern, gate 1.1).

Fail-closed per tenant (P13.13 FM8): a tenant whose
dormancy_period_months is missing or corrupt is REFUSED LOUDLY — the
job raises ConflictError before scanning anything, which is logged
here as the 409-class refusal with zero transitions for that tenant —
and the cycle moves on, so one unconfigured tenant never blocks the
others. No silent default period exists anywhere.

Per-tenant isolation for UNEXPECTED failures (!32 review finding R1):
any OTHER exception from one tenant's run (transient DB error, pool
exhaustion) is logged with its stack trace and counted on the separate
'errored' counter — distinct from 'refused' so operators can alert on
each class independently — and the cycle keeps walking the remaining
tenants. This is a handled-and-logged category, never a silent pass
(gate 1.2): the failed tenant's members are simply retried by the next
nightly cycle (the job is idempotent by construction — P13.13 FM4).
run_worker's outer catch-all remains the last resort for failures of
the cycle machinery itself (registry discovery, session factory).

Request handlers never import this module (import-linter contract):
the API drives the same application service directly per tenant.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from genesis.application.dormancy import run_dormancy_for_tenant
from genesis.errors import ConflictError
from genesis.infrastructure.outbox_worker import list_active_tenants
from genesis.infrastructure.tenancy import tenant_session

logger = logging.getLogger("genesis.infrastructure.dormancy")


@dataclass(frozen=True)
class DormancyCycleSummary:
    tenants: int
    transitioned: int
    #: Tenants refused fail-closed (missing/corrupt config): counted so
    #: operators can alert on a non-zero value; the tenant ids are in
    #: the log records (no member data — gate 1.6).
    refused: int
    #: Tenants whose run raised an UNEXPECTED error (!32 R1): logged
    #: with the stack trace and skipped for this cycle, never aborting
    #: the remaining tenants. Distinct from `refused` (fail-closed
    #: config) so operators can alert on both classes independently.
    errored: int


async def run_dormancy_cycle(
    factory: async_sessionmaker[AsyncSession],
) -> DormancyCycleSummary:
    """One pass over every active tenant (the export_worker shape)."""
    as_of = datetime.now(UTC).date()
    tenants = 0
    transitioned = 0
    refused = 0
    errored = 0
    for tenant_id in await list_active_tenants(factory):
        tenants += 1
        try:
            result = await run_dormancy_for_tenant(
                partial(tenant_session, factory, tenant_id),
                tenant_id,
                as_of=as_of,
            )
        except ConflictError:
            # FM8: the 409-class loud refusal — zero transitions for
            # this tenant, never a silent default; the cycle continues
            # so other tenants still run. Fail-closed semantics are
            # unchanged by the R1 broadening below.
            refused += 1
            logger.exception("dormancy run refused for tenant %s (fail-closed)", tenant_id)
            continue
        except Exception:
            # !32 review finding R1: per-tenant isolation for
            # UNEXPECTED failures too — logged with the stack trace,
            # counted separately from `refused`, and the cycle keeps
            # walking so one broken tenant never aborts the night's
            # run for everyone else. The idempotent job retries this
            # tenant's members on the next cycle.
            errored += 1
            logger.exception("dormancy run errored for tenant %s", tenant_id)
            continue
        transitioned += result.transitioned
    return DormancyCycleSummary(
        tenants=tenants, transitioned=transitioned, refused=refused, errored=errored
    )


async def run_worker(
    factory: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: float = 24 * 60 * 60,
    stop: asyncio.Event | None = None,
) -> None:
    """Long-running nightly loop; deployed as the worker process."""
    while stop is None or not stop.is_set():
        try:
            await run_dormancy_cycle(factory)
        except Exception:
            logger.exception("dormancy cycle failed")
        await asyncio.sleep(interval_seconds)
