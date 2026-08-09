"""Per-request tenant scoping via Postgres RLS (ADR-0002, least disclosure)."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@asynccontextmanager
async def tenant_session(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> AsyncIterator[AsyncSession]:
    """Yield a session whose transaction is scoped to exactly one tenant.

    set_config(..., true) is equivalent to SET LOCAL: the GUC is bound to the
    current transaction only, so pooled connections can never leak tenant
    context between requests. Commits on success, rolls back on error.
    """
    async with session_factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        yield session


@asynccontextmanager
async def tenant_snapshot_session(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> AsyncIterator[AsyncSession]:
    """Tenant session on a REPEATABLE READ transaction (blocker h).

    Exports read across multiple statements (opening balances, keyset
    batches, per-month aggregates); under READ COMMITTED a concurrent
    settlement could commit between two of them and leave the rendered
    document internally inconsistent. REPEATABLE READ pins one MVCC
    snapshot for the whole job transaction, so an export can never
    observe partial state. The isolation level is applied to the
    connection before the transaction begins and is reset by the pool
    on return.
    """
    async with session_factory() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        try:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            yield session
            await session.commit()
        finally:
            # No-op after a successful commit; undoes everything when
            # the block above raised (the kill-switch guarantee).
            await session.rollback()
