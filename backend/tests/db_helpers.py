"""Shared DB-backed test helpers (not a test module)."""

import os
import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from genesis.api.app import create_app
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session


def factory() -> async_sessionmaker[AsyncSession]:
    return get_sessionmaker(os.environ["DATABASE_URL"])


def api_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


async def seed_user(
    email: str, role_name: str = "System Admin", *, phone: str | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a tenant with one role and one active user; returns (tenant, role)."""
    tid = uuid.uuid4()
    role_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, name, slug) "
                "VALUES (CAST(:id AS uuid), 'Test Tenant', :slug)"
            ),
            {"id": str(tid), "slug": f"t-{tid.hex[:12]}"},
        )
        await session.execute(
            text(
                "INSERT INTO roles (id, tenant_id, name, is_system) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, true)"
            ),
            {"id": str(role_id), "tid": str(tid), "name": role_name},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, role_id, full_name, email, phone) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), "
                "CAST(:rid AS uuid), 'Test User', :email, :phone)"
            ),
            {
                "id": str(user_id),
                "tid": str(tid),
                "rid": str(role_id),
                "email": email,
                "phone": phone,
            },
        )
    return tid, role_id


async def latest_otp_code(tid: uuid.UUID) -> str:
    async with tenant_session(factory(), tid) as session:
        code = (
            await session.execute(
                text(
                    "SELECT payload->>'code' FROM outbox_events "
                    "WHERE event_type = 'auth.otp_requested' "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).scalar_one()
    return str(code)


def unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:10]}@example.com"
