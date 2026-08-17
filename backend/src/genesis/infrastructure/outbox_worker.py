"""Outbox dispatcher: retry with backoff + jitter, dead-letter after N (P5).

Claiming happens in a short transaction touching ONLY outbox rows and is
committed before any provider call, so dispatch never holds domain row
locks (gate 1.4). Providers are called through adapters that are
idempotent by event id (gate 1.2).

P13.17(e) / DSA-6 hardening (no observable delivery-semantics change):

* the claimed batch is leased with ONE set-based UPDATE instead of one
  UPDATE per row — same rows (exactly the SKIP LOCKED claim, still
  locked by the claim transaction), same CLAIM_LEASE_SECONDS lease,
  still committed BEFORE any provider call;
* dispatched rows are delivery receipts, not an archive: purge_dispatched
  deletes them once older than DISPATCHED_RETENTION_DAYS, in bounded
  batches through the shared batch runner (v1.1 rule 8) so the purge
  never holds a long transaction. Pending rows are undone work and dead
  rows are the alertable dead-letter queue — neither is EVER purged;
* run_dispatch_cycle discovers due tenants with one registry query
  (outbox_due_tenant_ids(), migration 0024 — the 0003 SECURITY DEFINER
  pattern) instead of sweeping every active tenant each interval, with
  the !37 per-tenant error isolation on the walk.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import traceback
import uuid
from dataclasses import dataclass
from functools import partial
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from genesis.application.batch_runner import run_in_batches
from genesis.infrastructure.providers import NotificationProvider
from genesis.infrastructure.tenancy import tenant_session

logger = logging.getLogger("genesis.infrastructure.outbox")

MAX_ATTEMPTS = 8
BASE_BACKOFF_SECONDS = 30
CLAIM_LEASE_SECONDS = 300
#: DSA-6 retention window for dispatched rows. Config-owned exactly like
#: MAX_ATTEMPTS above (v1.1 rule 1): a module constant, never
#: caller-supplied — changing retention policy is a reviewed code change.
#: Applies ONLY to status = 'dispatched'; pending and dead rows are
#: exempt by status, not by age.
DISPATCHED_RETENTION_DAYS = 30
#: Purge batch bound: each DELETE removes at most this many rows inside
#: its own short transaction (gate 1.3 — no long transactions).
PURGE_BATCH_SIZE = 500
#: run_worker interleaves a purge cycle on this slower cadence: the
#: retention window is measured in days, so hourly is ample and the
#: hot dispatch loop stays cheap.
PURGE_INTERVAL_SECONDS = 3600.0


def backoff_delay(attempts: int, jitter: float) -> float:
    """Exponential backoff with jitter in [0.5x, 1x] of the raw delay."""
    return float(BASE_BACKOFF_SECONDS * (2**attempts)) * (0.5 + jitter / 2)


def _jitter() -> float:
    return secrets.randbelow(1000) / 1000


@dataclass(frozen=True)
class OutboxMetrics:
    pending: int
    dead: int
    dispatched: int
    oldest_pending_seconds: float


async def dispatch_due(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    provider: NotificationProvider,
    *,
    batch_size: int = 20,
) -> int:
    """Dispatch due pending events for one tenant; returns the delivered count.

    Phase 1 claims a batch (SKIP LOCKED + lease) and commits. Phase 2 calls
    the provider outside any transaction. Phase 3 records the outcome in a
    fresh short transaction per event.
    """
    async with tenant_session(factory, tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, event_type, payload, attempts FROM outbox_events "
                    "WHERE status = 'pending' AND next_attempt_at <= now() "
                    "ORDER BY next_attempt_at LIMIT :limit FOR UPDATE SKIP LOCKED"
                ),
                {"limit": batch_size},
            )
        ).all()
        claimed = [(str(r[0]), str(r[1]), dict(r[2]), int(r[3])) for r in rows]
        if claimed:
            # DSA-6: ONE set-based lease UPDATE for the whole claimed
            # batch (previously one UPDATE per row). The id set is
            # exactly the SKIP LOCKED claim above — the rows are still
            # locked by this transaction, so no other worker can lease
            # or take them — and the claim transaction still commits
            # BEFORE any provider call.
            await session.execute(
                text(
                    "UPDATE outbox_events "
                    "SET next_attempt_at = now() + make_interval(secs => :lease) "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {
                    "lease": CLAIM_LEASE_SECONDS,
                    "ids": [event_id for event_id, _, _, _ in claimed],
                },
            )
    delivered = 0
    for event_id, event_type, payload, attempts in claimed:
        try:
            await provider.send(event_id, event_type, payload)
        except Exception:
            logger.exception("outbox dispatch failed for event %s", event_id)
            await _record_failure(
                factory,
                tenant_id,
                event_id,
                attempts + 1,
                traceback.format_exc(limit=3)[:1000],
            )
        else:
            delivered += 1
            async with tenant_session(factory, tenant_id) as session:
                await session.execute(
                    text(
                        "UPDATE outbox_events SET status = 'dispatched', "
                        "dispatched_at = now(), attempts = :attempts "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"attempts": attempts + 1, "id": event_id},
                )
    return delivered


async def _record_failure(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    event_id: str,
    attempts: int,
    error_text: str,
) -> None:
    async with tenant_session(factory, tenant_id) as session:
        if attempts >= MAX_ATTEMPTS:
            await session.execute(
                text(
                    "UPDATE outbox_events SET status = 'dead', attempts = :attempts, "
                    "last_error = :err WHERE id = CAST(:id AS uuid)"
                ),
                {"attempts": attempts, "err": error_text, "id": event_id},
            )
            logger.error("outbox event dead-lettered: %s", event_id)
            return
        delay = backoff_delay(attempts, _jitter())
        await session.execute(
            text(
                "UPDATE outbox_events SET attempts = :attempts, last_error = :err, "
                "next_attempt_at = now() + make_interval(secs => :delay) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"attempts": attempts, "err": error_text, "delay": delay, "id": event_id},
        )


async def purge_dispatched(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    batch_size: int = PURGE_BATCH_SIZE,
) -> int:
    """Delete dispatched rows older than the retention window (DSA-6).

    Bounded batches through the shared batch runner (v1.1 rule 8): each
    DELETE claims at most batch_size rows via a FOR UPDATE SKIP LOCKED
    driving subquery (served by idx_outbox_dispatched_purge, migration
    0024) inside its OWN short transaction, so the purge never holds a
    long transaction and never waits on a row another worker holds.
    Re-runs are lock-free no-ops: zero rows match, zero rows locked.

    ONLY status = 'dispatched' rows are eligible. Pending rows are
    undone work and dead rows are the alertable dead-letter queue — the
    status predicate, not age, is the guard (P13.17 FM5). The purge
    touches outbox_events rows only: the outbox stays a single-node
    locker (lock-order.md §3).
    """

    async def _purge_batch(
        session: AsyncSession, _cursor: uuid.UUID | None
    ) -> tuple[int, uuid.UUID | None, None]:
        result = cast(
            CursorResult[Any],
            await session.execute(
                text(
                    "DELETE FROM outbox_events WHERE id IN ("
                    "SELECT id FROM outbox_events WHERE status = 'dispatched' "
                    "AND dispatched_at < now() - make_interval(days => :days) "
                    "LIMIT :limit FOR UPDATE SKIP LOCKED)"
                ),
                {"days": DISPATCHED_RETENTION_DAYS, "limit": batch_size},
            ),
        )
        return int(result.rowcount or 0), None, None

    purged, _, _ = await run_in_batches(
        partial(tenant_session, factory, tenant_id), _purge_batch, batch_size=batch_size
    )
    return purged


async def outbox_metrics(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> OutboxMetrics:
    """Lag and failure metrics; P21 wires these into exporters and alerts."""
    async with tenant_session(factory, tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT "
                    "count(*) FILTER (WHERE status = 'pending') AS pending, "
                    "count(*) FILTER (WHERE status = 'dead') AS dead, "
                    "count(*) FILTER (WHERE status = 'dispatched') AS dispatched, "
                    "coalesce(extract(epoch FROM now() - min(created_at) "
                    "FILTER (WHERE status = 'pending')), 0) AS oldest "
                    "FROM outbox_events"
                )
            )
        ).one()
    return OutboxMetrics(
        pending=int(row[0]),
        dead=int(row[1]),
        dispatched=int(row[2]),
        oldest_pending_seconds=float(row[3]),
    )


async def list_active_tenants(
    factory: async_sessionmaker[AsyncSession],
) -> list[uuid.UUID]:
    """Tenant registry via the SECURITY DEFINER function (migration 0003)."""
    async with factory() as session:
        rows = (await session.execute(text("SELECT active_tenant_ids()"))).scalars().all()
    return [uuid.UUID(str(value)) for value in rows]


async def list_due_tenants(
    factory: async_sessionmaker[AsyncSession],
) -> list[uuid.UUID]:
    """Active tenants that have due pending events right now (DSA-6).

    One registry query through the SECURITY DEFINER pattern of 0003
    (outbox_due_tenant_ids(), migration 0024) replaces the every-tenant
    sweep: a tenant with no due work costs zero claim queries. The due
    set IS the pending set, so idx_outbox_pending (0001) serves it.
    """
    async with factory() as session:
        rows = (await session.execute(text("SELECT outbox_due_tenant_ids()"))).scalars().all()
    return [uuid.UUID(str(value)) for value in rows]


async def list_purgeable_tenants(
    factory: async_sessionmaker[AsyncSession],
) -> list[uuid.UUID]:
    """Active tenants holding dispatched rows older than retention (DSA-6).

    The cutoff is computed here from DISPATCHED_RETENTION_DAYS (config-
    owned, module constant) — never caller-supplied. Served by
    idx_outbox_dispatched_purge (migration 0024).
    """
    async with factory() as session:
        result = await session.execute(
            text("SELECT outbox_purgeable_tenant_ids(now() - make_interval(days => :days))"),
            {"days": DISPATCHED_RETENTION_DAYS},
        )
        rows = result.scalars().all()
    return [uuid.UUID(str(value)) for value in rows]


async def run_dispatch_cycle(
    factory: async_sessionmaker[AsyncSession], provider: NotificationProvider
) -> int:
    """One pass over the tenants that actually have due work (DSA-6).

    Per-tenant isolation for unexpected failures (the !37 dormancy-cycle
    pattern, preserved on the due-tenant walk): one tenant's dispatch
    error is logged with its stack trace and the cycle keeps walking —
    the failed tenant's events stay pending (or leased) and are retried
    next cycle. Never a silent pass (gate 1.2).
    """
    delivered = 0
    for tenant_id in await list_due_tenants(factory):
        try:
            delivered += await dispatch_due(factory, tenant_id, provider)
        except Exception:
            logger.exception("outbox dispatch errored for tenant %s", tenant_id)
    return delivered


async def run_purge_cycle(factory: async_sessionmaker[AsyncSession]) -> int:
    """One retention-purge pass over tenants with purgeable rows (DSA-6).

    Same per-tenant isolation as the dispatch cycle: a failed tenant is
    logged and skipped; its rows are simply purged on a later cycle
    (the purge is idempotent by side-effect counts).
    """
    purged = 0
    for tenant_id in await list_purgeable_tenants(factory):
        try:
            purged += await purge_dispatched(factory, tenant_id)
        except Exception:
            logger.exception("outbox purge errored for tenant %s", tenant_id)
    return purged


async def run_worker(
    factory: async_sessionmaker[AsyncSession],
    provider: NotificationProvider,
    *,
    interval_seconds: float = 5.0,
    purge_interval_seconds: float = PURGE_INTERVAL_SECONDS,
    stop: asyncio.Event | None = None,
) -> None:
    """Long-running dispatcher loop; deployed as the worker process.

    Dispatch runs every interval; the retention purge (DSA-6) is
    interleaved on its own slower cadence so the hot loop stays cheap.
    """
    next_purge = 0.0
    while stop is None or not stop.is_set():
        try:
            await run_dispatch_cycle(factory, provider)
        except Exception:
            logger.exception("outbox dispatch cycle failed")
        if time.monotonic() >= next_purge:
            next_purge = time.monotonic() + purge_interval_seconds
            try:
                await run_purge_cycle(factory)
            except Exception:
                logger.exception("outbox purge cycle failed")
        await asyncio.sleep(interval_seconds)
