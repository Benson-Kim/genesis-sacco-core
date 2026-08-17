"""Worker discovery must work WITHOUT BYPASSRLS (migration 0049).

The three registries — active_tenant_ids, outbox_due_tenant_ids,
outbox_purgeable_tenant_ids — are SECURITY DEFINER, which bypasses RLS
only when the DEFINING role holds BYPASSRLS. On every managed host the
application role owns the tables instead, and FORCE ROW LEVEL SECURITY
applies to owners, so those functions returned ZERO rows and every
worker silently no-opped: outbox undispatched, exports unrendered,
dormancy never run, exit code 0 throughout.

0049 adds a SELECT-only `registry_scan` policy keyed on the
app.registry_scan GUC, set exclusively by
infrastructure.tenancy.registry_session.

Both directions are pinned here, so the guard is falsifiable:
  - WITH the GUC (registry_session): discovery sees the tenant.
  - WITHOUT it (a plain session): discovery sees NOTHING — delete the
    set_config from registry_session, or the policy from 0049, and the
    first test fails; grant the scan unconditionally and the second
    fails.

These run under the CI app role, which is deliberately non-superuser and
non-BYPASSRLS — the same posture as production, and the only posture in
which this bug is visible at all.
"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text

from db_helpers import factory
from genesis.infrastructure.outbox_worker import list_active_tenants
from genesis.infrastructure.tenancy import registry_session

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


async def _seed_active_tenant() -> uuid.UUID:
    """One active tenant, inserted the way the bootstrap does it.

    tenants carries FORCE RLS, so the very first row needs the fence
    lifted for exactly one INSERT — the provision_tenant.sql precedent.
    """
    tenant_id = uuid.uuid4()
    async with factory()() as session, session.begin():
        await session.execute(text("ALTER TABLE tenants NO FORCE ROW LEVEL SECURITY"))
        await session.execute(
            text(
                "INSERT INTO tenants (id, name, slug, status) "
                "VALUES (CAST(:tid AS uuid), :name, :slug, 'active')"
            ),
            {"tid": str(tenant_id), "name": f"Registry {tenant_id}", "slug": f"reg-{tenant_id}"},
        )
        await session.execute(text("ALTER TABLE tenants FORCE ROW LEVEL SECURITY"))
    return tenant_id


def test_registry_scan_finds_the_active_tenant_without_bypassrls() -> None:
    """WITH app.registry_scan the registry sees the tenant.

    Falsifiable: drop the set_config from registry_session, or the
    policy from 0049, and this returns an empty list — the exact silent
    failure that stopped every worker in production.
    """

    async def run() -> None:
        tenant_id = await _seed_active_tenant()
        found = await list_active_tenants(factory())
        assert tenant_id in found, (
            "registry discovery returned nothing for an ACTIVE tenant — "
            "workers would silently process no tenant at all"
        )

    asyncio.run(run())


def test_registry_scan_grants_nothing_without_the_guc() -> None:
    """WITHOUT the GUC the same query sees NOTHING.

    Proves the policy is opt-in rather than a blanket widening of
    tenants: a request-path session (which never sets the GUC) gains no
    cross-tenant visibility from 0049.
    """

    async def run() -> None:
        await _seed_active_tenant()
        # A plain session: no app.registry_scan, no app.tenant_id.
        async with factory()() as session:
            rows = (await session.execute(text("SELECT active_tenant_ids()"))).scalars().all()
        assert list(rows) == [], (
            "the registry answered a session that never set "
            "app.registry_scan — 0049 would be a blanket read widening"
        )

    asyncio.run(run())


def test_registry_guc_does_not_outlive_its_transaction() -> None:
    """The GUC is transaction-local, so a pooled connection cannot leak it.

    set_config(..., true) is the SET LOCAL form; were it session-scoped,
    the NEXT user of this connection would inherit scan rights.
    """

    async def run() -> None:
        await _seed_active_tenant()
        async with registry_session(factory()) as session:
            inside = (
                await session.execute(text("SELECT current_setting('app.registry_scan', true)"))
            ).scalar_one()
        assert inside == "on"

        async with factory()() as session:
            after = (
                await session.execute(text("SELECT current_setting('app.registry_scan', true)"))
            ).scalar_one()
        assert after in (None, ""), f"registry GUC leaked past its transaction: {after!r}"

    asyncio.run(run())
