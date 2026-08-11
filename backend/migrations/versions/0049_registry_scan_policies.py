"""registry-scan RLS policies so worker discovery works without BYPASSRLS

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-11

The three worker discovery functions — active_tenant_ids() (0003),
outbox_due_tenant_ids() and outbox_purgeable_tenant_ids() (0024) — are
SECURITY DEFINER, and both migrations state the assumption plainly:

    "The defining role is the migration role (BYPASSRLS/superuser in CI
     and ops environments)."

That assumption does not hold on managed hosting. There the application
role OWNS the tables, and PostgreSQL's FORCE ROW LEVEL SECURITY applies
to owners too, so SECURITY DEFINER confers nothing: every discovery
function returns ZERO rows. The failure is SILENT — the workers report
`tenants=0` and exit 0, so the outbox never dispatches, exports never
render and dormancy never runs, with no error anywhere. Observed on a
real deployment; a superuser-owned local database hides it completely.

This adds an EXPLICIT, opt-in scan policy to the two tables discovery
reads. A connection that sets

    app.registry_scan = 'on'

may SELECT the rows the registries need; nothing else changes. The GUC
is set ONLY by the worker registry helper
(infrastructure.tenancy.registry_session) — request paths never set it,
so no API surface widens. It sits inside the existing trust model, which
already relies on the application being the sole setter of
app.tenant_id.

Deliberately SELECT-only and additive: policies are OR'ed, so the
tenant-isolation policies keep governing every write and every scoped
read exactly as before. Falsifiable: scan without the GUC and discovery
returns nothing (tests/test_registry_scan.py pins both directions).

Downgrade drops both policies, restoring the pre-0049 behaviour.
"""

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

_UP = """
-- current_setting(..., true) = missing_ok: an unset GUC yields NULL,
-- the predicate is NULL (not true), and the policy grants nothing.
CREATE POLICY registry_scan ON tenants
    FOR SELECT
    USING (current_setting('app.registry_scan', true) = 'on');

CREATE POLICY registry_scan ON outbox_events
    FOR SELECT
    USING (current_setting('app.registry_scan', true) = 'on');

-- The isolation policies on these two tables must also tolerate an
-- UNSET app.tenant_id. Policies are OR'ed, but every one of them is
-- EVALUATED, and 0001 wrote these without the missing_ok flag:
--     current_setting('app.tenant_id')::uuid
-- On a connection that has not scoped a tenant — exactly the registry
-- scan above — that RAISES
--     unrecognized configuration parameter "app.tenant_id"
-- before the scan policy can grant anything, so registry_scan alone is
-- inert. Re-created with missing_ok so an unset GUC yields NULL, the
-- comparison is NULL, and the policy simply does not match.
--
-- Trade-off, stated plainly: an unscoped query against these two tables
-- used to fail LOUDLY and now returns zero rows. Both are fail-closed —
-- no row ever escapes its tenant — but the loud form catches a
-- forgotten scope faster. Limited to tenants and outbox_events (the two
-- the registries read); every other table keeps the loud behaviour.
DROP POLICY tenant_self ON tenants;
CREATE POLICY tenant_self ON tenants
    USING (id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (id = current_setting('app.tenant_id', true)::uuid);

DROP POLICY tenant_isolation ON outbox_events;
CREATE POLICY tenant_isolation ON outbox_events
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
"""

_DOWN = """
DROP POLICY IF EXISTS registry_scan ON outbox_events;
DROP POLICY IF EXISTS registry_scan ON tenants;

-- Restore 0001's exact (non-missing_ok) isolation predicates.
DROP POLICY IF EXISTS tenant_isolation ON outbox_events;
CREATE POLICY tenant_isolation ON outbox_events
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

DROP POLICY IF EXISTS tenant_self ON tenants;
CREATE POLICY tenant_self ON tenants
    USING (id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (id = current_setting('app.tenant_id')::uuid);
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
