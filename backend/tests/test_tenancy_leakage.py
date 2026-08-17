"""Cross-tenant leakage suite (ADR-0002). Release blocker: must always be green."""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

TENANT_TABLES = [
    "roles",
    "permissions",
    "users",
    "branches",
    "members",
    "share_accounts",
    "deposit_accounts",
    "loan_products",
    "loan_applications",
    "loans",
    "loan_schedules",
    "transactions",
    "ledger_entries",
    "repayments",
    "guarantees",
    "member_exits",
    "exit_votes",
    # P13.7: the tenant settings row holds the money parameters, so its
    # RLS posture is release-blocking like every other tenant table.
    "tenant_settings",
    "outbox_events",
    "audit_log",
    "idempotency_keys",
    "otp_challenges",
    "refresh_tokens",
]


def _factory() -> async_sessionmaker[AsyncSession]:
    return get_sessionmaker(os.environ["DATABASE_URL"])


async def _create_tenant(name: str) -> uuid.UUID:
    tid = uuid.uuid4()
    async with tenant_session(_factory(), tid) as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (CAST(:id AS uuid), :name, :slug)"),
            {"id": str(tid), "name": name, "slug": f"{name}-{tid.hex[:8]}"},
        )
    return tid


async def _insert_member(session_tenant: uuid.UUID, row_tenant: uuid.UUID) -> uuid.UUID:
    mid = uuid.uuid4()
    async with tenant_session(_factory(), session_tenant) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', :name)"
            ),
            {
                "id": str(mid),
                "tid": str(row_tenant),
                "no": f"GP-{mid.hex[:6]}",
                "name": "Leakage Probe",
            },
        )
    return mid


def test_rls_enabled_forced_and_policied_on_every_tenant_table() -> None:
    async def run() -> None:
        tid = await _create_tenant("struct")
        stmt = text(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
            "count(p.polname) AS policies "
            "FROM pg_class c LEFT JOIN pg_policy p ON p.polrelid = c.oid "
            "WHERE c.relname IN :tables GROUP BY 1, 2, 3"
        ).bindparams(bindparam("tables", expanding=True))
        async with tenant_session(_factory(), tid) as session:
            rows = (await session.execute(stmt, {"tables": [*TENANT_TABLES, "tenants"]})).all()
        found = {row[0]: row for row in rows}
        for table in [*TENANT_TABLES, "tenants"]:
            assert table in found, f"table missing: {table}"
            _, enabled, forced, policies = found[table]
            assert enabled, f"RLS not enabled on {table}"
            assert forced, f"RLS not forced on {table} (owner would bypass)"
            assert policies >= 1, f"no policy on {table}"

    asyncio.run(run())


def test_cross_tenant_rows_are_invisible_and_immutable() -> None:
    async def run() -> None:
        tid_a = await _create_tenant("alpha")
        tid_b = await _create_tenant("beta")
        await _insert_member(tid_a, tid_a)

        async with tenant_session(_factory(), tid_b) as session:
            count = (await session.execute(text("SELECT count(*) FROM members"))).scalar_one()
            assert count == 0, "tenant B can see tenant A rows"
            updated = await session.execute(text("UPDATE members SET name = 'hijack'"))
            assert updated.rowcount == 0, "tenant B can update tenant A rows"
            deleted = await session.execute(text("DELETE FROM members"))
            assert deleted.rowcount == 0, "tenant B can delete tenant A rows"

        async with tenant_session(_factory(), tid_a) as session:
            count = (await session.execute(text("SELECT count(*) FROM members"))).scalar_one()
            assert count == 1, "tenant A cannot see its own row"

    asyncio.run(run())


def test_cross_tenant_branch_rows_are_invisible_and_immutable() -> None:
    """P13.6 EXIT: leakage suite extended to branches.

    Tenant B must not read, update, delete or insert tenant A branch
    rows even via raw SQL through the app role (ADR-0002).
    """

    async def run() -> None:
        tid_a = await _create_tenant("branch-a")
        tid_b = await _create_tenant("branch-b")
        async with tenant_session(_factory(), tid_a) as session:
            await session.execute(
                text(
                    "INSERT INTO branches (tenant_id, name) "
                    "VALUES (CAST(:tid AS uuid), 'Leakage HQ')"
                ),
                {"tid": str(tid_a)},
            )

        async with tenant_session(_factory(), tid_b) as session:
            count = (await session.execute(text("SELECT count(*) FROM branches"))).scalar_one()
            assert count == 0, "tenant B can see tenant A branches"
            updated = await session.execute(text("UPDATE branches SET name = 'hijack'"))
            assert updated.rowcount == 0, "tenant B can update tenant A branches"
            deleted = await session.execute(text("DELETE FROM branches"))
            assert deleted.rowcount == 0, "tenant B can delete tenant A branches"

        with pytest.raises(DBAPIError):
            async with tenant_session(_factory(), tid_b) as session:
                await session.execute(
                    text(
                        "INSERT INTO branches (tenant_id, name) "
                        "VALUES (CAST(:tid AS uuid), 'Forged Branch')"
                    ),
                    {"tid": str(tid_a)},
                )

        async with tenant_session(_factory(), tid_a) as session:
            count = (await session.execute(text("SELECT count(*) FROM branches"))).scalar_one()
            assert count == 1, "tenant A cannot see its own branch"

    asyncio.run(run())


def test_cross_tenant_write_is_rejected() -> None:
    async def run() -> None:
        tid_a = await _create_tenant("gamma")
        tid_b = await _create_tenant("delta")
        with pytest.raises(DBAPIError):
            await _insert_member(session_tenant=tid_b, row_tenant=tid_a)

    asyncio.run(run())


def test_missing_tenant_context_fails_loudly() -> None:
    async def run() -> None:
        factory = _factory()
        async with factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(text("SELECT count(*) FROM members"))

    asyncio.run(run())
